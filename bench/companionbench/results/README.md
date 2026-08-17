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
