## 2026-11-09 - Load-aware routing (C3), step 2: paired comparison (on vs queue order)

- Change: new read-only script `scripts/load_routing_pairwise.py`. It is the paired comparison the
  shadow report (`scripts/load_routing_report.py`, C3 step 1) names as the next step: the report can
  only say how OFTEN the router would have chosen differently, not whether acting on that choice was
  BETTER, because the goal it did not admit was never run in its place. This script pairs an `on`
  run (the router reordered admission, `fanout_load_routing=on`, already on main from #482) with an
  `off`/`shadow` run of the SAME goal (queue order) and measures each side from the same telemetry
  `scripts/ab_compare.py` reads (success, turns, wall_s, tool_calls, retries; deltas are B minus A).
- Why not ab_compare alone: ab_compare pairs two conditions by experiment id but cannot tell an
  `on` run in which the router actually reordered admission from one in which it AGREED with queue
  order (nothing to measure). The `on` mode writes one shadow row per admission with mode="on" and
  differs=true/false; this script reads those rows per run_id and refuses a B side that never acted
  (`router_did_not_act`), and refuses an A side that actually reordered (`mislabelled`). That guard
  is the whole reason it is a separate script and not a manifest handed to ab_compare.
- Behaviour: READ-ONLY. It schedules nothing, submits nothing, writes nothing; running the two
  conditions is the operator's job. Manifest is the SAME shape ab_compare accepts (one entry per
  goal+condition), with run_id made required because the shadow rows are keyed by run_id. Verdict is
  INSUFFICIENT when complete pairs < --min-pairs (default 5), else DIRECTION_ONLY. As in ab_compare
  there is NO significance test, and the text says so every time: with a handful of goals the
  numbers show which way the conditions leaned, not that the difference is real.
- Tests: scripts/test_load_routing_pairwise.py (16 cases: the shadow-row reader counts only mode=on
  rows and needs a differs row; a missing log is empty not an error; side labelling of B-never-acted,
  B-no-on-rows, A-reordered, and a clean pair; an end-to-end known pair measures the delta and
  counts it as acted; B-never-acted and five-such-goals are incomplete not measured; too-few-acted
  is INSUFFICIENT; a mislabelled A side drops the goal; experiment_id mismatch blocks the pair; the
  manifest is the ab_compare shape; main is read-only and emits JSON then text; the script and the
  recorder agree on the log name; the two scripts share the same metric set). Registered in the
  hermetic CI list in .github/workflows/ci.yml.
- Conclusion: the C3 measurement chain is now complete end to end -- step 1 records what the router
  WOULD do (shadow) and how often it differs; step 2 measures what acting on it DID, paired on the
  same goal, with the router-acted guard ensuring a complete pair always has a B side that genuinely
  reordered admission. No routing decision is switched on by this slice: the script produces the
  evidence a switch-on would need, and withholds a verdict stronger than DIRECTION_ONLY until the
  operator has run enough acted pairs. The on-mode from #482 remains the only thing on main that can
  reorder, and it is still gated behind the operator setting.
- Open: a complete pair needs a real `on` run that reordered AND an `off`/`shadow` run of the same
  goal under the same experiment id; until the operator collects >= --min-pairs such pairs the
  verdict stays INSUFFICIENT by design. The producer-only cost limitation from step 1 still bounds
  how often the router has two known costs to compare, so acted pairs will accrue slowly.
