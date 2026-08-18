"""Stage 0 of the settle A/B: replay recorded turns through both rules, offline.

WHY OFFLINE FIRST

The plan's most important judgement is that pass@1 is the wrong primary endpoint. Settle
quality reaches it through many intermediate steps, so detecting a 1-2 point effect needs
thousands of turns; under fleet and RAM limits the achievable conclusion is "underpowered",
not "no effect". So the primary endpoint is the MECHANISM: how often the accepted text was
not the text the turn settled to.

`_settle_trace` in collect mode already records, per turn, the sequence of texts the block
went through -- which means both rules can be fed the same recorded samples and asked where
they would have stopped. No fleet, no RAM, nothing that touches the product's terms of use.

WHAT THE LABEL IS, AND WHERE IT IS BLIND

Ground truth is the text the turn actually settled to. That cannot be the last PRE-accept
sample: the settle loop ends when production accepts, so scoring against it makes truncation
zero by construction -- and zero exactly when production truncated, because that is the
moment recording stops. Collect mode therefore keeps reading afterwards (`post_accept`), and
those samples are the label.

That tail is finite. It watches for about eight seconds, so a continuation arriving later
than that is recorded as "the text never grew". The failure being measured IS a long pause,
so the blind spot overlaps the target: a pause longer than the tail looks identical to no
pause at all. This script therefore reports the tail length alongside every truncation count,
because "we saw none" and "we could not have seen them" are different claims and only one of
them is evidence.

WHAT IT CAN ANSWER REGARDLESS

Where the two rules stop differs whether or not truncation is observable, so the accept-index
discordance and the added latency (the plan's §5.4 side-effect budget) are measurable from the
same corpus.
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from relay import settle as S   # noqa: E402

DEFAULT_TRACE = ROOT / ".fleet" / "settle_trace_collect.jsonl"

#: Phases that carry an observation of the answer block BEFORE production decided.
_PRE = ("stable", "changed", "processing", "generating")


def load_turns(path):
    """{turn_id: [row, ...]} in time order, dropping turns with no usable observation."""
    by_turn = collections.defaultdict(list)
    for line in io.open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("turn_id"):
            by_turn[row["turn_id"]].append(row)
    out = {}
    for tid, rows in by_turn.items():
        rows.sort(key=lambda r: float(r.get("ts") or 0))
        if any(r.get("phase") in _PRE for r in rows):
            out[tid] = rows
    return out


def _legacy_accept(samples, *, dwell_s, samples_needed):
    """Where the OLD rule would have stopped: dwell only, no sample requirement.

    This is the fleet/refuter/research shape -- one dwell of stillness and commit -- which is
    what three of the four sites did and what the migration replaces.
    """
    last, since = None, None
    for i, (ts, text, generating, processing, marker) in enumerate(samples):
        if generating:
            last, since = None, None
            continue
        if processing:
            last, since = None, None
            continue
        if text == last:
            need = dwell_s if marker else dwell_s * 2.0
            if since is not None and (ts - since) >= need:
                return i, text
        else:
            last, since = text, ts
    return None, None


def _unified_accept(samples, *, dwell_s, samples_needed):
    """Where `relay.settle` would have stopped, on the same samples."""
    state = S.SettleState()
    for i, (ts, text, generating, processing, marker) in enumerate(samples):
        state, outcome = S.settle_step(state, text, now=ts, dwell_s=dwell_s,
                                       generating=bool(generating),
                                       is_processing=bool(processing),
                                       has_marker=bool(marker),
                                       samples=samples_needed)
        if outcome == S.ACCEPT:
            return i, text
    return None, None


def replay(rows, *, dwell_s, samples_needed):
    """One turn: both accept points, the label, and whether the label could see a change."""
    pre = [r for r in rows if r.get("phase") in _PRE]
    post = [r for r in rows if r.get("phase") == "post_accept"]
    samples = []
    for r in pre:
        text = r.get("text") or ""
        samples.append((float(r.get("ts") or 0), text,
                        bool(r.get("generating")),
                        r.get("phase") == "processing",
                        bool(r.get("marker", r.get("has_marker", False)))))
    if not samples:
        return None

    label = (post[-1].get("text") if post else None)
    if label is None:
        label = samples[-1][1]
    watched = 0.0
    if len(post) >= 2:
        watched = float(post[-1]["ts"]) - float(post[0]["ts"])

    # PRODUCTION'S OWN PARAMETERS, READ FROM THE TRACE. The first version passed CLI
    # defaults, which replayed the corpus under settings it was not recorded under: every
    # recorded turn is markerless, so a dwell of 4.0 doubled to 8.0 and neither arm ever
    # accepted anything. The counts were then 0/120 for both -- which reads exactly like
    # "no difference" and was in fact "the probe never ran". `need_dwell` / `need_samples`
    # are on every stable row precisely so the replay does not have to guess.
    stable = [r for r in rows if r.get("phase") == "stable"]
    if stable:
        marker_seen = bool(stable[-1].get("marker"))
        factor = 1.0 if marker_seen else S.MARKERLESS_DWELL_FACTOR
        try:
            dwell_s = float(stable[-1]["need_dwell"]) / factor
            samples_needed = max(1, int(round(int(stable[-1]["need_samples"]) / factor)))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pass

    li, ltext = _legacy_accept(samples, dwell_s=dwell_s, samples_needed=samples_needed)
    ui, utext = _unified_accept(samples, dwell_s=dwell_s, samples_needed=samples_needed)
    return {
        "polls": len(samples),
        "label_len": len(label),
        "tail_watched_s": round(watched, 2),
        "text_ever_changed": len({s[1] for s in samples}) > 1,
        "legacy_index": li, "unified_index": ui,
        "legacy_truncated": (li is not None and ltext != label),
        "unified_truncated": (ui is not None and utext != label),
        "legacy_never_accepted": li is None,
        "unified_never_accepted": ui is None,
        "delay_polls": (None if (li is None or ui is None) else ui - li),
        "delay_s": (None if (li is None or ui is None)
                    else round(samples[ui][0] - samples[li][0], 2)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", default=str(DEFAULT_TRACE))
    ap.add_argument("--dwell", type=float, default=4.0)
    ap.add_argument("--samples", type=int, default=S.DEFAULT_SAMPLES)
    args = ap.parse_args()

    path = Path(args.trace)
    if not path.is_file():
        print("no trace at %s -- collect one with MCP_SETTLE_TRACE_COLLECT=1" % path)
        return 2

    turns = load_turns(path)
    results = [r for r in (replay(rows, dwell_s=args.dwell, samples_needed=args.samples)
                           for rows in turns.values()) if r]
    # THE PROMPT IS THE CLUSTER. `turn_id` is "<prompt>|<turn>", so the prefix groups turns
    # that asked the same thing. Ten repeats of twelve prompts is twelve observations of the
    # rule, not a hundred and twenty, and treating them as independent is how a confident
    # interval gets built on a sample that does not support one.
    n_clusters = len({tid.split("|")[0] for tid in turns})
    if not results:
        print("no replayable turns in %s" % path)
        return 2

    changed = [r for r in results if r["text_ever_changed"]]
    tails = sorted(r["tail_watched_s"] for r in results if r["tail_watched_s"])
    delays = [r["delay_s"] for r in results if r["delay_s"] is not None]

    print("SETTLE STAGE 0 -- OFFLINE REPLAY")
    # REPO-RELATIVE, NEVER ABSOLUTE. This printed the full path, the output was saved
    # into the results directory, and the account name in it reached a public
    # repository. The local identity check ran clean because it was run BEFORE the
    # file was `git add`ed -- it inspects tracked files, so an untracked one is
    # invisible to it. The tool that writes the artefact is the right place to fix
    # this: a checker that has to catch it is already one step too late.
    try:
        shown = path.resolve().relative_to(ROOT)
    except Exception:
        shown = path.name
    print("  trace                    %s" % shown)
    print("  parameters               per turn from the trace's own need_dwell/need_samples,")
    print("                           never from a default -- see the note in replay()")
    print()
    print("POPULATION")
    print("  turns replayed           %d" % len(results))
    print("  ... whose text EVER changed  %d" % len(changed))
    print("      A turn whose text never moved cannot exhibit truncation, so it carries no")
    print("      information about the primary endpoint. The informative N is the second")
    print("      number, not the first.")
    print("  independent clusters     %d" % n_clusters)
    print("      Turns sharing a prompt are not independent observations of the rule; they")
    print("      are repeats of one observation. %d turns over %d prompts is %d clusters, and"
          % (len(results), n_clusters, n_clusters))
    print("      any interval computed as though N were %d is too narrow." % len(results))
    print()
    print("PRIMARY -- truncated capture (accepted text != the text the turn settled to)")
    print("  legacy rule              %d / %d" % (sum(r["legacy_truncated"] for r in results),
                                                  len(results)))
    print("  unified rule             %d / %d" % (sum(r["unified_truncated"] for r in results),
                                                  len(results)))
    if tails:
        print("  label observation window median %.1fs (min %.1f, max %.1f)"
              % (statistics.median(tails), tails[0], tails[-1]))
        print("      THE LABEL IS BLIND PAST THAT WINDOW. The failure being counted is a long")
        print("      streaming pause, so a pause longer than the tail is recorded as 'the text")
        print("      never grew' -- indistinguishable from no pause at all. A zero here is not")
        print("      evidence that truncation does not happen; it is evidence that none was")
        print("      visible within %.0f seconds of the accept." % statistics.median(tails))
    print()
    print("SECONDARY -- where the two rules disagree")
    disc = [r for r in results if r["legacy_index"] != r["unified_index"]]
    print("  turns with different accept points  %d / %d" % (len(disc), len(results)))
    print("  legacy never accepted    %d" % sum(r["legacy_never_accepted"] for r in results))
    print("  unified never accepted   %d" % sum(r["unified_never_accepted"] for r in results))
    print("      A turn the unified rule never accepts inside the recorded window is the")
    print("      REGRESSION the plan names: production settled it and the stricter rule would")
    print("      still be waiting. Recorded windows are finite, so this over-counts -- it is a")
    print("      list to investigate, not a verdict.")
    print()
    print("SIDE EFFECT -- added latency (the plan budgets +1.5s on the median)")
    if delays:
        delays.sort()
        med = statistics.median(delays)
        p95 = delays[int(len(delays) * 0.95) - 1] if len(delays) >= 20 else delays[-1]
        print("  median  %+.2fs   p95  %+.2fs   (n=%d paired turns)" % (med, p95, len(delays)))
        print("  within the +1.5s budget: %s" % ("yes" if med <= 1.5 else "NO"))
    else:
        print("  no turn was accepted by both rules, so there is nothing to pair")
    print()
    print("VERDICT")
    if not tails or statistics.median(tails) < args.dwell * 4:
        print("  CANNOT DECIDE on the primary endpoint. The label's observation window is")
        print("  short relative to the pauses that produce the failure, so neither a zero nor")
        print("  a difference would mean what it appears to. Stage 1 is NOT justified by this")
        print("  run; a longer post-accept tail is what would make the corpus answer.")
    else:
        print("  The label window is long enough relative to dwell for the counts above to be")
        print("  read as measurements rather than as absence of evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
