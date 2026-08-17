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
    delivery           66 of 66 CONFIRMED -- zero unknown, zero denied

**THIS REVERSES THE EARLIER CONCLUSION.** An earlier probe found eight of nine failures with
no prompt in the conversation and called non-delivery the leading explanation for why episodes
flip. Across 66 turns here the detector answered for every one and confirmed every one, and
all 14 failures were delivered:

    failures 14  ->  delivered 14,  not delivered 0,  unknown 0
    why_they_flip: fails_without_delivery = []   varies_with_delivery = 7 episodes

The detector is not abstaining its way out of the question — it is answering all of it. The
old one stopped at the first `ok` response whatever it contained, so a view that had not
finished rendering read as "the marker is not here", which read as "the turn never arrived".
It now keeps looking while the marker is missing. So the phenomenon that was going to be
written up as a transport fault was a hydration race in the check.

That matters for what to do next, not only for the record. A flip that happens WITH delivery
cannot be fixed once in the harness; it is variance in the target, and the only remedy is
repetition — which multiplies the cost of every future A/B by k. The cheap explanation was
the wrong one.

Milestone A is still not met: 15 episodes stable against the 18 required.
