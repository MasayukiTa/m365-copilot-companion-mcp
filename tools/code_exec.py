import os
import subprocess
import sys
import tempfile
from typing import Optional

from ._subproc import sanitized_child_env
from .file_ops import _validate_path
from .security import require_unlocked
from .shell_extra import _gate_detail


def _working_dir(path: Optional[str]) -> str:
    if not path:
        return os.getcwd()
    p = _validate_path(path)
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    return str(p)


def _decode(raw: bytes) -> str:
    """子プロセスの出力を、落とさずに文字へ直す。

    text=True にすると、その場のコードページ（日本語Windowsなら cp932）で復号する。
    UTF-8 で出力するスクリプトを走らせると、そこで例外になって出力が丸ごと消える。
    実際、日本語を出す調査スクリプトが returncode 0 なのに「cp932 で復号できない」で
    落ち、呼び出し側はファイルへ迂回する羽目になった。

    UTF-8 を先に試し、駄目ならその場のコードページへ落とす。どちらでも読めない字は
    捨てずに置き換える。出力の一部が化けるより、出力ごと消えるほうが困る。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    import locale
    fallback = locale.getpreferredencoding(False) or "utf-8"
    return raw.decode(fallback, errors="replace")


def _format_output(result, label: str) -> str:
    out = ""
    stdout, stderr = _decode(result.stdout), _decode(result.stderr)
    if stdout:
        out += f"[stdout]\n{stdout}"
    if stderr:
        out += f"[stderr]\n{stderr}"
    if result.returncode != 0:
        out += f"\n[returncode: {result.returncode}]"
    return out or "(no output)"


def run_python(
    code: str,
    timeout: int = 60,
    working_dir: Optional[str] = None,
) -> str:
    """Run Python code in a temporary file and return stdout, stderr, and return code.

    NOT a sandbox. Under an active autonomy contract, destructive file ops in the
    source (os.remove/unlink/rmdir, shutil.rmtree/move, pathlib unlink/rmdir,
    truncating open(...,'w'), and os.system/subprocess escape hatches) are routed
    through the contract gate (op_class 'shell_destructive') for approval — this is
    detection-based, so it can miss obfuscated code. Treat run_python as not fully
    sandboxed when granting long-running autonomy.

    Args:
        code: Python source code to execute.
        timeout: Maximum execution time in seconds.
        working_dir: Optional working directory under the allowed base.

    If the script produces an artifact, verify it before declaring success: read_image
    for a saved plot/image, or verify_python / verify_file_contains for a computed result.
    """
    locked = require_unlocked()
    if locked:
        return locked
    from . import contract_gate as _cg
    # Gate destructive ops whether expressed as shell text OR as Python source. Both are
    # routed through the existing 'shell_destructive' op_class so any contract that already
    # asks-before destructive shell also covers destructive Python (no schema change needed).
    if _cg.destructive_shell(code) or _cg.destructive_python(code):
        _g = _cg.check_op("shell_destructive", _gate_detail(code))
        if _g is not None:
            return _g
    _j = _judged("python", code, working_dir)
    if _j is not None:
        return _j
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name

        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            timeout=timeout,
            cwd=_working_dir(working_dir),
            shell=False,
            env=sanitized_child_env(),
        )
        return _format_output(result, "run_python")
    except subprocess.TimeoutExpired:
        return f"[timeout: exceeded {timeout} seconds]"
    except Exception as e:
        return f"[run_python error: {type(e).__name__}: {e}]"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _judged(kind, text, working_dir):
    """Contextual judgement, ahead of running anything. Returns a refusal string or None.

    ORDER, AND WHY. The deterministic checks above run first and this cannot overrule them --
    a model may not waive a rule the machine is sure about. What it adds is the part a pattern
    list structurally cannot see: whether the effect is what the user actually asked for, at
    the blast radius they asked for, on the directory they meant.

    Judging only what the regex flags would leave exactly those out. So everything that is not
    provably read-only is judged, and the read-only exemption is what keeps the latency
    survivable -- see tools/command_triage.py.

    SHADOW BY DEFAULT. The verdict is recorded and nothing is blocked until MCP_JUDGE_MODE is
    set to enforce. Switching a gate from permissive to closed without measuring first is a
    mistake this repository has been corrected for once already.
    """
    try:
        from . import command_triage as _tri
        m = _tri.mode()
        if m == "off":
            return None
        exempt, why = _tri.is_read_only(text) if kind == "shell" else (False, "python source")
        if exempt:
            return None

        from . import command_judge as _cj
        req = _cj.build_request(
            command=text,
            cwd=_working_dir(working_dir) or "",
            workspace_root=str(_ALLOWED_BASE_FOR_JUDGE()),
            deterministic_flags=_deterministic_flags(text),
        )
        judge = _judge_backend()
        verdict = _cj.judge_command(req, judge)
        blocks = _cj.outcome_blocks_execution(verdict, human_available=False)
        approved = None
        if m == "enforce" and blocks:
            # THE PERSON MAY OVERRULE EITHER LAYER, and this is the only place they can.
            # "引っかかったものでも問題なしとユーザが明示的に承認したら当然実行OK。それは
            # ユーザの責任" -- so an explicit approval releases a BLOCK_AND_RETRY as well as a
            # REQUIRE_HUMAN. What it does not do is release a deterministic rule: that ran
            # before this function was called, and is not reconsidered here.
            #
            # ONLY IN ENFORCE. Shadow exists to measure without changing behaviour, and a
            # prompt on every unrecognised command is a change in behaviour -- and the fastest
            # way to have the layer switched off before it has been measured once.
            #
            # None (nobody could be asked) is a decline, never a pass.
            approved = _ask_operator(text, verdict)
            if approved:
                blocks = False
        _record_judgement(kind, text, req, verdict, m, blocks, human=approved)
        if m == "enforce" and blocks:
            return ("[refused by review] %s\n"
                    "The pending command was assessed before running and was not allowed: %s\n"
                    "Choose a narrower action, or ask the operator to approve this one."
                    % (verdict.get("decision"), verdict.get("reason") or "no reason given"))
        return None
    except Exception as exc:
        # FAILURE IS NOT PERMISSION, INCLUDING MY OWN FAILURE. This handler used to return None
        # for everything, and its comment claimed "a crash here is not an allow" while doing
        # exactly that. It was not a hypothetical: adding the `human` argument to
        # _record_judgement broke a test stub, every call raised TypeError, this line swallowed
        # it, and four tests reported an empty log rather than a signature error. In shadow
        # that costs nothing. In enforce it would have been an unjudged command running because
        # the judging code had a bug -- which is the precise failure the whole layer exists to
        # prevent, one level down.
        #
        # Shadow still never costs a command: measuring must not change behaviour, and an
        # exception in a layer that is only observing is a defect to fix, not a refusal to
        # issue.
        try:
            enforcing = _mode_is_enforce()
        except Exception:
            enforcing = False
        if not enforcing:
            return None
        return ("[refused by review] internal error\n"
                "The pre-execution review could not complete (%s: %s), and an unassessed "
                "command is not permitted while review is enforced."
                % (type(exc).__name__, str(exc)[:200]))


def _mode_is_enforce():
    from . import command_triage as _tri
    return _tri.mode() == "enforce"


def _ALLOWED_BASE_FOR_JUDGE():
    try:
        from .file_ops import ALLOWED_BASE
        return ALLOWED_BASE
    except Exception:
        return ""


def _deterministic_flags(text):
    try:
        from . import contract_gate as _cg
        flags = []
        if _cg.destructive_shell(text):
            flags.append("destructive_shell")
        try:
            if _cg.destructive_python(text):
                flags.append("destructive_python")
        except Exception:
            pass
        return flags
    except Exception:
        return []


def _ask_operator(text, verdict):
    """Put the blocked command to the person at the client. True only on explicit approval.

    Returns None when nobody could be asked -- no request context, a client without
    elicitation, a timeout. The caller treats None exactly as a decline: an unattended run
    that reads "could not ask" as "go ahead" has inverted the point of asking.
    """
    try:
        from . import judge_backend as _jb
        question = (
            "この操作を実行してよいか確認してください。事前レビューで止まりました。\n"
            "  コマンド: %s\n"
            "  判定: %s\n"
            "  理由: %s\n"
            "承認した場合、この実行の責任は承認者にあります。"
            % ((text or "")[:300],
               verdict.get("decision"),
               (verdict.get("reason") or "理由なし")[:300]))
        return _jb.ask_human(question)
    except Exception:
        return None


def _judge_backend():
    """The callable that asks a model, or None when none is configured.

    Kept behind a lookup so this module never imports a transport, and so a deployment with no
    judge available is a first-class state rather than an import error. With no backend the
    verdict is REQUIRE_HUMAN -- which in shadow is recorded and in enforce is a refusal.
    """
    try:
        from . import judge_backend as _jb
        return _jb.get()
    except Exception:
        return None


def _record_judgement(kind, text, req, verdict, mode_name, blocks, human=None):
    """One line per judged command, including allows.

    Raw command text is recorded because a verdict cannot be reviewed without knowing what was
    judged -- and it is truncated, because a command line can carry a token. Output is never
    recorded here.
    """
    try:
        import hashlib
        import json as _json
        import os as _os
        import time as _time
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        path = _os.path.join(repo, ".fleet", "judge.jsonl")
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        row = {
            "ts": _time.time(),
            "mode": mode_name,
            "kind": kind,
            "cmd_sha16": hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16],
            "cmd": (text or "")[:400],
            "cwd": req.get("cwd"),
            "inside_workspace": req.get("inside_workspace"),
            "flags": req.get("deterministic_flags"),
            "decision": verdict.get("decision"),
            "categories": verdict.get("categories"),
            "reason": (verdict.get("reason") or "")[:300],
            "source": verdict.get("source"),
            "would_block": bool(blocks),
            # THREE STATES, NOT TWO. null = nobody was asked (shadow, or the command was
            # allowed outright); true = a person explicitly approved and owns the decision;
            # false = asked and declined, OR nobody could be reached. A boolean alone could
            # not tell "approved" from "never asked", which is the distinction the audit needs.
            "human_approved": human,
            "schema": _json.dumps(None) and 1,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def shell_exec(
    command: str,
    timeout: int = 30,
    working_dir: Optional[str] = None,
) -> str:
    """Run a shell command and return stdout, stderr, and return code.

    Args:
        command: Command line to execute.
        timeout: Maximum execution time in seconds.
        working_dir: Optional working directory under the allowed base.
    """
    locked = require_unlocked()
    if locked:
        return locked
    from . import contract_gate as _cg
    if _cg.destructive_shell(command):
        _g = _cg.check_op("shell_destructive", _gate_detail(command))
        if _g is not None:
            return _g
    _j = _judged("shell", command, working_dir)
    if _j is not None:
        return _j
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=timeout,
            cwd=_working_dir(working_dir),
            env=sanitized_child_env(),
        )
        return _format_output(result, "shell_exec")
    except subprocess.TimeoutExpired:
        return f"[timeout: exceeded {timeout} seconds]"
    except Exception as e:
        return f"[shell_exec error: {type(e).__name__}: {e}]"


#: This tool runs caller-supplied code, so the evidence trace cannot see what it did:
#: it records that the call happened and nothing about its effects. Declared HERE rather
#: than in a list inside evidence_trace.py, because a list in another file is a list that
#: goes stale the first time an executor is added -- which is exactly what happened.
run_python.evidence_opaque = True
shell_exec.evidence_opaque = True
