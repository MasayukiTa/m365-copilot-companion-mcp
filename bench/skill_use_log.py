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


def observe(start, end, path=DEFAULT_LOG):
    """What happened in one run's window.

    `matched` and `loaded` are counts, not booleans, because a worker that consults twice is
    doing something different from one that consults once and the difference should not be
    flattened at the point of measurement.
    """
    rows = within(read(path), start, end)
    return {
        "matched": sum(1 for r in rows if r.get("kind") == "match"),
        "matched_something": sum(1 for r in rows
                                 if r.get("kind") == "match" and r.get("matched")),
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
    out = {}
    for arm, windows in (runs or {}).items():
        obs = [observe(s, e, path) if False else {
            "matched": sum(1 for r in within(rows, s, e) if r.get("kind") == "match"),
            "loaded": sum(1 for r in within(rows, s, e) if r.get("kind") == "load"),
        } for (s, e) in windows]
        n = len(obs) or 1
        out[arm] = {
            "n": len(obs),
            "consulted_runs": sum(1 for o in obs if o["matched"] > 0),
            "loaded_runs": sum(1 for o in obs if o["loaded"] > 0),
            "consult_rate": sum(1 for o in obs if o["matched"] > 0) / n,
            "load_rate": sum(1 for o in obs if o["loaded"] > 0) / n,
        }
    return out
