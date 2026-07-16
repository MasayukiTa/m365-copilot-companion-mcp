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

# Matches a backslash that is NOT the start of a valid JSON escape sequence
# (\" \\ \/ \b \f \n \r \t \uXXXX). Used to repair the single most common cause of
# unparsable findings blocks in practice: M365 Copilot agents on Windows emit raw
# Windows paths (C:\Users\..., .fleet\gaia\...) and regex snippets (\d, \g) inside
# JSON string values without escaping the backslash, which is invalid JSON.
_BAD_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _loads_tolerant(s):
    """json.loads with one safe repair-and-retry fallback for invalid backslash
    escapes. Never raises: returns the parsed object, or None if parsing fails even
    after the repair.

    Fast path: try json.loads(s) unchanged first -- already-valid JSON (the common
    case) never touches the repair, so this cannot regress correct input.

    Repair (only on JSONDecodeError): double every backslash that is not already
    starting a valid JSON escape (see _BAD_ESCAPE_RE). This is the standard fix for
    "Windows path pasted into a JSON string" -- `C:\\Users\\x` (invalid, \\U and \\x
    are not JSON escapes) becomes `C:\\\\Users\\\\x` (valid, decodes back to a single
    backslash each). Legitimate escapes like \\n, \\", \\uXXXX are left untouched by
    the negative lookahead, so they still decode to newline/quote/unicode char, not a
    literal backslash. Retried exactly once; if it still fails, give up and return
    None (caller treats that the same as a JSONDecodeError today)."""
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        repaired = _BAD_ESCAPE_RE.sub(r"\\\\", s)
        return json.loads(repaired)
    except Exception:
        return None


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
        data = _loads_tolerant(candidate)
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
        data = _loads_tolerant(m.group(1))
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


# --- P1b: refuter-based verify_verdict rendering ------------------------------------------
# merge_verdicts (bench/review_run.py) tags each finding with verify_verdict in
# {"confirmed", "false_positive", "unclear"} after the adversarial refuter pass runs (None if
# that pass never ran, or never covered a given finding). "false_positive" (REFUTED by the
# independent skeptic) findings are demoted out of the normal severity sections below into
# their own "Refuted / dropped" section -- visible for transparency, but not counted in the
# headline totals; bench/review_fix.py:filter_findings already drops them from any fix run.
_VERIFY_RANK = {"confirmed": 0, "unclear": 2}  # anything else (None / missing) ranks 1

# --- P2 piece A: behavioral_verdict rendering -----------------------------------------------
# bench/review_run.py's OPT-IN --behavioral pass (behavioral_verify()) attaches
# "behavioral_verdict" (bench.review_build_goals.BEHAVIOR_VERDICTS: reproduced/not_reproduced/
# inconclusive) + "behavioral_evidence" to CONFIRMED findings it actually tried to reproduce
# via READ-ONLY run_python/shell_exec. A "reproduced" finding has gone from reasoned to
# DEMONSTRATED -- the most trustworthy signal this report can carry -- so it is marked and
# ranked ahead of everything else, including a plain "confirmed" finding the behavioral pass
# never touched. A "not_reproduced" CONFIRMED finding is flagged (not silently dropped: the
# refuter already upheld it) so a human can see the discrepancy and judge it themselves.
_BEHAVIOR_VERDICTS_COUNTED = ("reproduced", "not_reproduced", "inconclusive")


def _behavioral_counts(findings):
    """Count findings by behavioral_verdict. Returns {} (falsy) if NO finding carries a
    "behavioral_verdict" key at all -- lets callers distinguish "the --behavioral pass never
    ran" from "it ran" the same way _verify_counts distinguishes the refuter pass."""
    if not any(isinstance(f, dict) and "behavioral_verdict" in f for f in findings):
        return {}
    counts = {k: 0 for k in _BEHAVIOR_VERDICTS_COUNTED}
    for f in findings:
        if not isinstance(f, dict):
            continue
        bv = f.get("behavioral_verdict")
        if bv in counts:
            counts[bv] += 1
    return counts


def _rank_key(it):
    """Sort key for findings within one severity section (highest-trust first):

      1) behavioral_verdict == "reproduced" -- actually DEMONSTRATED by running code, not
         just reasoned about -- always sorts first, ahead of every other finding regardless
         of its verify_verdict.
      2) otherwise, the existing _VERIFY_RANK order (confirmed -> no-verdict -> unclear),
         with a "not_reproduced" behavioral_verdict pushing a finding just behind its
         verify_verdict peers (a CONFIRMED-but-not-reproduced finding is de-prioritized
         relative to a CONFIRMED finding the behavioral pass never touched or found
         inconclusive)."""
    if it.get("behavioral_verdict") == "reproduced":
        return (-1, 0)
    base = _VERIFY_RANK.get(it.get("verify_verdict"), 1)
    bump = 1 if it.get("behavioral_verdict") == "not_reproduced" else 0
    return (base, bump)


def _verify_counts(findings):
    """Count findings by verify_verdict. Returns {} (falsy) if NO finding carries a
    "verify_verdict" key at all -- lets callers distinguish "the refuter pass never ran" from
    "it ran and every finding happened to be confirmed" without a separate flag."""
    if not any(isinstance(f, dict) and "verify_verdict" in f for f in findings):
        return {}
    counts = {"confirmed": 0, "false_positive": 0, "unclear": 0}
    for f in findings:
        if not isinstance(f, dict):
            continue
        vv = f.get("verify_verdict")
        if vv in counts:
            counts[vv] += 1
    return counts


def render_markdown(agg):
    """Render an aggregate() dict as a readable Markdown report, high -> medium -> low,
    with a header of counts and a parse-error note.

    If the refuter pass (bench/review_run.py:refute_findings + merge_verdicts) has annotated
    findings with verify_verdict: REFUTED ("false_positive") findings are excluded from the
    severity sections and counts, and listed instead in a separate "Refuted / dropped" section
    at the end (with the skeptic's reason) so nothing disappears silently. Within each severity
    section, CONFIRMED findings are sorted first (unclear/no-verdict findings stay in their
    original relative order after)."""
    lines = []
    lines.append("# Review findings report")
    lines.append("")
    if agg.get("error"):
        lines.append("**ERROR:** %s" % agg["error"])
        lines.append("")
    findings_all = agg.get("findings", [])
    lines.append("- workers total: %d" % agg.get("workers_total", 0))
    lines.append("- findings total: %d" % len(findings_all))
    lines.append("- parse errors (worker text missing/malformed FINDINGS block): %d" %
                 agg.get("parse_errors", 0))
    counts = _verify_counts(findings_all)
    if counts:
        lines.append("- refuter verdicts: confirmed=%d refuted/dropped=%d unclear=%d" %
                     (counts["confirmed"], counts["false_positive"], counts["unclear"]))
    behavior_counts = _behavioral_counts(findings_all)
    if behavior_counts:
        lines.append(
            "- behavioral verification: reproduced=%d (DEMONSTRATED) not_reproduced=%d "
            "inconclusive=%d" % (behavior_counts["reproduced"], behavior_counts["not_reproduced"],
                                  behavior_counts["inconclusive"]))
    dims_covered = agg.get("dimensions_covered")
    if dims_covered:
        lines.append("- dimensions covered: %s" % ", ".join(dims_covered))

    assurance = agg.get("validation_assurance")
    if assurance:
        lines.append("- P2c full-validation verdict: **%s**" %
                     assurance.get("verdict", "INCONCLUSIVE"))
        lines.append("- active validation evidence: %d/%d required goal(s) complete" % (
            assurance.get("completed_active_goals", 0),
            assurance.get("required_active_goals", 0),
        ))
        for reason in assurance.get("reasons") or []:
            lines.append("  - incomplete: %s" % reason)
        if assurance.get("verdict") == "VERIFIED_WITHIN_SCOPE":
            lines.append("  NOTE: verified only for the recorded scope and evidence; this is "
                         "not an unconditional guarantee against every attacker or future change.")

    resilience = agg.get("resilience")
    if resilience:
        lines.append("- P2c resilience: profile=%s fresh_replays=%d content_refusals=%d "
                     "decomposed_parents=%d child_goals=%d unresolved=%d" % (
                         resilience.get("profile", "?"),
                         resilience.get("fresh_replays", 0),
                         resilience.get("content_refusals", 0),
                         resilience.get("decomposed_parents", 0),
                         resilience.get("child_goals", 0),
                         resilience.get("unresolved_refusals", 0),
                     ))
        lines.append("- P2c recovery budget: %d/%d goal(s) used" % (
            resilience.get("recovery_goals_used", 0),
            resilience.get("recovery_goal_budget", 0),
        ))
        if resilience.get("budget_truncated_children"):
            lines.append("  NOTE: %d decomposed child goal(s) were not launched because the "
                         "hard recovery-goal budget was reached." %
                         resilience.get("budget_truncated_children"))
        if resilience.get("validation_errors"):
            lines.append("- P2c decomposition validation errors: %d" %
                         len(resilience.get("validation_errors") or []))
        if resilience.get("events"):
            lines.append("- P2c recovery events:")
            for event in resilience.get("events") or []:
                if not isinstance(event, dict):
                    continue
                lines.append("  - %s: %s (depth=%s%s)" % (
                    event.get("task_id", "?"), event.get("result", "?"),
                    event.get("depth", "?"),
                    ", children=%s" % event.get("children")
                    if event.get("children") is not None else "",
                ))

    # P3 piece C: loop-until-dry + completeness-critic metadata. Purely additive -- both keys
    # are absent from `agg` unless bench.review_run.py's --loop/--completeness were actually
    # used, so a report built without P3 renders byte-identical to before P3 existed.
    loop_meta = agg.get("loop_meta")
    if loop_meta:
        lines.append("- loop: %d/%d round(s) run, stopped: %s" %
                     (loop_meta.get("rounds_run", 0), loop_meta.get("max_rounds", 0),
                      loop_meta.get("stopped_reason", "?")))
        if loop_meta.get("stopped_reason") == "max_rounds":
            lines.append("  NOTE: stopped because the max-rounds cap was reached, NOT because "
                         "findings went dry -- rounds may remain; re-run with a higher "
                         "--max-rounds to keep looking.")
        lines.append("- loop: %d unique finding(s) accumulated across all rounds" %
                     loop_meta.get("unique_findings", 0))

    gaps = agg.get("completeness_gaps")
    if gaps:
        missing_dims = gaps.get("missing_dimensions") or []
        missing_files = gaps.get("missing_files") or []
        unverified = gaps.get("unverified_claims") or []
        if missing_dims or missing_files or unverified:
            lines.append("- completeness critic:")
            if missing_dims:
                lines.append("  missing dimensions: %s" % ", ".join(missing_dims))
            if missing_files:
                lines.append("  missing files/areas: %s" % ", ".join(missing_files))
            if unverified:
                lines.append("  unverified claims:")
                for c in unverified:
                    lines.append("    - %s" % c)
        else:
            lines.append("- completeness critic: no gaps identified")

    # Baseline/regression gate (bench/review_baseline.py, wired in by bench/review_run.py's
    # --baseline). Purely additive -- absent from `agg` unless --baseline was actually used, so
    # a report built without it renders byte-identical to before this feature existed.
    baseline_diff = agg.get("baseline_diff")
    if baseline_diff:
        lines.append("- baseline diff: new=%d regressed=%d resolved=%d unchanged=%d" % (
            len(baseline_diff.get("new") or []), len(baseline_diff.get("regressed") or []),
            len(baseline_diff.get("resolved") or []), len(baseline_diff.get("unchanged") or [])))

    lines.append("")

    by_severity = agg.get("by_severity", {})
    for sev in _SEVERITIES:
        raw_items = by_severity.get(sev, [])
        items = [it for it in raw_items if it.get("verify_verdict") != "false_positive"]
        items = sorted(items, key=_rank_key)
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
            verify_verdict = it.get("verify_verdict")
            if verify_verdict == "confirmed":
                meta_bits.append("CONFIRMED")
            elif verify_verdict == "unclear":
                meta_bits.append("refute: unclear")
            if it.get("finding_state"):
                meta_bits.append("state=" + str(it.get("finding_state")))
            if it.get("adjudicator_verdict"):
                meta_bits.append("adjudicator=" + str(it.get("adjudicator_verdict")))
            behavioral_verdict = it.get("behavioral_verdict")
            if behavioral_verdict == "reproduced":
                meta_bits.append("DEMONSTRATED")
            elif behavioral_verdict == "not_reproduced":
                meta_bits.append("reasoned-but-not-reproduced")
            elif behavioral_verdict == "inconclusive":
                meta_bits.append("behavior: inconclusive")
            meta = ", ".join(meta_bits)
            lines.append("- [%s] %s:%s — %s (%s)" % (sev, file_, ln_s, title, meta))
            if detail:
                lines.append("  %s" % detail)
            behavioral_evidence = it.get("behavioral_evidence")
            if behavioral_evidence:
                lines.append("  behavior evidence: %s" % behavioral_evidence)
            if it.get("adjudicator_reason"):
                lines.append("  adjudicator: %s" % it.get("adjudicator_reason"))
        lines.append("")

    refuted = [f for f in findings_all if f.get("verify_verdict") == "false_positive"]
    if refuted:
        lines.append("## Refuted / dropped by adversarial refuter (%d)" % len(refuted))
        lines.append("")
        lines.append("_These findings were REFUTED by an independent skeptic reviewer "
                      "(relay/refuter.py) -- excluded from the counts above, and "
                      "bench/review_fix.py's filter_findings drops them automatically._")
        lines.append("")
        for it in refuted:
            file_ = it.get("file", "?")
            ln = it.get("line")
            ln_s = str(ln) if ln is not None else "?"
            sev = it.get("severity", "?")
            title = it.get("title", "")
            reason = it.get("verify_reason", "")
            lines.append("- [%s] %s:%s — %s" % (sev, file_, ln_s, title))
            if reason:
                lines.append("  refuter: %s" % reason)
        lines.append("")

    if baseline_diff:
        new_items = baseline_diff.get("new") or []
        regressed_items = baseline_diff.get("regressed") or []
        if new_items or regressed_items:
            lines.append("## Baseline diff: new / regressed findings (%d)" %
                         (len(new_items) + len(regressed_items)))
            lines.append("")
            lines.append("_These are the actionable findings from the baseline gate -- NEW "
                          "findings never seen in the baseline, and REGRESSED findings the "
                          "baseline already knew about that are freshly reconfirmed (or have "
                          "no refute verdict this run). RESOLVED/UNCHANGED findings are not "
                          "listed here (see the counts line above)._")
            lines.append("")
            if new_items:
                lines.append("### New (%d)" % len(new_items))
                for it in new_items:
                    ln = it.get("line")
                    lines.append("- %s:%s:%s:%s" % (
                        it.get("file", "?"), ln if ln is not None else "?",
                        it.get("title", ""), it.get("severity", "?")))
                lines.append("")
            if regressed_items:
                lines.append("### Regressed (%d)" % len(regressed_items))
                for it in regressed_items:
                    ln = it.get("line")
                    lines.append("- %s:%s:%s:%s" % (
                        it.get("file", "?"), ln if ln is not None else "?",
                        it.get("title", ""), it.get("severity", "?")))
                lines.append("")

    return "\n".join(lines)


def render_json(agg):
    """Return agg as a plain JSON-serializable dict (a shallow copy), plus a "verify_summary"
    counts dict (see _verify_counts) IF at least one finding carries a verify_verdict, and a
    "behavioral_summary" counts dict (see _behavioral_counts) IF at least one finding carries
    a behavioral_verdict -- otherwise the output is an exact shallow copy of agg (no key
    added), matching the old "plain dict copy" contract for reports built without the
    refuter/behavioral passes. Any timestamp stamping is the caller's responsibility
    (agg["generated_at"] is already set by aggregate()'s `now` argument if the caller passed
    one)."""
    out = dict(agg)
    findings = out.get("findings", [])
    counts = _verify_counts(findings)
    if counts:
        out["verify_summary"] = counts
    behavior_counts = _behavioral_counts(findings)
    if behavior_counts:
        out["behavioral_summary"] = behavior_counts
    return out


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
