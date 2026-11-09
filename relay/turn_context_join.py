# -*- coding: utf-8 -*-
"""Read-only join of a tool call to the fan-out identity of the worker whose turn was in flight.

WHY THIS EXISTS (B3 communication telemetry, attribution). The cost-weighted sibling-duplication
reports (scripts/sibling_dup_report.py and scripts/sibling_cost_report.py) can only measure a
fan-out campaign's duplicated work when each tool call is attributed to a campaign child. On the
live data that attribution is almost always empty: a Copilot fleet worker never volunteers its
identity to the MCP session (measured: 1 claim_turn in 5,718 calls), so tool_events.jsonl call
rows carry an empty `task`, no `campaign_id`/`subtask_id`, and only a tab-local worker name. The
coordinator, however, already records which worker it sent each turn to, for which campaign child,
and when (tools/turn_context.py writes `.fleet/turn_context.jsonl`, two rows per turn, with the
fan-out identity campaign_id / task_id / role on the row).

This module reads those turn windows from a chosen fleet directory (so a report can pass
--fleet-dir and a test can point at a fixture) and answers, for a call's (worker, wall-clock ts),
the fan-out identity of the single turn window that contained it. It is the SAME overlap rule as
tools/turn_context.identity_of, lifted off the module-level live path so it is read-only and
testable: exactly one matching window wins; windows that disagree (touching turns share the slack)
return nothing rather than guess. It writes nothing and changes no behaviour; a report that does
not read turn windows is unaffected.

The join runs over data already on disk, so it attributes PAST calls without any new run.
"""
from __future__ import annotations

import json
import os

#: Window edge tolerance, seconds, applied to both ends. Matches tools/turn_context.SLACK_S so a
#: call stamped just after a send, or landing just after the reply was read, still lands in its
#: window; touching windows of different workers become ambiguous rather than guessed.
SLACK_S = 1.5
#: A window with no close row claims events for at most this long after it started. Matches
#: tools/turn_context.MAX_OPEN_S.
MAX_OPEN_S = 1800.0
#: Identity fields carried on a turn window (a subset of tools/turn_context.IDENT_KEYS; these are
#: the ones the sibling reports join on).
_IDENT_KEYS = ("campaign_id", "task_id", "role")


def _read_jsonl(path):
    """[dict] from a .jsonl file, skipping unparseable lines. [] when absent. Pure read."""
    rows = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        pass
    return rows


def _ident(row):
    """{campaign_id, task_id, role} present on a window row, empty values omitted."""
    return {k: str(row[k]) for k in _IDENT_KEYS if row.get(k)}


def build_windows(rows):
    """rows (file order, open/close turn-context events) -> [window dict].

    A window is {worker, start, end|None, ident}. A new `open` for a worker supersedes a window
    that never closed (its end becomes the new send time). A `close` whose `open` fell off the
    read tail stands on its own. This mirrors tools/turn_context._build so the two agree.
    """
    last = {}
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("worker"):
            continue
        try:
            w = str(r["worker"])
            start = float(r["t_send"])
        except (KeyError, TypeError, ValueError):
            continue
        ident = _ident(r)
        if r.get("event") == "open":
            prev = last.get(w)
            if prev is not None and prev["end"] is None:
                prev["end"] = start
            win = {"worker": w, "start": start, "end": None, "ident": ident}
            last[w] = win
            out.append(win)
        elif r.get("event") == "close":
            try:
                end = float(r["t_done"])
            except (KeyError, TypeError, ValueError):
                continue
            prev = last.get(w)
            if prev is not None and prev["end"] is None and abs(prev["start"] - start) < 0.01:
                prev["end"] = end
                if ident and not prev["ident"]:
                    prev["ident"] = ident
            else:
                win = {"worker": w, "start": start, "end": end, "ident": ident}
                last[w] = win
                out.append(win)
    return out


class WindowIndex:
    """Turn windows indexed by worker, answering identity_of(worker, ts). Read-only, pure.

    An empty index (no turn_context.jsonl, or a file that carried no identity) answers {} to
    every query, so a caller that joins against it simply gains nothing and keeps its old result.
    """

    def __init__(self, windows=None):
        self.by_worker = {}
        for win in (windows or []):
            self.by_worker.setdefault(win["worker"], []).append(win)
        #: how many windows carry a non-empty identity (for a report's honesty counts)
        self.windows = sum(len(v) for v in self.by_worker.values())
        self.with_ident = sum(1 for v in self.by_worker.values() for w in v if w["ident"])

    def identity_of(self, worker, ts):
        """{campaign_id, task_id, role} of the turn window of `worker` containing wall-clock `ts`.

        {} when no window matches, when the matching window carried no identity, or when the
        worker's matching windows disagree (an identity is never picked between two).
        """
        if not worker or not isinstance(ts, (int, float)):
            return {}
        found = []
        for win in self.by_worker.get(str(worker), ()):
            hi = (win["end"] if win["end"] is not None else win["start"] + MAX_OPEN_S) + SLACK_S
            if win["start"] - SLACK_S <= ts <= hi:
                found.append(win["ident"])
        if found and all(f == found[0] for f in found) and found[0]:
            return dict(found[0])
        return {}


def load_windows(fleet_dir):
    """WindowIndex built from `turn_context.jsonl` (+ rotated `.1`) under fleet_dir. Read-only.

    The rotated copy is read first so a window whose open row rotated out is still whole. An
    absent file yields an empty index (every query answers {}).
    """
    rows = []
    for name in ("turn_context.jsonl.1", "turn_context.jsonl"):
        rows += _read_jsonl(os.path.join(fleet_dir, name))
    return WindowIndex(build_windows(rows))
