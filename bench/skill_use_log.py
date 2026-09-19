"""Did a skill actually get consulted, read from the server rather than from the worker.

The experiment this serves had no way to observe the thing it measured. Transcripts hold user
and assistant text and nothing else -- no tool calls -- so the only signal was the shape of the
final answer, which varied 0..2 WITHIN a single arm and drowned any effect between arms. The
obvious alternative, asking the worker whether it consulted a skill, is a self-report; a
self-report nobody can verify is worth less than no field at all.

`tools/skill_ops` now records each consultation. This reads those records back and answers one
question per run: between these two times, was a skill matched, and was one loaded.
"""
from __future__ import annotations

import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG = os.path.join(REPO, ".fleet", "skill_use.jsonl")


def read(path=DEFAULT_LOG):
    """Every consultation record. A half-written last line is skipped, not fatal."""
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("kind"):
                    rows.append(row)
    except OSError:
        return []
    return rows


def within(rows, start, end):
    """Records in [start, end]. Both bounds are required: a window open at either end would
    let a neighbouring run's consultations count as this one's, which in a sequential A/B is
    the arm that ran immediately before."""
    out = []
    for r in rows:
        try:
            ts = float(r.get("ts"))
        except (TypeError, ValueError):
            continue
        if start <= ts <= end:
            out.append(r)
    return out


def observe(start, end, path=DEFAULT_LOG, rows=None):
    """What happened in one run's window.

    `matched` and `loaded` are counts, not booleans, because a worker that consults twice is
    doing something different from one that consults once and the difference should not be
    flattened at the point of measurement.

    `rows` TAKES AN ALREADY-READ LOG, and its absence is why this function had no caller.
    `compare_runs` needs one window per run and re-reading the file for each of them is the
    obvious waste -- so the call was switched off with a literal `if False` and a NARROWER
    copy of this dict was written inline beside it. Two implementations of "count the events
    in this window", one of them unreachable, is the shape this repository keeps finding; the
    parameter is what the inline copy was standing in for.

    `injected` WAS MISSING FROM BOTH COPIES. The live log holds three kinds -- measured
    2026-09-19 over 789 records: match 566, inject 181, load 42 -- and neither this dict nor
    the inline one counted the middle one. A reader that silently drops a fifth of the
    records it is summarising is worse than no reader, because the total still looks like a
    total.
    """
    rows = within(read(path) if rows is None else rows, start, end)
    return {
        "matched": sum(1 for r in rows if r.get("kind") == "match"),
        "matched_something": sum(1 for r in rows
                                 if r.get("kind") == "match" and r.get("matched")),
        "injected": sum(1 for r in rows if r.get("kind") == "inject"),
        "loaded": sum(1 for r in rows if r.get("kind") == "load"),
        "skills": sorted({r.get("matched") for r in rows if r.get("matched")}),
        "consulted": any(r.get("kind") == "match" for r in rows),
    }


def compare_runs(runs, path=DEFAULT_LOG):
    """arm -> [(start, end), ...]. Returns the consultation rate per arm.

    This is the variance the answer-shape score could not control. Consulting is a binary
    event the server witnessed; the shape of an answer is a judgement about text a worker
    wrote, and the two are not the same measurement even when they are about the same thing.
    """
    rows = read(path)
    # ONE IMPLEMENTATION. This used to be `observe(s, e, path) if False else {...}` -- the call
    # disabled by a literal and its result typed out again, more narrowly, in the else branch.
    # `observe` now takes the rows, which is what the inline copy existed to avoid re-reading.
    obs_all = {arm: [observe(s, e, rows=rows) for (s, e) in windows]
               for arm, windows in (runs or {}).items()}
    out = {}
    for arm, obs in obs_all.items():
        n = len(obs) or 1
        out[arm] = {
            "n": len(obs),
            "consulted_runs": sum(1 for o in obs if o["matched"] > 0),
            "loaded_runs": sum(1 for o in obs if o["loaded"] > 0),
            "consult_rate": sum(1 for o in obs if o["matched"] > 0) / n,
            "load_rate": sum(1 for o in obs if o["loaded"] > 0) / n,
        }
    return out


def report(path=DEFAULT_LOG) -> str:
    """What this log holds, for a person who wants to know whether skills are being consulted.

    A RECORD NOBODY READS IS THE SAME AS NO RECORD, and this one had been written for three
    weeks: measured 2026-09-19, 789 records spanning 08-28 to 09-18, written from three sites
    in tools/skill_ops.py, and every reader in this module was unreachable. The module opens
    by explaining that the experiment it serves "had no way to observe the thing it measured";
    the observation existed and the way to look at it did not.

    NOT `compare_runs`. That one answers a question about ARMS and needs the per-run windows
    only the experiment can supply. This answers the question a person actually asks in front
    of the file -- is anything consulting skills, which ones, and when did it last happen --
    and needs nothing but the log.
    """
    import time

    rows = read(path)
    if not rows:
        # "No records" and "no file" are the same sentence here on purpose: both mean nothing
        # has been observed, and claiming to tell them apart would need a fact this does not
        # have (a log is created lazily by its first write).
        return "skill use: no records at %s" % path
    stamps = [float(r["ts"]) for r in rows
              if isinstance(r.get("ts"), (int, float, str)) and str(r.get("ts")).strip()
              and _is_number(r.get("ts"))]
    if not stamps:
        return "skill use: %d records, none with a usable timestamp" % len(rows)
    first, last = min(stamps), max(stamps)
    seen = observe(first, last, rows=rows)
    lines = [
        "skill use: %d records, %s -> %s"
        % (len(rows), time.strftime("%Y-%m-%d %H:%M", time.localtime(first)),
           time.strftime("%Y-%m-%d %H:%M", time.localtime(last))),
        "  matched %d (%d of them matched something), injected %d, loaded %d"
        % (seen["matched"], seen["matched_something"], seen["injected"], seen["loaded"]),
    ]
    # THE THREE KINDS ARE NOT A PIPELINE AND THE GAP BETWEEN THEM IS THE FINDING. A match that
    # matched nothing is a query no skill covered; a match that never became a load is a skill
    # found and not read. Printing the counts without the gaps leaves the reader to subtract.
    if seen["matched"]:
        lines.append("  %d queries matched no skill at all"
                     % (seen["matched"] - seen["matched_something"]))
    if seen["skills"]:
        lines.append("  skills matched: %s" % ", ".join(seen["skills"]))
    return "\n".join(lines)


def _is_number(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


if __name__ == "__main__":                                      # pragma: no cover
    print(report())
