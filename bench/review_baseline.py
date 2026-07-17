"""Baseline / regression gate for the review harness.

A "baseline" is a small JSON snapshot of previously-accepted findings (see save_baseline's
schema below). A later run can diff its own findings against that snapshot
(diff_against_baseline) to separate "we already knew about this" from "this is new, or a
previously-known issue that just got reconfirmed" -- and, optionally, gate CI on the latter
(should_gate).

Pure functions plus minimal file I/O only -- no fleet, no network, no wall-clock reads (any
timestamp is caller-supplied, see save_baseline's `generated_at`).

  from bench.review_baseline import (
      dedupe_key, load_baseline, save_baseline, diff_against_baseline, should_gate,
  )
"""
from __future__ import annotations

import json
import os
import posixpath


def dedupe_key(finding):
    """Stable dedupe key for one finding: (normpath(file), line, title.strip().lower()).

    Deliberately duplicated from bench.review_run._dedupe_key rather than imported -- both
    are a trivial 4-line pure function, and importing bench.review_run here would create a
    circular import (review_run imports this module to wire the baseline gate into its CLI).
    Keep this in sync with review_run._dedupe_key if that shape ever changes.

    Defensive: a non-dict `finding`, or one missing "file"/"line"/"title" entirely, still
    produces a key rather than raising."""
    if not isinstance(finding, dict):
        finding = {}
    file_ = posixpath.normpath(
        str(finding.get("file", "") or "").replace("\\", "/")
    )
    line = finding.get("line")
    title = str(finding.get("title", "") or "").strip().lower()
    return (file_, line, title)


def _is_baseline_shape(data):
    """A dict written by save_baseline: has a proper "accepted" list."""
    return isinstance(data, dict) and isinstance(data.get("accepted"), list)


def _accepted_from_run_report(data):
    """Adapt a bench.review_aggregate.aggregate()-shaped run report (has "findings", not
    "accepted") into the same "accepted" entry list save_baseline would have written, so
    --baseline auto can point directly at a prior review_report_<stamp>.json instead of
    requiring a separate --write-baseline file.

    Mirrors bench.review_aggregate._verify_counts' own rule for "did the refuter pass run at
    all": if ANY finding in the report carries a "verify_verdict" key, only "confirmed" ones
    are accepted; if NONE do (the report was built with --no-refute, or refute simply never
    tagged anything), every reported finding is accepted -- there is no verdict data to be
    selective about."""
    findings = data.get("findings")
    if not isinstance(findings, list):
        findings = []
    has_verify_data = any(isinstance(f, dict) and "verify_verdict" in f for f in findings)
    if has_verify_data:
        selected = [f for f in findings
                    if isinstance(f, dict) and f.get("verify_verdict") == "confirmed"]
    else:
        selected = [f for f in findings if isinstance(f, dict)]

    entries = []
    for finding in selected:
        entries.append({
            "file": finding.get("file", ""),
            "line": finding.get("line"),
            "title": finding.get("title", ""),
            "severity": finding.get("severity", ""),
            "key": list(dedupe_key(finding)),
        })
    return entries


def load_baseline(path):
    """Read a baseline JSON file.

    A missing file is NOT an error -- it just means "no baseline yet" (bootstrap case), and
    returns an empty baseline: {"version": 1, "accepted": []}.

    A file that exists but is corrupt/unreadable (bad JSON, not an object, and not recognized
    as either supported shape below, permission error, etc.) DOES raise -- a ValueError with a
    clear message. This is deliberate: a bad --baseline path must be a loud failure, not
    silently degrade to an empty baseline that would make every current finding look "new" and
    hide the fact the baseline itself couldn't be read.

    Tolerates TWO input shapes so --baseline can point at either kind of file:
      (a) a baseline file written by save_baseline: {"version":1, "accepted":[...]}.
      (b) a full run report written by bench/review_run.py (review_report_<stamp>.json --
          bench.review_aggregate.render_json's shape): has "findings"/"by_severity" instead of
          "accepted". This is what --baseline auto points at (the previous run's own report).
          Normalized via _accepted_from_run_report into the same "accepted" shape as (a)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "accepted": []}
    except Exception as e:
        raise ValueError("could not read baseline file %r: %s" % (path, e)) from e

    if _is_baseline_shape(data):
        return data

    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        return {"version": 1, "accepted": _accepted_from_run_report(data)}

    raise ValueError(
        "baseline file %r is not a valid baseline or run report (expected a JSON object with "
        "an 'accepted' list, or a run report with a 'findings' list)" % (path,))


def save_baseline(path, accepted_findings, *, kind, generated_at):
    """Write a NEW baseline JSON file to `path`, atomically (tmp file + os.replace -- same
    pattern as tools/tool_probe.py's record_probe).

    accepted_findings is a list of finding dicts (typically every CONFIRMED finding from a
    run); only the fields relevant to future baseline matching/display are extracted into
    each stored entry: file, line, title, severity, and the dedupe_key itself (as a list, for
    JSON) so a later diff_against_baseline never needs to recompute it from a possibly-shifted
    finding shape.

    `generated_at` is a caller-supplied timestamp (e.g. bench/review_run.py's existing `stamp`
    variable) -- this function never reads the wall clock itself, so callers stay in full
    control of when "now" is captured (and tests stay deterministic).

    Schema written:
      {"version": 1, "kind": kind, "generated_at": generated_at,
       "accepted": [{"file":..., "line":..., "title":..., "severity":...,
                      "key": [file, line, title_lower]}, ...]}

    Defensive: non-dict entries in accepted_findings are skipped rather than raising."""
    entries = []
    for finding in accepted_findings or []:
        if not isinstance(finding, dict):
            continue
        entries.append({
            "file": finding.get("file", ""),
            "line": finding.get("line"),
            "title": finding.get("title", ""),
            "severity": finding.get("severity", ""),
            "key": list(dedupe_key(finding)),
        })

    payload = {
        "version": 1,
        "kind": kind,
        "generated_at": generated_at,
        "accepted": entries,
    }

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def diff_against_baseline(findings, baseline):
    """Compare this run's `findings` against a previously loaded `baseline` dict (from
    load_baseline).

    Returns {"new": [...], "regressed": [...], "resolved": [...], "unchanged": [...]}, each a
    list of ORIGINAL dicts (finding dicts for new/regressed/unchanged; the baseline's own
    stored stub dict for resolved) so a caller can render full file/line/title/severity info
    without a second lookup.

      - new: findings whose dedupe_key is not present in baseline["accepted"].
      - regressed: findings whose dedupe_key IS in baseline, and whose verify_verdict is
        "confirmed" OR missing/None (no refute data). Conservative on purpose: a
        known-and-previously-accepted issue that gets freshly reconfirmed -- or whose
        confirmation status we simply don't know this run because refute didn't run -- must
        never be silently downgraded to "unchanged" and hidden from a gate.
      - unchanged: findings whose dedupe_key IS in baseline and whose verify_verdict is
        something other than "confirmed"/None (e.g. "false_positive", "unclear") -- stayed
        baseline-accepted, not surfaced as new or regressed.
      - resolved: baseline keys that are NOT present at all in the current findings list.
        Informational only (no gating implication) -- the issue appears to be gone.

    Defensive: non-dict findings/baseline entries are skipped rather than raising. A falsy
    `baseline` (e.g. {}) is treated the same as an empty accepted list -- everything is "new"."""
    baseline_by_key = {}
    for entry in (baseline or {}).get("accepted", []) or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if isinstance(key, list):
            key = tuple(key)
        if not isinstance(key, tuple):
            # Malformed entry (missing/invalid "key") -- fall back to recomputing it from the
            # entry's own file/line/title fields so a hand-edited baseline still matches.
            key = dedupe_key(entry)
        baseline_by_key[key] = entry

    new = []
    regressed = []
    unchanged = []
    seen_keys = set()

    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        key = dedupe_key(finding)
        if key not in baseline_by_key:
            new.append(finding)
            continue
        seen_keys.add(key)
        verdict = finding.get("verify_verdict")
        if verdict == "confirmed" or verdict is None:
            regressed.append(finding)
        else:
            unchanged.append(finding)

    resolved = [entry for key, entry in baseline_by_key.items() if key not in seen_keys]

    return {"new": new, "regressed": regressed, "resolved": resolved, "unchanged": unchanged}


_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def should_gate(diff, fail_on):
    """Decide whether a review run should fail CI based on `diff` (from diff_against_baseline)
    and a `fail_on` severity threshold ("low"/"medium"/"high", or None/"" to never gate).

    Gates (returns should_fail=True) iff at least one finding in diff["new"] + diff["regressed"]
    has severity >= fail_on (low < medium < high). Returns (should_fail: bool,
    offending_findings: list) -- the offending list is the exact finding dicts responsible, in
    new-then-regressed order, for a caller to render.

    Defensive: an unrecognized fail_on value, or a finding with a missing/unrecognized
    severity, never raises -- unrecognized severities simply don't count toward the gate."""
    if not fail_on:
        return False, []
    threshold = _SEVERITY_ORDER.get(str(fail_on).strip().lower())
    if threshold is None:
        return False, []

    offending = []
    for finding in list(diff.get("new", []) or []) + list(diff.get("regressed", []) or []):
        if not isinstance(finding, dict):
            continue
        sev = _SEVERITY_ORDER.get(str(finding.get("severity", "")).strip().lower())
        if sev is not None and sev >= threshold:
            offending.append(finding)

    return (len(offending) > 0, offending)
