"""Replay the effort policy over a recorded mechanisms.jsonl. READ-ONLY.

    python scripts/effort_policy_replay.py [path]

Default path is the live ledger. It is opened for reading only and nothing is ever written to
it. The replay drives the SAME functions the worker's shadow hook calls
(effort_policy.build_signals / evaluate / apply), through a stand-in worker object, so what is
reported is what the hook would have recorded -- not a re-implementation of the rule.

WHAT THE ROWS CAN AND CANNOT SHOW (stated in the report too):
  * Evidence sequences exist only for refuter / panel verdict rows that carry an `instance`
    (the worker's cwd tail, not its name). Retry rows carry a run_id and an outcome but NO
    instance or goal_hash, and veto rows carry neither, so they cannot be attributed to a
    worker and are counted, not replayed.
  * With an empty run_id, a sequence is split wherever the turn number goes backwards,
    because the same cwd tail recurs across days. That can merge two workers or split one.
  * DONE vs stuck cannot be joined per worker. The only per-worker end state in these rows
    is the LAST refuter verdict (UPHELD / REFUTED), used below as a labelled proxy.
  * no-progress, STUCK/REFUSED, verify-failed, budget pressure and low-confidence signals are
    not in the rows, so those rules cannot fire in replay; only the REFUTED and first-pass
    UPHELD rules can.
"""
from __future__ import annotations

import collections
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from relay import effort_policy as ep  # noqa: E402


def default_ledger():
    """This checkout's .fleet/mechanisms.jsonl; from a linked git worktree, the MAIN
    checkout's (the live one), found through the worktree's `.git` pointer file."""
    here = os.path.join(ROOT, ".fleet", "mechanisms.jsonl")
    if os.path.exists(here):
        return here
    try:
        with io.open(os.path.join(ROOT, ".git"), "r", encoding="utf-8") as fh:
            ptr = fh.read().strip()
        if ptr.startswith("gitdir:"):
            gitdir = ptr.split(":", 1)[1].strip()          # <main>/.git/worktrees/<name>
            main = os.path.dirname(os.path.dirname(os.path.dirname(gitdir)))
            return os.path.join(main, ".fleet", "mechanisms.jsonl")
    except OSError:
        pass
    return here


class _Worker:
    """Stand-in exposing only what build_signals reads."""

    def __init__(self, level_knobs):
        self.turn = 0
        self.max_turns = 0
        self.refute_count = 0
        self._last_refute_verdict = ""
        self.no_progress = 0
        self.outcome = ""
        self.status = "running"
        self.verify_attempts = 0
        self.fresh_replay_count = 0
        self.transient = 0
        self.__dict__.update(level_knobs)


def load_rows(path):
    rows = []
    with io.open(path, "r", encoding="utf-8") as fh:        # read-only
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def sequences(rows):
    """{(run_id, instance, segment): [verdict rows in time order]} and unattributable counts."""
    verdicts = [r for r in rows if r.get("mechanism") in ("refuter", "panel")
                and r.get("executed") and r.get("decision_after") in ("REFUTED", "UPHELD")]
    skipped = collections.Counter()
    by = collections.defaultdict(list)
    for r in sorted(verdicts, key=lambda r: r.get("ts") or 0):
        if not r.get("instance"):
            skipped["verdict rows without instance"] += 1
            continue
        by[(r.get("run_id") or "", r["instance"])].append(r)
    out = {}
    for (run, inst), rs in by.items():
        seg, last_turn, cur = 0, -1, []
        for r in rs:
            t = r.get("turn")
            t = -1 if t is None else t
            if not run and t < last_turn and cur:
                out[(run, inst, seg)] = cur
                seg, cur = seg + 1, []
            cur.append(r)
            last_turn = t
        if cur:
            out[(run, inst, seg)] = cur
    for r in rows:
        if r.get("mechanism") == "retry":
            skipped["retry rows (no instance/goal_hash)"] += 1
        elif r.get("mechanism") == "veto":
            skipped["veto rows (no instance)"] += 1
    return out, skipped


def replay_one(rs, run_level, cfg):
    w = _Worker({"refuter": True, "max_refute": 3, "max_research": 3, "review_lenses": None})
    st = ep.EffortState(level=run_level)
    ctx, events = {}, []
    for n, r in enumerate(rs, 1):
        w.turn = r.get("turn") or n
        w.refute_count = n
        w._last_refute_verdict = r["decision_after"]
        sig = ep.build_signals(w, ctx, cfg)
        d = ep.evaluate(st, sig, cfg, turn=sig.turns_used)
        ep.apply(st, d, sig.turns_used)
        if d.action != "stay":
            ctx["refuted"] = ctx["upheld"] = ctx["stuck"] = 0
            events.append((w.turn, d.action, d.reason, st.level))
    return events, st


def main(argv):
    path = argv[1] if len(argv) > 1 else default_ledger()
    rows = load_rows(path)
    cfg = ep.PolicyConfig.from_env()
    run_level = os.environ.get("REPLAY_RUN_LEVEL", "auto")
    seqs, skipped = sequences(rows)

    esc, de, none = [], [], []
    rule_counts = collections.Counter()
    first_turns = []
    for key, rs in seqs.items():
        events, _ = replay_one(rs, run_level, cfg)
        ups = [e for e in events if e[1] == "up"]
        downs = [e for e in events if e[1] == "down"]
        for e in events:
            rule_counts[(e[1], e[2])] += 1
        (esc if ups else de if downs else none).append((key, rs, events))
        if events:
            first_turns.append(events[0][0])

    def end_upheld(group):
        n = len(group)
        u = sum(1 for _, rs, _ in group if rs[-1]["decision_after"] == "UPHELD")
        return n, u

    print("effort policy replay  (shadow rule, read-only)")
    print("ledger: %s  rows=%d  size=%d bytes" % (path, len(rows), os.path.getsize(path)))
    print("assumed starting level: %s   thresholds: %s" % (run_level, cfg))
    print("attributable worker sequences: %d (verdict rows in them: %d)"
          % (len(seqs), sum(len(v) for v in seqs.values())))
    for k, v in sorted(skipped.items()):
        print("  not replayable: %-40s %d" % (k, v))
    print()
    print("workers that would have been escalated:   %d" % len(esc))
    print("workers that would have been de-escalated (never escalated): %d" % len(de))
    print("workers with no switch:                   %d" % len(none))
    if first_turns:
        first_turns.sort()
        print("first switch turn: min=%d median=%d max=%d"
              % (first_turns[0], first_turns[len(first_turns) // 2], first_turns[-1]))
    print("rules fired (action, rule): count")
    for (a, rule), c in rule_counts.most_common():
        print("  %-5s %-28s %d" % (a, rule, c))
    print()
    print("end-state PROXY = last refuter verdict of the sequence (NOT the DONE/STUCK outcome,")
    print("which these rows cannot be joined to per worker):")
    for label, g in (("escalated", esc), ("de-escalated", de), ("no switch", none)):
        n, u = end_upheld(g)
        print("  %-13s n=%-5d last verdict UPHELD=%-5d (%.0f%%)  REFUTED=%d"
              % (label, n, u, 100.0 * u / n if n else 0.0, n - u))
    print()
    print("NOTE the 'no switch' row is UPHELD by construction (any REFUTED escalates), so its")
    print("100%% is not evidence. De-escalation needs %d first-pass UPHELDs IN A ROW; inside one"
          % cfg.upheld_down)
    print("worker an UPHELD ends the goal, so that streak can only be run-level (across goals),")
    print("which phase 1 does not feed. Zero de-escalations here is structural, not a finding.")
    print("CANNOT BE SHOWN FROM THESE ROWS: DONE vs STUCK per worker; no-progress, stuck/refused")
    print("streak, verify-failed, budget-pressure and low-confidence rules (no such rows). Only")
    print("the REFUTED and first-pass UPHELD rules are exercised. Sequence boundaries with an")
    print("empty run_id are inferred from the turn number and may merge or split workers.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
