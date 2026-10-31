# -*- coding: utf-8 -*-
"""judge_gate -- the final-step seam that routes DATA-ANALYSIS answers through the frozen
validity engine before they are delivered.

OWNER INSTRUCTION (2026-10-27, verbatim): 「データ解析系は、すべて最終でこの判定器を
通すハーネスを入れ、databricks を圧倒す。これが先週与えたタスクだ。」 Re-scoped 2026-10-30:
the owner leaves whether to freeze the registered claims to the agent, and the harness must be
able to read past verdicts from the sessions DB.

WHAT THIS MODULE IS, AND IS NOT.
  * It is the NON-SECRET orchestration seam in the public repo. It DECIDES which goals are data
    analysis (so the judge is applied to them and only them), and it COMPOSES the footer that
    carries the engine's verdict (or the reason the engine could not run) onto the final answer.
  * It is NOT the engine. The verdict engine lives OUTSIDE this repo
    (Desktop/AHA資料_参照一式/証明/公開データ検証/engine_v1_8c2/validity), is secret, remote-less
    and byte-identical-locked by PREREGISTRATION_v1_8c2.md. This module READS it (runs its CLI);
    it NEVER edits the engine's code, rules, thresholds (delta_min, k_min, ...) or registered
    claim files. (A) is read-only, by owner instruction.

THE THREE STEPS the harness performs for a data-analysis final answer (owner 「やること」 2):
  (i)   record the analysis RESULT + DATA sha256 and the fetch time  -> validity_harness.record_manifest
  (ii)  register the claim and LOCK it, recording register-vs-fetch time ORDER
                                                                    -> validity_harness.register_and_lock
  (iii) run the engine and put verdict / reason codes / interval / n on the final answer
                                                                    -> run_engine_judge + build_judge_footer
  When the engine cannot be run, build_judge_footer states WHY on the final answer -- never silent.

Data minimisation: this module surfaces only id / verdict / reason codes / hashes / aggregated
numbers (K, df, n, interval). It reads and emits no raw measurement row and no employee id.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

try:
    from relay.task_kind import task_kind, DATA_QUERY
except Exception:  # pragma: no cover - allow flat import when run as a script
    from task_kind import task_kind, DATA_QUERY  # type: ignore

__all__ = [
    "is_data_analysis_goal",
    "build_judge_footer",
    "run_engine_judge",
    "ENGINE_ROOT",
    "MISCLASSIFICATION_NOTE",
]

# The frozen engine checkout (A). Read-only. The engine lives OUTSIDE this public repo on the
# operator's machine, so its path is NOT hard-coded here (a machine-specific absolute path would
# leak a local account name into a public repo). It is supplied at run time via the
# VALIDITY_ENGINE_ROOT environment variable; if unset we fall back to a path under the current
# user's Desktop, built from HOME at run time so no account name is committed. The engine's own
# docs (SKILL.md) record the operator-specific location out-of-band.
_ENGINE_SUBPATH = os.path.join(
    "Desktop", "AHA資料_参照一式", "証明", "公開データ検証", "engine_v1_8c2"
)


def _default_engine_root() -> str:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, _ENGINE_SUBPATH)


ENGINE_ROOT = os.environ.get("VALIDITY_ENGINE_ROOT") or _default_engine_root()

#: WHY THIS NOTE EXISTS. The classifier below is a keyword screen over the goal text (reusing
#: relay/task_kind.py's DATA_QUERY markers, which were read off the live history). A keyword
#: screen necessarily mis-sorts some goals; naming the mis-sorts is what turns the boundary into
#: a decision instead of an accident. The gate is deliberately CONSERVATIVE: a goal is treated as
#: data analysis only when a DATA_QUERY marker is present AND no stronger non-analysis kind
#: (code change / M365 lookup / prose) claims it first -- because applying the judge to a
#: non-analysis answer wastes an engine run and clutters the answer, while MISSING a real
#: analysis is the failure the owner is fixing. When in doubt the caller may force the gate on.
MISCLASSIFICATION_NOTE = (
    "判別条件: task_kind(goal) == DATA_QUERY を基礎に、ADAMS / MCL DB / odbc / sql server 等のデータ"
    "取得と、その結果を表・相関・群間比較にする仕事を「データ解析」と判別する。\n"
    "誤検知の例(本当は解析なのに漏れる / 偽陰性): データ元を名指しせず「前回の表をもう一度"
    "集計して」だけの依頼は、DATA_QUERY マーカーが無ければ unknown に落ちて False になる。\n"
    "誤判定の例(解析でないのに拾う / 偽陽性): 「MCL のコードを small fix する」のようにデータ"
    "語を含むが実際はコード変更の仕事。task_kind は CODE_CHANGE / M365_LOOKUP / WRITING を先に"
    "返す順位なので、それらは解析として拾わない。境界が疑わしいときは呼び出し側が強制 ON にできる。"
)


def is_data_analysis_goal(goal, request_text="", tool_names=()) -> bool:
    """True when `goal` asks for a DB-backed analysis whose answer should pass the judge.

    Apply the existing task-kind rules independently to the goal and resolved request. At
    completion, attributed data-tool calls also signal analysis even if the text did not.
    """
    # The resolved request supplies the substance hidden by a short case reference.
    if any(task_kind(text) == DATA_QUERY for text in (goal, request_text)):
        return True
    # Recorded data-tool calls are evidence of work actually attempted, even when neither
    # the reference nor request described its analysis in the classifier's vocabulary.
    return any(name in {"odbc_query", "odbc_to_excel", "run_python"}
               for name in tool_names)


def complete_judge_request(goal, output_folder, *, strict=False, request_text="", tool_names=()):
    """Evaluate the worker's request at completion; return (accept, visible detail)."""
    if not is_data_analysis_goal(goal, request_text, tool_names):
        return True, ""
    path = os.path.join(output_folder, "judge_request.json") if output_folder else ""
    if not path or not os.path.exists(path):
        return (False, "judge: none (request required)") if strict else (True, "judge: none")
    try:
        with open(path, encoding="utf-8-sig") as f:
            request = json.load(f)
        claim_id, data_location, fetched_at = (request[k] for k in
                                               ("claim_id", "data_location", "fetched_at"))
        if not all(isinstance(v, str) and v.strip() for v in
                   (claim_id, data_location, fetched_at)):
            raise ValueError("request values must be non-empty strings")
        if os.path.basename(claim_id) != claim_id or claim_id in (".", ".."):
            raise ValueError("invalid claim id")
        if not os.path.isabs(data_location):
            data_location = os.path.abspath(os.path.join(output_folder, data_location))
        from tools import validity_harness

        statuses = []
        order_record = None
        manifest = request.get("manifest")
        if not isinstance(manifest, dict) or not all(
                k in manifest for k in ("path", "dataset_id", "result")):
            statuses.append("manifest: not run: path, dataset_id and result are required")
        else:
            try:
                if (not isinstance(manifest["path"], str) or not manifest["path"].strip()
                        or not isinstance(manifest["dataset_id"], str)
                        or not manifest["dataset_id"].strip()
                        or not isinstance(manifest["result"], dict)):
                    raise ValueError("invalid manifest fields")
                manifest_path = os.path.abspath(os.path.join(output_folder, manifest["path"]))
                validity_harness.record_manifest(manifest_path, dataset_id=manifest["dataset_id"],
                                                 data_path=data_location, result=manifest["result"],
                                                 fetched_at=fetched_at)
                statuses.append("manifest: recorded")
            except (OSError, ValueError, TypeError) as e:
                statuses.append("manifest: not run: %s" % e)
        registration = request.get("registration")
        if not isinstance(registration, dict) or not all(
                k in registration for k in ("root", "claim", "registered_at")):
            statuses.append("registration: not run: root, claim and registered_at are required")
        else:
            try:
                claim = registration["claim"]
                if (not isinstance(registration["root"], str) or not registration["root"].strip()
                        or not isinstance(claim, dict) or claim.get("id") != claim_id
                        or not isinstance(registration["registered_at"], str)
                        or not registration["registered_at"].strip()):
                    raise ValueError("invalid registration fields or claim id mismatch")
                # Validate timestamps before register_and_lock writes the claim.
                validity_harness._parse_iso(registration["registered_at"])
                validity_harness._parse_iso(fetched_at)
                root = os.path.abspath(os.path.join(output_folder, registration["root"]))
                order_record = validity_harness.register_and_lock(
                    root, claim=claim, data_fetched_at=fetched_at,
                    registered_at=registration["registered_at"])
                statuses.append("registration: recorded (order_ok=%s)" % order_record["order_ok"])
            except (OSError, ValueError, TypeError, OverflowError) as e:
                statuses.append("registration: not run: %s" % e)
        lot = request.get("lot_diagnosis")
        if not isinstance(lot, dict) or not all(k in lot for k in ("y", "x", "lot")):
            statuses.append("lot diagnosis: not run: y, x and lot are required")
        else:
            try:
                if (not all(isinstance(lot[k], list) and lot[k] for k in ("y", "x", "lot"))
                        or len({len(lot[k]) for k in ("y", "x", "lot")}) != 1):
                    raise ValueError("y, x and lot must be non-empty arrays of equal length")
                diagnosis = validity_harness.lot_independence_check(
                    y=lot["y"], x=lot["x"], lot=lot["lot"])
                statuses.append("lot diagnosis: " + json.dumps(diagnosis, ensure_ascii=False))
            except (ValueError, TypeError, ImportError) as e:
                statuses.append("lot diagnosis: not run: %s" % e)
        verdict = run_engine_judge(claim_id, data_location=data_location, fetched_at=fetched_at)
        footer = build_judge_footer(verdict=verdict,
                                    engine_available=verdict.get("engine_available", False),
                                    order_record=order_record)
        return True, "judge: requested\n" + "\n".join(statuses) + "\n" + footer
    except (OSError, ValueError, TypeError, KeyError) as e:
        return False, "judge: invalid request (%s)" % e


def run_engine_judge(config_id: str, *, data_location: str | None = None,
                     fetched_at: str | None = None, root: str | None = None,
                     python_exe: str | None = None, timeout: int = 120) -> dict:
    """Run the frozen engine's `judge` CLI for a registered claim id and return its verdict JSON.

    This READS (A): it shells `python -m validity judge --claim configs/<id>.json --root . --out
    results/tool_runs [--lock lock.json]` with the engine checkout as cwd, exactly as the skill
    prescribes. It changes nothing in (A). On any failure it returns a dict with engine_available
    False and a reason, so the caller can surface WHY on the final answer rather than omit it.
    """
    root = root or ENGINE_ROOT
    if not os.path.isdir(root):
        return {"engine_available": False,
                "unavailable_reason": f"engine root not found: {root}"}
    cfg = os.path.join(root, "configs", f"{config_id}.json")
    if not os.path.isfile(cfg):
        return {"engine_available": False,
                "unavailable_reason": f"registered claim not found: configs/{config_id}.json"}
    if data_location is not None:
        # The frozen engine obtains fetch time from its manifest, not from a CLI option.
        # Never run against a different dataset or silently substitute another fetch time.
        try:
            with open(os.path.join(root, "data", "manifest.json"), encoding="utf-8-sig") as f:
                manifest = json.load(f)
            with open(cfg, encoding="utf-8-sig") as f:
                claim = json.load(f)
            entries = [manifest.get("datasets", {}).get(claim.get("dataset_id"))]
            matches = [e for e in entries if isinstance(e, dict)
                       and os.path.normcase(os.path.abspath(os.path.join(root, e.get("path", ""))))
                       == os.path.normcase(os.path.abspath(data_location))
                       and e.get("fetched_at") == fetched_at]
            if not matches:
                return {"engine_available": False, "unavailable_reason":
                        "requested data and fetch time are not recorded in the engine manifest"}
        except (OSError, ValueError, TypeError, AttributeError) as e:
            return {"engine_available": False, "unavailable_reason": f"manifest unavailable: {e}"}
    py = python_exe or sys.executable
    args = [py, "-m", "validity", "judge", "--claim", f"configs/{config_id}.json",
            "--root", ".", "--out", "results/tool_runs"]
    if data_location is not None:
        args += ["--data", data_location]
    if os.path.isfile(os.path.join(root, "lock.json")):
        args += ["--lock", "lock.json"]
    try:
        # Unattended by construction: the engine's `judge` CLI reads files and prints JSON; it
        # never prompts (no console I/O). So we ask for headless creationflags BY NAME -- without
        # them a parent that has no console (wscript/WPF/scheduled task) would make Windows hand
        # the child a brand-new console that Windows Terminal then shows. See
        # tools.childproc.headless_creationflags (its docstring: not safe for anything that prompts).
        from tools.childproc import headless_creationflags
        p = subprocess.run(args, cwd=root, capture_output=True, text=True,
                           encoding="utf-8", timeout=timeout,
                           creationflags=headless_creationflags())
    except subprocess.TimeoutExpired:
        return {"engine_available": False,
                "unavailable_reason": f"engine timed out after {timeout}s"}
    except OSError as e:
        return {"engine_available": False, "unavailable_reason": f"engine exec failed: {e}"}
    last = ""
    for line in (p.stdout or "").splitlines():
        if line.strip():
            last = line.strip()
    try:
        verdict = json.loads(last)
    except (ValueError, TypeError):
        return {"engine_available": False,
                "unavailable_reason": "engine produced no JSON verdict on stdout",
                "stderr_tail": (p.stderr or "")[-400:]}
    verdict["engine_available"] = True
    return verdict


def _fmt_interval(ev: dict) -> str:
    for k in ("ci_std", "ci", "interval"):
        if k in ev and ev[k] is not None:
            return f"{k}={ev[k]}"
    return "区間=未返却"


def _fmt_n(ev: dict) -> str:
    parts = []
    for k in ("K", "df", "n", "loo_n"):
        if k in ev and ev[k] is not None:
            parts.append(f"{k}={ev[k]}")
    return ", ".join(parts) if parts else "n=未返却"


def build_judge_footer(*, verdict: dict | None, engine_available: bool,
                       unavailable_reason: str = "", order_record: dict | None = None) -> str:
    """Compose the block appended to a data-analysis final answer.

    When the engine ran: surfaces id / verdict (+ja) / finding / claim_supported / reason codes /
    notes / interval / n / input hash -- the fields the skill requires, verbatim from the engine.
    When it did NOT run: states that the judge did NOT run and WHY (never silently omitted).
    Language is bounded by the skill's (e): a verdict is 'judgeable', never 'proven'/'correct'.
    """
    lines = ["", "--- 判定器(少標本評価 validity_judge)の結果 ---"]
    if order_record is not None:
        lines.append(
            "登録→ロックの順序: order_ok=%s (registered_at=%s, data_fetched_at=%s)" % (
                order_record.get("order_ok"), order_record.get("registered_at"),
                order_record.get("data_fetched_at")))
    if not engine_available or not verdict:
        reason = unavailable_reason or (
            verdict.get("unavailable_reason") if isinstance(verdict, dict) else "") or "理由不明"
        lines.append("判定器は実行されませんでした(judge NOT run)。理由: " + str(reason))
        lines.append("※ この解析は判定器を通していません。品質判断に用いる前に人が判断器を通してください。")
        lines.append("--- ここまで ---")
        return "\n".join(lines)
    ev = verdict.get("evidence") or {}
    lines.append("ID: %s" % verdict.get("id"))
    lines.append("判定: %s(%s)  結果: %s  主張の支持: %s" % (
        verdict.get("verdict"), verdict.get("verdict_ja"),
        verdict.get("finding"), verdict.get("claim_supported")))
    reasons = verdict.get("reasons") or []
    notes = verdict.get("notes") or []
    lines.append("理由コード: %s" % (", ".join(map(str, reasons)) if reasons else "(なし)"))
    lines.append("付記: %s" % (", ".join(map(str, notes)) if notes else "(なし)"))
    lines.append("数値: %s, %s" % (_fmt_n(ev), _fmt_interval(ev)))
    hashes = verdict.get("hashes") or {}
    if hashes.get("input_sha256"):
        lines.append("input_sha256: %s" % hashes.get("input_sha256"))
    lines.append("(事前登録した規則に従って機械的に判定。「採用=正しい」ではない。)")
    lines.append("--- ここまで ---")
    return "\n".join(lines)


# --- the final-step GATE used by the relay loop ------------------------------
#
# WHY THIS LIVES HERE (and is a pure function). The relay loop (copilot_autopilot_relay.py)
# reaches a candidate DONE and, for a data-analysis goal, must confirm the final answer passed
# the judge BEFORE it accepts the DONE -- the owner task. Measured 2026-10-30: the 679かすれ
# analysis was delivered three times with no judge block and nothing caught it. The relay code
# is not unit-tested in isolation; this seam is, so the decision lives in a tested pure function
# and the loop only calls it.

#: Markers that show the final answer already carries a validity-judge result (either a real
#: verdict block from build_judge_footer, or an explicit statement of WHY the judge could not
#: run). Matched case-sensitively against the delivered text.
_JUDGE_BLOCK_MARKERS = (
    "判定器(少標本評価 validity_judge)の結果",   # build_judge_footer header
    "validity_judge",
    "判定器は実行されませんでした",                # the unavailable-reason line
    "judge NOT run",
)

#: Reason codes / verdict tokens that, when present, show a real engine verdict was surfaced.
_VERDICT_TOKENS = ("GO", "HOLD", "NA")


def judge_block_present(final_response) -> bool:
    """True when the delivered answer already carries a judge result OR a stated reason it could
    not run. Either one satisfies the gate: the owner forbids SILENT omission, not an honest
    'the judge could not run because ...'."""
    if not isinstance(final_response, str) or not final_response.strip():
        return False
    return any(m in final_response for m in _JUDGE_BLOCK_MARKERS)


def needs_judge_rework(goal, final_response) -> bool:
    """True when `goal` is data analysis AND its final answer carries no judge block/reason.

    This is the single predicate the relay loop consults at candidate-DONE time. It is False for
    non-analysis goals (so nothing changes for them) and False once the answer carries the block
    or an explicit 'could not run because ...'.
    """
    if not is_data_analysis_goal(goal):
        return False
    return not judge_block_present(final_response)


#: Job text sent back to the implementer when a data-analysis answer is missing the judge block.
#: It asks for the three steps and -- per the skill -- forbids inventing a verdict: if the judge
#: cannot run, the answer must SAY WHY, not omit it.
REWORK_JOB = (
    "このゴールはDB照会(ADAMS/MCL等)の結果を表・相関・群間比較にするデータ解析の仕事です。"
    "最終回答に、少標本評価の判定器(validity_judge)の結果が載っていません。次の工程を通し、"
    "結果を最終回答に追記してください:\n"
    "(i) 解析結果とデータのsha256・取得時刻を記録 (tools/validity_harness.record_manifest)\n"
    "(ii) 主張を登録してロックし、登録→取得の時刻順を記録 (tools/validity_harness.register_and_lock)\n"
    "(iii) 判定エンジンにかけ、判定・理由コード・区間・n を最終回答に載せる\n"
    "判定器が使えない場合(未登録ID・エンジン不在・タイムアウト等)は、その理由を最終回答に明記し、"
    "黙って省略しないこと。規則・閾値・既存の登録主張は変えないこと。判定を自作・捏造しないこと。"
)


def rework_job_text(goal="", final_response="") -> str:
    """The job handed back to the implementer to make it pass the judge. Constant text; the
    arguments are accepted so a caller can log what triggered it."""
    return REWORK_JOB
