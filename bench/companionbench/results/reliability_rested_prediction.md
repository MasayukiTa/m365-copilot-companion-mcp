# What the rested-tenant run has to show, written before it finished

Recorded before the numbers arrived, because a criterion chosen afterwards fits whatever
turned up. The previous two rounds each ended with a story that explained the result; a story
is worth less than a threshold nobody could move.

## The question

Three repeats of the same suite, same system, nothing changed between them. Is the suite a
measuring instrument yet?

## What "yes" looks like

All three of these, or it is not:

1. **Rate spread ≤ 0.10.** The pass RATE, not the count, because the denominators can differ
   once infra is classified properly. Two rounds so far: 13/6/8 (spread 7 of 22 = 0.32) and
   7/17/19 (spread 12 of 22 = 0.55).
2. **No monotone trend.** 7 → 17 → 19 is the shape that says the tenant, not the system, is
   what changed. A trend of that size in three points is not something to average away.
3. **Stable episodes ≥ 18 of 22.** Currently 9. An episode whose verdict is a coin flip
   contributes noise to every comparison built on it, and 22 episodes with 13 of them
   flipping cannot resolve a harness difference of any plausible size.

## What each failure mode would mean

- **Infra count rises, pass rate steady** — the rate-limit classification is working and the
  tenant is still throttled. The measurement is honest; it just cannot be run this often. The
  fix would be pacing, not code.
- **Pass rates low and flat, few infra** — the companion genuinely fails these episodes. That
  is a capability result and the suite is fine; the episodes are just hard.
- **Spread still large, no trend, few infra** — genuine per-turn nondeterminism. Then repeats
  have to be built into the design: every episode run k times and scored on its rate, which
  multiplies the cost of every future A/B by k.
- **Another monotone trend** — something else is warming or degrading across a run, and it has
  not been identified. That is the worst outcome, because it means a third unknown after two
  were found and fixed.

## What this run cannot settle regardless

22 episodes measured three times is 22 units, not 66. Repeats buy precision about each
episode's rate, not more episodes. A suite that is stable here is stable on THESE episodes
against THIS target on ONE evening; it says nothing about a different target, and the sealed
pool exists precisely because the other two pools have been looked at too often.
