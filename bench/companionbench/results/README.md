# Measurement results

What the benchmark actually produced, kept next to the code that produced it.

These live here rather than under `docs/research/` because that directory is gitignored --
which meant an earlier round of commit messages said "recorded under docs/research/results/"
while the files existed only on one machine. A result nobody else can read is not recorded.

Each file states its own limits. Read those before quoting a number: three of the four say,
in different words, that the figure does not mean what a reader would assume.

| file | what it is |
|---|---|
| `baseline_bridge.txt` | first suite run against a live target, 13/22. Superseded as a capability figure by the reliability run below. |
| `reliability_after_fix.txt` | three repeats: 7, 17, 19. Confounded -- back to back, fixed order. Superseded. |
| `reliability_rested.txt` | three repeats on a rested tenant: 7, 20, 16. Same confound. Superseded. |
| `reliability_deconfounded.txt` | three repeats, rested and reshuffled: 17, 16, 15. Three of four criteria pass; per-episode stability does not. **The current reading.** |
| `stage0_settle_replay.txt` | settle-unification Stage 0: nothing changed after acceptance on any of 120 turns. |
| `section15_security_experiment.txt` | the guard simulation, with a list of the metrics it cannot produce. |

The `.json` files are the raw rows behind two of the reports, with absolute paths stripped.

## Transport: why episodes flip

`transport_probe.json` — 4 episodes × 5 repeats = 20 turns, with the conversation read back
after each one. Eleven passes, every one with its prompt in the conversation. Nine failures:
one with the prompt present, replying in 608 characters, and eight without it, replying in 8
to 113. The check answered on 20 of 20.

**A CLAIM MADE FROM THIS AND THEN WITHDRAWN.** Those eight turns were briefly treated as
never having happened, which moved them out of the capability denominator and took capability
from 0.579 to 0.917. That figure is retracted; it was never earned.

The reasoning was circular. The detector was validated on the same twenty observations it
then re-scored, and "it abstained on none of them" measures whether the check *answers*, not
whether the answers are *right*. Nothing tested the direction that matters: a turn that WAS
delivered, made to look absent. Three ways that happens were reachable in the code and none
were handled — a capture the bridge itself flagged as incomplete, a conversation that rotated
between sending and looking, and a view read before it had rendered. A capability failure that
navigated would have arranged its own exclusion.

**AND 0.579 IS NOT THE SUITE'S CAPABILITY EITHER.** Retracting the inflated figure left the
uninflated one in its place, quoted as though it described the system. It does not. It is
11/19 over FOUR episodes chosen precisely because they flip -- a subsample selected on
instability, which biases it downward by construction, and 19 turns of one episode repeated is
not 19 episodes. The full-suite deconfounded run is the figure that describes the suite:
15, 16 and 17 of 22, or roughly 0.68-0.77.

Two numbers were therefore wrong in opposite directions for the same reason: a denominator
was quoted without the population it came from. Over the four flip-prone episodes,
capability is 0.579, coverage 0.950, end-to-end 0.550.

**AND THE CAUSAL CLAIM IS WEAKENED TOO.** A second round of review pointed out that "the probe
established a cause" is still more than the data carries. It established an ASSOCIATION,
measured with the old detector, on a run whose saved rows record the booleans but not the
truncation, hydration, busy or retry evidence needed to re-judge those turns under the rules
that replaced it. Non-delivery is the leading hypothesis for why episodes flip. It is not a
finding, and it will not be one until a run is measured with a detector whose negatives are
anchored and whose abstentions are reported separately from its denials.

`test_delivery_detector_validation.py` is the held-out matrix the original claim lacked —
six constructed rows where the ground truth is known rather than inferred, including a
delivered turn with a bad answer, which must never be excluded. One row is a knowing false
negative: if the conversation renders the prompt without the marker, nothing here can tell
that from an undelivered turn. It is recorded as a limit rather than left to be discovered.

## The transport finding was the instrument, and here is the run that shows it

`reliability_anchored.txt` — 3 repeats, rested 600s, reshuffled, measured with the delivery
detector anchored to the conversation's own contents.

    pass counts        19, 16, 17 of 22
    stable / flipped   15 / 7
    coverage           1.0 on all three
    delivery           66 of 66 "confirmed" -- BUT SEE BELOW, that grade is composite

**THIS REVERSES THE EARLIER CONCLUSION.** An earlier probe found eight of nine failures with
no prompt in the conversation and called non-delivery the leading explanation for why episodes
flip. Across 66 turns here the detector answered for every one and confirmed every one, and
all 14 failures were delivered:

    failures 14  ->  delivered 14,  not delivered 0,  unknown 0
    why_they_flip: fails_without_delivery = []   varies_with_delivery = 7 episodes

**AND "66 OF 66" WAS A MISREPORT.** `delivery: confirmed` is a COMPOSITE grade, and the raw
rows say what actually produced it:

    the prompt was found in the conversation   59
    the workdir changed                         7

So the conversation check answered for 59 of 66 (89.4%); the other seven were rescued by a
filesystem fallback and the conversation outcome for them is not in this file at all. The
headline was quoted as though the marker had been found 66 times. It was found 59 times.

What survives is the part the conclusion rests on: all 14 failures are among the 59, so every
failed turn did have a rendered user bubble carrying its marker.

**AND THE ANCHOR CONTRIBUTED NOTHING TO THIS RESULT.** Every one of those 59 took the positive
shortcut — the marker is looked for FIRST, and finding it returns immediately, before the
truncation check and before the anchor comparison. So calling this "the anchored detector's
result" is wrong: the anchor is what makes a NEGATIVE trustworthy, and this run produced no
negatives. The anchoring work is not validated by these numbers.

The detector is not abstaining its way out of the question — it is answering all of it. The
old one stopped at the first `ok` response whatever it contained, so a view that had not
finished rendering read as "the marker is not here", which read as "the turn never arrived".
It now keeps looking while the marker is missing. So the phenomenon that was going to be
written up as a transport fault was a hydration race in the check.

That matters for what to do next, not only for the record. A flip that happens WITH delivery
cannot be fixed once in the harness; it is variance in the target, and the only remedy is
repetition — which multiplies the cost of every future A/B by k. The cheap explanation was
the wrong one.

**WHAT A RENDERED MARKER DOES AND DOES NOT PROVE.** `/history` scrapes `chatQuestion` inside
rendered turn blocks, not the composer, so this is stronger than "the text stayed in the box".
It is still a same-page UI acknowledgement. It does not establish that the backend admitted
the request, associated it with the intended conversation, or consumed it — an optimistically
rendered user bubble whose submission was then rejected looks identical from here. So the
defensible claim is "the page eventually rendered this turn's prompt", and everything after
UI submission — tool transport, consent state, reply capture, fixture and grader determinism —
remains a candidate for the failures. "Variance in the target" is narrower than the evidence.

**AND THE RETRY IS OPTIONAL STOPPING.** The loop cannot re-send, but it can keep looking until
the marker appears, and the observation window is far wider than "six attempts two seconds
apart" suggests: each `/history` can poll internally for up to 30 seconds and the scroll pass
is bounded at 45. None of the saved rows records attempt count, first-attempt state,
confirmation latency, truncation or anchor status — so it cannot be told from this file
whether the 59 appeared immediately or were found minutes later.

Milestone A is still not met: 15 episodes stable against the 18 required. AND THE SPREAD
CRITERION IS ALSO FAILED, which the first write-up did not mention: 3/22 = 13.6% against the
preregistered 0.10. The earlier run's 2/22 = 9.5% passed it. Reporting only the stability
miss made this look like one criterion short when it is two.

**"15 STABLE VERSUS 13" IS NOT DEMONSTRATED IMPROVEMENT.** 48/66 passes before, 52/66 now —
about 6 points, which a naive independent-binomial check puts at p ≈ 0.42. Only three flipping
episodes overlap between the two runs, and the rest period differed (15 minutes then, 10 now).
Three trials also call a true 50/50 episode "stable" a quarter of the time.

## Milestone A's two criteria are met, and three trials is a thin basis for saying so

`reliability_logged.txt` -- 3 repeats, rested 600s, reshuffled, with every `/history` attempt
recorded.

    pass counts        18, 20, 18 of 22
    stable / flipped   18 / 4          (Milestone A requires 18)
    spread             2/22 = 0.091    (requires <= 0.10)
    coverage           1.0 on all three
    marker seen        63 of 66; the other 3 rest on a workdir change

Both criteria pass. What that is worth: a genuinely 50/50 episode looks "stable" in three
trials a quarter of the time, so 18 of 22 is consistent with several coin-flips landing the
same way. The earlier run was 15, 16, 17 (mean 16.0) against 18, 20, 18 (mean 18.7) here --
the direction is consistent but three points either side is not a demonstration. The honest
statement is that the suite now MEETS the bar it was given, not that it has been shown to sit
comfortably above it.

**AND THE HYDRATION EXPLANATION IS REFUTED BY THE INSTRUMENT BUILT TO TEST IT.** `shadow_rules`
scored the old rule and the new one over the same 66 turns:

    rescued  0        turns absent on the first `ok` look and present later
    agreed   63 found, 3 unknown

Forty turns did need more than one look -- but those were BUSY retries for the page lock, not
a view that had not rendered. So "the old detector had a hydration race" is unsupported by
this sample, and the earlier probe's eight-of-nine absences still have no explanation. The
tool was written to be able to say that, and it said it.

**A RATE ABOVE 1 WAS PRINTED IN THIS RUN.** `delivery_rate_where_answered` came out as 1.0476
and 1.1: the numerator counted rows the filesystem vouched for while the denominator counted
only rows the conversation check answered, so the two halves were measuring different sets.
It is 1.000 with the denominator fixed. A rate over one is not a rounding artefact, and it was
sitting next to figures a reader is asked to trust.

## Milestone B: the loop closes, and running it found three things reading it had not

`milestone_b_closed_loop.txt` -- one full turn through the controller against the fleet, four
paired episodes, 870 seconds.

    DECISION   REJECT      <- and this was WRONG; see below
    gate       n=4, b=0, c=0, both=4, neither=0
    security / regression / sentinel / infra   all reported, no regressions
    paired_ids 4
    hypothesis ledger       2 entries (proposed before the result existed, concluded after)

Every stage ran: a hypothesis recorded before any data, both arms against a target that
applies the manifest, every gate answering, and a named state rather than a pass/fail.

**THE VERDICT WAS WRONG, AND THE PREDICTION CAUGHT IT.** Written before the run: "no change;
expected verdict INCONCLUSIVE at this N". The loop said REJECT -- "the measurement says the
change is not an improvement". With b=0 and c=0 not a single pair disagreed, and McNemar reads
only the pairs that disagree; concordant pairs cancel. So the sample had no power at all, and
"the change did nothing" and "this sample could not have detected anything" are the same
observation. Only one of them is a statement about the candidate.

The power check missed it because `n < min_n` counts PAIRS, and four pairs clears any small
threshold. The quantity that has to be large enough is the DISCORDANT count. Zero discordant
pairs is now `underpowered`.

Two more, both found by running rather than reading:

  The baseline arm was checking its children against the CANDIDATE's harness id, because the
  expectation was set by the last preflight attestation and never updated when the arm
  changed. Every baseline episode became infrastructure and the loop aborted with "no episode
  ran on both arms". Recorded in the commit history; fixed before the run above.

  A controller with no archive returned the same value as a successful write, so an
  experiment could be kept -- and activated -- with no durable record and nothing saying so.
  Three existing tests asserted that behaviour and passed, including one named
  `test_a_working_archive_still_activates` which had no archive at all.

So Milestone B's pipeline is demonstrated end to end. The one verdict it produced was
incorrect, is now corrected, and the correction is the reason to write predictions down first.
