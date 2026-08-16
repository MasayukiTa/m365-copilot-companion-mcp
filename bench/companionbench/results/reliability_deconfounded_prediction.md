# Criteria for the deconfounded run, written before it finished

Third batch of three repeats. The first two are not usable as reliability estimates: both ran
back to back in a fixed episode order, so a trend across the runs is confounded with the
tenant's state over the same period, and each episode's position in that period was fixed.
This one rests 15 minutes between runs and shuffles the order.

Recorded in advance because the previous two rounds each ended with a story that explained the
result, and a story is worth less than a threshold nobody can move afterwards.

## What "the suite is a measuring instrument" requires

All three, or it is not:

1. **Rate spread ≤ 0.10.** On the pass RATE, since denominators can now differ. Previous:
   0.32 (13/6/8) and 0.59 (7/20/16).
2. **Stable episodes ≥ 18 of 22.** Previously 3, then 9, then 7. An episode whose verdict is
   a coin flip contributes noise to every comparison built on it.
3. **Coverage ≥ 0.95 in every run**, and capability equal to end-to-end within 0.05. A gap
   between those two means environment failures are being excluded, and the size of the gap
   is how much of the capability figure is that exclusion.

## The question this run can newly answer

Every row now carries `delivery_confirmed` — positive evidence the prompt arrived, from a
change under the episode's unique workdir. So for each episode that flips:

- **Flips WITH delivery confirmed every time** → the companion genuinely varies. That is a
  property of the target, the suite is measuring it correctly, and the remedy is repeats
  built into the design (every episode run k times, scored on its rate), which multiplies the
  cost of every future A/B by k.
- **Flips WITHOUT delivery confirmed on the failing runs** → the prompt did not arrive, the
  failure is transport, and the remedy is in the harness rather than in the statistics.
- **Mixed** → both, and the two have to be separated before either can be fixed.

The third is the likely one and is the least convenient, so it is written down first.

## What this run cannot settle regardless

22 episodes measured three times is 22 units, not 66. Repeats buy precision about each
episode's rate, not more episodes. Three observations give a very coarse per-episode rate: an
episode that is genuinely 50/50 will show 3-0 or 0-3 about a quarter of the time, which looks
perfectly stable. So "stable" here means "did not visibly move in three tries", and a suite
that passes criterion 2 has cleared a low bar rather than a high one.

Shuffling removes the position effect but not the tenant's state: all three runs still happen
within a few hours of each other, on one tenant, on one day.
