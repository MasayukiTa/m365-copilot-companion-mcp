"""Pure parsing/formatting helpers for the /review and /security-review chat slash commands.

No I/O, no subprocess, no Playwright anywhere in this module -- everything here is a plain
string/dict transform, unit-testable without a running bridge or fleet. The impure
orchestration (launching bench/review_run.py as a subprocess, reading its stdout, loading the
.json report it writes) lives in bridge/copilot_bridge.py's Handler._review_stream, which calls
these functions and does nothing else non-trivial itself.
"""
from __future__ import annotations

import os

_SECURITY_TOKENS = ("security-review", "securityreview", "security")
# Public names describe the behavior. The old -2 spellings remain accepted as hidden
# compatibility aliases so existing scripts do not break, but they are never advertised.
_P2C_REVIEW_TOKENS = ("deep-review", "deepreview", "review-2", "review2")
_P2C_SECURITY_TOKENS = (
    "deep-security-review", "deepsecurityreview",
    "security-review-2", "securityreview-2", "securityreview2",
)


def parse_review_command(cmd):
    """Parse the raw slash-command text (e.g. "/review diff", "/security-review src/foo",
    "/review") into {"kind": "review"|"security", "mode": "all"|"diff",
    "target_path": str|None}.

    Grammar:
      - the leading token selects kind: "security-review" / "securityreview" (with or
        without the leading slash) -> "security"; anything else (including "review" itself,
        or a garbled/unrecognized token) -> "review".
      - of the remaining text: if its first word (case-insensitive) is "diff" ->
        mode="diff", target_path=None (any text after "diff" is ignored); elif the
        remaining text is non-empty -> mode="all", target_path=<remaining text, stripped>;
        else -> mode="all", target_path=None.

    Pure and total: this never raises. Empty input, a missing leading slash, or unexpected
    tokens all fall back to a sane default (kind="review", mode="all", target_path=None)
    rather than propagating an exception into the caller's SSE stream.
    """
    try:
        text = (cmd or "").strip()
        if not text:
            return {"kind": "review", "mode": "all", "target_path": None}

        parts = text.split(None, 1)
        head = parts[0].lstrip("/").lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        p2c = head in _P2C_REVIEW_TOKENS or head in _P2C_SECURITY_TOKENS
        kind = "security" if head in (_SECURITY_TOKENS + _P2C_SECURITY_TOKENS) else "review"

        def result(mode, target_path):
            out = {"kind": kind, "mode": mode, "target_path": target_path}
            if p2c:
                out["resilience"] = True
            return out

        if not rest:
            return result("all", None)

        first_word = rest.split(None, 1)[0].lower()
        if first_word == "diff":
            return result("diff", None)

        return result("all", rest)
    except Exception:
        return {"kind": "review", "mode": "all", "target_path": None}


def build_review_argv(parsed, repo_root, venvpy):
    """Build the argv list to launch bench/review_run.py for a parsed /review command.

    Pure list assembly: repo_root/venvpy are supplied by the caller (which knows the real
    filesystem paths); this only joins repo_root with the fixed bench/review_run.py location
    and appends --target-path when parsed carries one. max-concurrent/effort are left at
    review_run.py's own defaults (4 / "auto") -- not passed here.
    """
    script = os.path.join(repo_root, "bench", "review_run.py")
    argv = [
        venvpy, script,
        "--kind", parsed.get("kind", "review"),
        "--mode", parsed.get("mode", "all"),
    ]
    target = parsed.get("target_path")
    if target:
        argv += ["--target-path", target]
    if parsed.get("resilience"):
        argv += ["--resilience-profile", parsed.get("kind", "review")]
    return argv


def parse_run_output(stdout):
    """Scan bench/review_run.py's captured stdout for its two parseable summary lines:
      report: <abs .md path>
      summary: high=H medium=M low=L parse_errors=P

    Returns {"report_md": path|None, "counts": {"high","medium","low","parse_errors"}|None}.
    Tolerant of missing or malformed lines -- never raises; a missing/unparseable line just
    leaves the corresponding key None instead of raising.
    """
    report_md = None
    counts = None
    try:
        for line in (stdout or "").splitlines():
            line = line.strip()
            if line.startswith("report:"):
                candidate = line[len("report:"):].strip()
                report_md = candidate or report_md
            elif line.startswith("summary:"):
                body = line[len("summary:"):].strip()
                parsed_counts = {}
                for tok in body.split():
                    if "=" not in tok:
                        continue
                    k, _, v = tok.partition("=")
                    try:
                        parsed_counts[k.strip()] = int(v.strip())
                    except Exception:
                        continue
                if parsed_counts:
                    counts = parsed_counts
    except Exception:
        pass
    return {"report_md": report_md, "counts": counts}


_KIND_LABEL = {"review": "review", "security": "security"}


def _format_finding_line(severity, item):
    try:
        file_ = item.get("file", "?")
        line_ = item.get("line")
        line_s = str(line_) if line_ is not None else "?"
        title = item.get("title", "")
        return "- [%s] %s:%s — %s" % (severity, file_, line_s, title)
    except Exception:
        return "- [%s] ?:? — ?" % (severity,)


def parse_review_fix_command(cmd):
    """Parse the raw slash-command text for /review-fix (e.g. "/review-fix",
    "/review-fix confirm", "/review-fix high", "/review-fix verified",
    "/review-fix confirm high verified") into
    {"confirm": bool, "min_severity": "medium"|"high", "verified_only": bool}.

    Grammar:
      - tokens[0] is the command word itself ("/review-fix" or "review-fix") and is
        dropped.
      - of the remaining tokens: if the FIRST one (case-insensitive) is "confirm" ->
        confirm=True and it is consumed too; otherwise confirm=False and nothing is
        consumed.
      - whatever tokens remain after that are scanned (case-insensitive, any order) for
        "high" -> min_severity="high" (else "medium"), and "verified" ->
        verified_only=True (else False). Unrecognized tokens are silently ignored.

    Pure and total: this never raises. Empty/whitespace-only/None input all fall back to
    the sane default {"confirm": False, "min_severity": "medium", "verified_only": False}.
    """
    try:
        text = (cmd or "").strip()
        if not text:
            return {"confirm": False, "min_severity": "medium", "verified_only": False}

        tokens = text.split()
        args = tokens[1:] if len(tokens) > 1 else []

        confirm = False
        if args and args[0].lower() == "confirm":
            confirm = True
            args = args[1:]

        lowered = [a.lower() for a in args]
        min_severity = "high" if "high" in lowered else "medium"
        verified_only = "verified" in lowered

        return {"confirm": confirm, "min_severity": min_severity, "verified_only": verified_only}
    except Exception:
        return {"confirm": False, "min_severity": "medium", "verified_only": False}


def build_review_fix_argv(parsed, repo_root, venvpy, dry_run):
    """Build the argv list to launch bench/review_fix.py for a parsed /review-fix command.

    Pure list assembly, mirroring build_review_argv: repo_root/venvpy are supplied by the
    caller; this only joins repo_root with the fixed bench/review_fix.py location and
    appends --dry-run / --min-severity / --verified-only. max-concurrent/group-size/effort/
    branch/skip-tests/out-dir are left at review_fix.py's own defaults -- not passed here.
    """
    script = os.path.join(repo_root, "bench", "review_fix.py")
    argv = [venvpy, script]
    if dry_run:
        argv.append("--dry-run")
    argv += ["--min-severity", parsed.get("min_severity", "medium")]
    if parsed.get("verified_only"):
        argv.append("--verified-only")
    return argv


def parse_fix_run_output(stdout):
    """Scan bench/review_fix.py's captured stdout from a REAL (non-dry-run) invocation for
    its final structured summary lines:
      fix report: <path>
      applied=<N> skipped=<M> test_gate=<result>
      backup: <dir>
      undo: <bat> (or `<cmd>`)
      branch: <name>              (only present when a git bonus branch was created)
      TEST GATE FAILED -- ...     (only present when the post-fix test gate failed)

    Returns {"fix_report_md", "applied", "skipped", "test_gate", "backup_dir", "undo_line",
    "branch", "test_gate_failed"}. Any field whose line is absent/unparseable stays at its
    default (None / False). Tolerant of missing or malformed lines -- never raises.
    """
    info = {
        "fix_report_md": None, "applied": None, "skipped": None, "test_gate": None,
        "backup_dir": None, "undo_line": None, "branch": None, "test_gate_failed": False,
    }
    try:
        for raw in (stdout or "").splitlines():
            line = raw.strip()
            low = line.lower()
            if low.startswith("fix report:"):
                val = line[len("fix report:"):].strip()
                info["fix_report_md"] = val or info["fix_report_md"]
            elif low.startswith("applied="):
                for tok in line.split():
                    if "=" not in tok:
                        continue
                    k, _, v = tok.partition("=")
                    if k in ("applied", "skipped"):
                        try:
                            info[k] = int(v)
                        except Exception:
                            pass
                    elif k == "test_gate":
                        info["test_gate"] = v
            elif low.startswith("backup:"):
                val = line[len("backup:"):].strip()
                info["backup_dir"] = val or info["backup_dir"]
            elif low.startswith("undo:"):
                val = line[len("undo:"):].strip()
                info["undo_line"] = val or info["undo_line"]
            elif low.startswith("branch:"):
                val = line[len("branch:"):].strip()
                info["branch"] = val or info["branch"]
            elif low.startswith("test gate failed"):
                info["test_gate_failed"] = True
    except Exception:
        pass
    return info


def format_fix_summary(info):
    """Build the final Japanese chat message for a completed (non-dry-run) /review-fix
    confirm run, from parse_fix_run_output's dict.

    Always includes the applied/skipped/test_gate counts (defaulting to 0/"unknown" when
    the fleet status could not be parsed), then the backup location and the exact undo
    instruction so the user can revert with a single double-click even if they never touch
    a terminal, then (optionally) the git branch, a test-gate-failure warning, and a
    pointer to the full fix report. Never raises.
    """
    try:
        info = info or {}
        applied = info.get("applied") if info.get("applied") is not None else 0
        skipped = info.get("skipped") if info.get("skipped") is not None else 0
        test_gate = info.get("test_gate") or "unknown"

        lines = ["修正完了", "applied=%s skipped=%s test_gate=%s" % (applied, skipped, test_gate)]

        if info.get("backup_dir"):
            lines.append("バックアップ: %s" % info["backup_dir"])
        if info.get("undo_line"):
            lines.append("元に戻すには: %s" % info["undo_line"])
        if info.get("branch"):
            lines.append("git ブランチ: %s" % info["branch"])
        if info.get("test_gate_failed"):
            lines.append("⚠ テストゲート失敗 -- 自動では戻していません。上記の undo で手動ロールバックできます。")
        if info.get("fix_report_md"):
            lines.append("詳細レポート: %s" % info["fix_report_md"])

        return "\n".join(lines)
    except Exception:
        return "修正完了"


def format_review_summary(kind, counts, agg_json, report_md, max_high=20, max_medium=10):
    """Build a compact Japanese chat message summarizing a finished /review or
    /security-review run: a header, the high/medium/low/parse_errors counts line, then
    inline high findings (up to max_high) and medium findings (up to max_medium) pulled
    from agg_json's by_severity lists, then a pointer to the full report path.

    Never raises: missing/malformed counts or agg_json just shrink what gets shown -- if
    agg_json is None (report .json could not be loaded), the message falls back to just the
    counts line + report path.
    """
    try:
        label = _KIND_LABEL.get(kind, str(kind))
        lines = ["レビュー完了 (%s)" % label]

        counts = counts or {}
        lines.append("high=%s medium=%s low=%s parse_errors=%s" % (
            counts.get("high", 0), counts.get("medium", 0),
            counts.get("low", 0), counts.get("parse_errors", 0),
        ))

        if agg_json is not None:
            try:
                by_sev = agg_json.get("by_severity", {}) or {}
                for item in (by_sev.get("high", []) or [])[:max_high]:
                    lines.append(_format_finding_line("high", item))
                for item in (by_sev.get("medium", []) or [])[:max_medium]:
                    lines.append(_format_finding_line("medium", item))
            except Exception:
                pass

        if report_md:
            lines.append("詳細レポート: %s (.json も同ディレクトリ)" % report_md)

        return "\n".join(lines)
    except Exception:
        return "レビュー完了 (%s)" % (kind,)
