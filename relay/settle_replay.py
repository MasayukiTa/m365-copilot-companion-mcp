"""Stage 0 of the settle-unification plan: replay recorded turns through two implementations.

WHAT THIS IS FOR

The plan proposes replacing the settle predicate and asks, sensibly, whether that is worth
doing before doing it. Its Stage 0 answers that offline: take the sample sequences a turn
actually went through, feed them to the old logic and the new one, and compare WHERE each
accepted. No fleet, no RAM, no live tenant, and nothing that touches Copilot's terms -- it is
re-analysis of logs already on disk.

WHY IT COULD NOT BE RUN AS WRITTEN

The plan names the settle trace as its input. The trace was persisting -- 5,731 lines, not
stderr -- but four things stood between it and a replay, and the first is the one that
matters:

  * it recorded only turns already past 60 seconds, and the primary endpoint is TRUNCATED
    CAPTURE, which is an early accept. Those turns settle in seconds and wrote nothing. The
    file was a record of the opposite population;
  * each line kept text_len and the last 90 characters, so a sequence cannot be reconstructed;
  * there was no turn identifier, so lines cannot be grouped into turns;
  * and no final text, so the ground-truth label the plan defines -- "the turn's last text" --
    cannot be built.

Collect mode in copilot_autopilot_relay fixes all four. This module is what consumes it.

WHAT A RESULT MEANS

`truncated` here is exactly the plan's definition: the implementation accepted at a point
where the text was shorter than the turn's final text. It is a lower bound -- a turn that
settled correctly might still have been about to change -- and it is measured identically for
both arms, which is what makes the paired comparison fair even where the label is imperfect.
"""
from __future__ import annotations

import io
import json
import os
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TRACE = os.path.join(REPO, ".fleet", "settle_trace.jsonl")


class NotReplayable(RuntimeError):
    """Raised when a trace cannot answer the question, with what is missing."""


def load_turns(path=None) -> dict:
    """{turn_id: [poll, ...]} in recorded order, from a collect-mode trace.

    Refuses rather than degrading. A trace without turn ids or full text can be summarised
    into a number, and that number would describe something other than what the caller asked
    -- which is the failure this whole exercise exists to avoid.
    """
    path = path or DEFAULT_TRACE
    rows = []
    try:
        for line in io.open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except OSError as exc:
        raise NotReplayable("no trace at %s (%s)" % (path, exc))

    if not rows:
        raise NotReplayable(
            "the trace at %s is empty. An empty trace is a real finding in this module -- it "
            "is how a fifteen-minute hang was traced to a client bug -- but it cannot be "
            "replayed." % path)

    missing = [k for k in ("turn_id", "text") if k not in rows[0]]
    if missing:
        raise NotReplayable(
            "this trace was not recorded in collect mode: %s absent. Re-record with "
            "MCP_SETTLE_TRACE_COLLECT=1, which also drops the 60-second gate that excludes "
            "exactly the early-accept turns the primary endpoint is about."
            % ", ".join(missing))

    turns = defaultdict(list)
    for r in rows:
        turns[r.get("turn_id") or "unlabelled"].append(r)
    return dict(turns)


# --------------------------------------------------------------------------------------
# The two implementations, as pure functions over a recorded sequence
# --------------------------------------------------------------------------------------

def accept_index_legacy(polls, *, need_stable=2) -> int:
    """Where the current predicate would have accepted, or -1.

    Modelled on wait_for_idle's rule as it stands: not generating, and the text unchanged for
    `need_stable` consecutive polls.
    """
    # RUN LENGTH, counting the first read. Two identical reads is a run of two, which is what
    # "unchanged for two polls" means to everyone who says it -- counting transitions instead
    # is off by one and silently makes the predicate stricter than the thing it models.
    run = 0
    previous = None
    for i, p in enumerate(polls):
        text = p.get("text", "")
        if p.get("generating"):
            run, previous = 0, None
            continue
        run = run + 1 if text == previous else 1
        previous = text
        if run >= need_stable and text.strip():
            return i
    return -1


def accept_index_sampled(polls, *, need_stable=2, need_samples=3) -> int:
    """Where the proposed predicate would have accepted, or -1.

    The proposal adds a sample-count floor: stability is not enough on its own, because two
    identical reads can both land inside one pause in a stream that has not finished. The
    plan is explicit that this trades latency for correctness, and that the sample count is
    the quality argument and must not be the thing tuned away when the latency shows up.
    """
    run = 0
    previous = None
    for i, p in enumerate(polls):
        text = p.get("text", "")
        if p.get("generating"):
            run, previous = 0, None
            continue
        run = run + 1 if text == previous else 1
        previous = text
        if run >= need_stable and i + 1 >= need_samples and text.strip():
            return i
    return -1


IMPLEMENTATIONS = {"legacy": accept_index_legacy, "sampled": accept_index_sampled}


# --------------------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------------------

def replay(turns, implementations=None) -> dict:
    """Run every recorded turn through each implementation and count truncated captures.

    Returns per-implementation counts plus the DISCORDANT pairs, which are what a McNemar
    test consumes and what the plan's power calculation is written against. Reporting only
    the two rates would throw away the pairing and with it most of the statistical power.
    """
    impls = implementations or IMPLEMENTATIONS
    per = {name: {"accepted": 0, "never": 0, "truncated": 0} for name in impls}
    discordant = {"legacy_worse": 0, "sampled_worse": 0}
    rows = []

    for turn_id, polls in sorted(turns.items()):
        final = (polls[-1].get("text") or "") if polls else ""
        outcome = {}
        for name, fn in impls.items():
            idx = fn(polls)
            if idx < 0:
                per[name]["never"] += 1
                outcome[name] = None
                continue
            per[name]["accepted"] += 1
            accepted = polls[idx].get("text") or ""
            truncated = accepted != final
            per[name]["truncated"] += bool(truncated)
            outcome[name] = {"index": idx, "truncated": truncated,
                             "accepted_len": len(accepted), "final_len": len(final)}

        a, b = outcome.get("legacy"), outcome.get("sampled")
        if a and b and a["truncated"] != b["truncated"]:
            key = "legacy_worse" if a["truncated"] else "sampled_worse"
            discordant[key] += 1
        rows.append({"turn_id": turn_id, "polls": len(polls), **outcome})

    total = len(turns)
    for name, counts in per.items():
        counts["truncation_rate"] = (counts["truncated"] / counts["accepted"]
                                     if counts["accepted"] else None)
    return {
        "turns": total,
        "per_implementation": per,
        "discordant": discordant,
        "discordant_total": sum(discordant.values()),
        # The plan's own stopping rule, stated where the number is produced rather than left
        # to the reader: about forty discordant pairs are needed for the comparison to have
        # the power it claims, and a smaller run reports a difference it cannot support.
        "sufficiently_powered": sum(discordant.values()) >= 40,
        "rows": rows,
    }


def report(result) -> str:
    lines = ["settle replay: %d turns" % result["turns"]]
    for name, c in sorted(result["per_implementation"].items()):
        rate = c["truncation_rate"]
        lines.append("  %-8s accepted %4d  never %3d  truncated %4d  rate %s"
                     % (name, c["accepted"], c["never"], c["truncated"],
                        "%.3f" % rate if rate is not None else "n/a"))
    d = result["discordant"]
    lines.append("  discordant: legacy worse %d / sampled worse %d (total %d)"
                 % (d["legacy_worse"], d["sampled_worse"], result["discordant_total"]))
    if not result["sufficiently_powered"]:
        lines.append("  UNDERPOWERED: the plan asks for ~40 discordant pairs; this run has "
                     "%d, so a difference here is not evidence either way."
                     % result["discordant_total"])
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="relay.settle_replay", description=__doc__.splitlines()[0])
    ap.add_argument("--trace", default=DEFAULT_TRACE)
    args = ap.parse_args(argv)
    try:
        turns = load_turns(args.trace)
    except NotReplayable as exc:
        print("cannot replay: %s" % exc)
        return 2
    print(report(replay(turns)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
