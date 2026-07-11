"""Aggregate a completed fleet review run's status.json into a single findings report.

The refuter's verdict never lands in a worker's transcript -- it only appears in the final
status.json snapshot as workers[].reason == "refuter#N: UPHELD|REFUTED|...". So a worker's
actual FINDINGS come from its own final assistant text (display_result/last, falling back to
its transcript file), and the refuter reason is attached alongside each finding as extra
context, not as the source of the finding itself.

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


def parse_findings_block(text):
    """Extract the FINDINGS JSON array from a worker's final text.

    Returns (findings: list[dict], parse_error: bool). Never raises: a missing delimiter
    pair, malformed JSON inside it, or a JSON value that isn't a list all count as
    parse_error=True with findings=[]. Text after FINDINGS_END (e.g. trailing "DONE" or
    stray prose) does not affect parsing -- the regex is non-greedy and only cares about
    the first well-formed <<<FINDINGS>>>...<<<END_FINDINGS>>> span."""
    if not text:
        return [], True
    m = _FINDINGS_RE.search(text)
    if not m:
        return [], True
    try:
        data = json.loads(m.group(1))
    except Exception:
        return [], True
    if not isinstance(data, list):
        return [], True
    return data, False


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


def worker_final_text(worker, transcripts_dir):
    """Best-effort recovery of a worker's final assistant text: prefer the status.json
    snapshot's own display_result/last (cheap, already in memory); fall back to reading
    its transcript file only if both are empty. Never raises."""
    try:
        text = worker.get("display_result") or worker.get("last") or ""
        if text:
            return text
        path = _resolve_transcript_path(worker.get("transcript", ""), transcripts_dir)
        return load_transcript_final_answer(path)
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
            text = worker_final_text(w, transcripts_dir)
            findings, perr = parse_findings_block(text)
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
