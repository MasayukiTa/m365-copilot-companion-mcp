"""Aggregate a completed fleet review run's status.json into a single findings report.

The refuter's verdict never lands in a worker's transcript -- it only appears in the final
status.json snapshot as workers[].reason == "refuter#N: UPHELD|REFUTED|...". So a worker's
actual FINDINGS come from its own final assistant text (its full transcript file, falling
back to the status.json snapshot's display_result/last only if the transcript is missing --
see worker_final_text for why the transcript must win), and the refuter reason is attached
alongside each finding as extra context, not as the source of the finding itself.

This module is pure I/O-in-pure-out aside from reading the two input files given by the
caller; nothing here calls a network, subprocess, or wall clock on its own (see `now=` on
aggregate() -- callers stamp the timestamp, not this module).

  python -m bench.review_aggregate --status-json .fleet/review/status.json \\
      --transcripts-dir .fleet/review/transcripts --out-md report.md --out-json report.json
"""
from __future__ import annotations

import argparse
import json
import os
import re

from bench.review_build_goals import FINDINGS_BEGIN, FINDINGS_END

_FINDINGS_RE = re.compile(
    re.escape(FINDINGS_BEGIN) + r"\s*(.*?)\s*" + re.escape(FINDINGS_END), re.DOTALL
)

_SEVERITIES = ("high", "medium", "low")


def _sanitize_findings(data):
    """Drop any non-dict entries from a parsed findings array (real agent output
    occasionally mixes in stray scalars)."""
    return [item for item in data if isinstance(item, dict)]


def _looks_like_findings(data):
    """Heuristic used only by the last-resort recovery layer (no delimiters at all):
    accept a bare JSON array only if it is a non-empty list of dicts and at least one
    dict has a "file" or "title" key -- otherwise it's probably an unrelated array that
    happens to appear in the agent's prose, not a findings block."""
    if not isinstance(data, list) or not data:
        return False
    if not all(isinstance(item, dict) for item in data):
        return False
    return any(("file" in item or "title" in item) for item in data)


def _extract_last_json_array(text, before_index=None):
    """Scan backward for the last top-level JSON array in text[:before_index] (or the
    whole text if before_index is None) and return its parsed value, or None if no
    substring ending at a ']' parses as a JSON list.

    Bounded, pure, never raises: tries candidates starting at each '[' before the last
    ']' in the region, nearest first (smallest span first, growing backward), capped at
    a few hundred attempts so pathological input can't make this O(n^2)-blow-up in
    practice on real transcript sizes."""
    region = text if before_index is None else text[:before_index]
    close = region.rfind("]")
    if close == -1:
        return None
    starts = [i for i, ch in enumerate(region[: close + 1]) if ch == "["]
    if not starts:
        return None
    for start in reversed(starts[-300:]):
        candidate = region[start : close + 1]
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, list):
            return data
    return None


def parse_findings_block(text):
    """Extract the FINDINGS JSON array from a worker's final text.

    Real M365 Copilot workers reliably emit the closing <<<END_FINDINGS>>> marker (and
    the JSON array right before it), but frequently drop or mangle the opening
    <<<FINDINGS>>> marker. So this uses layered recovery, in order, and the first layer
    that yields a valid JSON array wins:

      (a) PREFERRED: text between FINDINGS_BEGIN and FINDINGS_END (original behavior).
      (b) FALLBACK: FINDINGS_END is present but (a) didn't yield a list -- scan backward
          from FINDINGS_END for the nearest balanced JSON array immediately preceding it.
      (c) LAST RESORT: no FINDINGS_END anywhere -- scan the whole text for the last JSON
          array and accept it only if it looks like a findings list (see
          _looks_like_findings).

    Returns (findings: list[dict], parse_error: bool). Never raises: if every layer
    fails to recover a findings array, that's parse_error=True with findings=[]. Any
    recovered array has its non-dict entries dropped (see _sanitize_findings)."""
    if not text:
        return [], True

    m = _FINDINGS_RE.search(text)
    if m:
        try:
            data = json.loads(m.group(1))
        except Exception:
            data = None
        if isinstance(data, list):
            return _sanitize_findings(data), False

    end_idx = text.rfind(FINDINGS_END)
    if end_idx != -1:
        data = _extract_last_json_array(text, before_index=end_idx)
        if data is not None:
            return _sanitize_findings(data), False
        return [], True

    data = _extract_last_json_array(text, before_index=None)
    if data is not None and _looks_like_findings(data):
        return _sanitize_findings(data), False

    return [], True


def _resolve_transcript_path(transcript_field, transcripts_dir):
    if not transcript_field:
        return ""
    if os.path.isabs(transcript_field) or os.path.exists(transcript_field):
        return transcript_field
    return os.path.join(transcripts_dir or "", transcript_field)


def load_transcript_final_answer(path):
    """Return the text of the LAST role=="assistant" line in a fleet worker transcript
    jsonl (see relay/relay_fleet.py:_Transcript -- line1 is {"meta": true, ...}, then
    {"turn","role","text","ts"} per turn). Returns "" on any error (missing file, bad
    json lines, no assistant turn at all) -- never raises."""
    if not path:
        return ""
    last = ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("role") == "assistant":
                    last = d.get("text", "") or ""
    except Exception:
        return ""
    return last


def load_transcript_all_assistant(path):
    """Return the text of EVERY role=="assistant" turn in a fleet worker transcript
    jsonl, concatenated in order and joined by "\\n". Returns "" on any error (missing
    file, bad json lines, no assistant turn at all) -- never raises.

    Unlike load_transcript_final_answer (which keeps only the LAST assistant turn),
    this recovers workers whose FINDINGS block was emitted in an EARLIER assistant turn
    and who then continued with wrap-up prose in later turns -- the last-turn-only read
    silently drops that block."""
    if not path:
        return ""
    turns = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("role") == "assistant":
                    turns.append(d.get("text", "") or "")
    except Exception:
        return ""
    return "\n".join(turns)


def _worker_findings(worker, transcripts_dir):
    """Layered per-worker findings extraction: first attempt that yields a non-error,
    non-empty findings list wins.

      (a) fast path: parse_findings_block(worker_final_text(...)) -- the current
          last-turn/status text. Keeps existing behavior for workers whose block is in
          their last assistant turn (or in the status.json snapshot text).
      (b) recovery: if (a) is a parse_error or empty AND the worker has a transcript,
          parse_findings_block(load_transcript_all_assistant(transcript)) -- the FULL
          concatenation of all assistant turns. This recovers workers whose block sits
          in an earlier turn, with only wrap-up prose in the last turn.

    Returns (findings: list[dict], parse_error: bool). Extracts ONE findings list per
    worker (the best single parse), never one per turn -- if multiple turns each
    contain a findings-shaped array, parse_findings_block's own last-resort rule
    already picks one; this function doesn't merge or double-count across layers."""
    text = worker_final_text(worker, transcripts_dir)
    findings, perr = parse_findings_block(text)
    if findings and not perr:
        return findings, perr

    transcript_field = worker.get("transcript", "") if isinstance(worker, dict) else ""
    path = _resolve_transcript_path(transcript_field, transcripts_dir)
    if not path:
        return findings, perr

    full_text = load_transcript_all_assistant(path)
    if not full_text:
        return findings, perr

    full_findings, full_perr = parse_findings_block(full_text)
    if full_findings and not full_perr:
        return full_findings, full_perr
    return findings, perr


def worker_final_text(worker, transcripts_dir):
    """Best-effort recovery of a worker's final assistant text.

    Prefer the FULL transcript file (load_transcript_final_answer's untruncated last
    assistant turn) over the status.json snapshot's own display_result/last fields.
    Those status fields are truncated snapshots (~600 chars in practice) taken while the
    worker was still running; the FINDINGS block sits at the very end of the worker's
    real answer, so truncation routinely cuts it off. The transcript file has the
    complete answer, so it must win whenever it resolves to something non-empty. Only
    fall back to display_result/last when the transcript is missing, unreadable, or has
    no assistant turn at all. Never raises.

    worker["transcript"] may be a path relative to transcripts_dir, or (as real
    status.json snapshots store it) an absolute path -- _resolve_transcript_path handles
    both."""
    try:
        path = _resolve_transcript_path(worker.get("transcript", ""), transcripts_dir)
        if path:
            text = load_transcript_final_answer(path)
            if text:
                return text
        return worker.get("display_result") or worker.get("last") or ""
    except Exception:
        return ""


def aggregate(status_path, transcripts_dir, now=None):
    """Read a fleet run's final status.json and build a flattened findings report.

    Returns a dict:
      {"generated_at": now, "workers_total": N, "parse_errors": count,
       "findings": [ {..each finding, plus "worker","worker_goal","worker_outcome",
                       "reason","verified"} ],
       "by_severity": {"high": [...], "medium": [...], "low": [...]}}

    Never raises: a missing/corrupt status.json yields a valid dict of the same shape
    plus an "error" field, rather than propagating the exception. `now` is accepted
    (not read from the wall clock here) so this stays deterministically testable --
    callers (main()) pass time.time() themselves."""
    by_severity = {sev: [] for sev in _SEVERITIES}
    base = {
        "generated_at": now,
        "workers_total": 0,
        "parse_errors": 0,
        "findings": [],
        "by_severity": by_severity,
    }
    try:
        with open(status_path, encoding="utf-8") as f:
            status = json.load(f)
    except Exception as e:
        base["error"] = "could not read status.json: %s" % (e,)
        return base

    workers = status.get("workers", []) if isinstance(status, dict) else []
    if not isinstance(workers, list):
        workers = []
    base["workers_total"] = len(workers)

    parse_errors = 0
    flattened = []
    for w in workers:
        if not isinstance(w, dict):
            continue
        try:
            findings, perr = _worker_findings(w, transcripts_dir)
        except Exception:
            findings, perr = [], True
        if perr:
            parse_errors += 1
        name = w.get("name", "")
        goal = w.get("goal", "")
        outcome = w.get("outcome", "")
        reason = w.get("reason", "")
        verified = w.get("verified")
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            entry = dict(finding)
            entry["worker"] = name
            entry["worker_goal"] = goal
            entry["worker_outcome"] = outcome
            entry["reason"] = reason
            entry["verified"] = verified
            flattened.append(entry)
            sev = str(finding.get("severity", "low")).lower()
            if sev not in _SEVERITIES:
                sev = "low"
            by_severity[sev].append(entry)

    base["parse_errors"] = parse_errors
    base["findings"] = flattened
    return base


def render_markdown(agg):
    """Render an aggregate() dict as a readable Markdown report, high -> medium -> low,
    with a header of counts and a parse-error note."""
    lines = []
    lines.append("# Review findings report")
    lines.append("")
    if agg.get("error"):
        lines.append("**ERROR:** %s" % agg["error"])
        lines.append("")
    lines.append("- workers total: %d" % agg.get("workers_total", 0))
    lines.append("- findings total: %d" % len(agg.get("findings", [])))
    lines.append("- parse errors (worker text missing/malformed FINDINGS block): %d" %
                 agg.get("parse_errors", 0))
    lines.append("")

    by_severity = agg.get("by_severity", {})
    for sev in _SEVERITIES:
        items = by_severity.get(sev, [])
        lines.append("## %s (%d)" % (sev, len(items)))
        lines.append("")
        if not items:
            lines.append("_none_")
            lines.append("")
            continue
        for it in items:
            file_ = it.get("file", "?")
            ln = it.get("line")
            ln_s = str(ln) if ln is not None else "?"
            title = it.get("title", "")
            detail = it.get("detail", "")
            worker = it.get("worker", "?")
            verified = it.get("verified")
            reason = it.get("reason", "")
            meta_bits = [worker]
            if verified is True:
                meta_bits.append("verified")
            elif verified is False:
                meta_bits.append("not verified")
            if reason:
                meta_bits.append(reason)
            meta = ", ".join(meta_bits)
            lines.append("- [%s] %s:%s — %s (%s)" % (sev, file_, ln_s, title, meta))
            if detail:
                lines.append("  %s" % detail)
        lines.append("")
    return "\n".join(lines)


def render_json(agg):
    """Return agg as a plain JSON-serializable dict (a shallow copy). Any timestamp
    stamping is the caller's responsibility (agg["generated_at"] is already set by
    aggregate()'s `now` argument if the caller passed one)."""
    return dict(agg)


def main(argv=None):
    import time

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status-json", required=True)
    ap.add_argument("--transcripts-dir", default="")
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--out-json", required=True)
    args = ap.parse_args(argv)

    agg = aggregate(args.status_json, args.transcripts_dir, now=time.time())

    for out_path in (args.out_md, args.out_json):
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    with open(args.out_md, "w", encoding="utf-8") as f:
        f.write(render_markdown(agg))
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(render_json(agg), f, ensure_ascii=False, indent=2)

    print("wrote %s and %s (%d findings, %d parse errors)" %
          (args.out_md, args.out_json, len(agg.get("findings", [])), agg.get("parse_errors", 0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
