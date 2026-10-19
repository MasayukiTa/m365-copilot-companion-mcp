## 2026-10-20 - Load-aware routing (C3), shadow only

- Change: setting `fanout_load_routing` (off|shadow, default off, each_gate). New module
  `relay/load_routing.py`. At each admission in `run_relay_fleet` (after the goal is chosen,
  before it is attached) it appends one row to `.fleet/load_routing_shadow.jsonl`: the goal
  actually admitted, the goal a router using the remaining admission margins (lane cap slots,
  rate gate) and the role profile (`scripts/capability_profile.py load_profiles`, C1) would have
  admitted, and the margins. Tight margins prefer the cheapest known goal, loose margins the most
  expensive known one; a comparison needs both costs known (unknown is not cheap); ties keep queue
  order.
- Behaviour: SHADOW ONLY. Nothing is reordered; prompts and outputs are unchanged. In off nothing
  is read or written. A write error is counted and swallowed. The log rotates at 1 MiB to `.1`.
  There is no value that routes.
- Report: `scripts/load_routing_report.py` (read-only) says how often the shadow choice differs
  and prints the threshold for "enough data" (200 rows with a choice, 30 comparable, 5 runs, 2
  roles with a known cost). Enough data and >= 10% differing means "run the paired comparison
  (C0)", not "switch on": a shadow row has no outcome for the goal that was not run.
- Cockpit: an off/shadow selector in the Fan-out section (SaveKey only), in-effect line from the
  additive status.json field `fanout_load_routing: {mode, recorded, differed}`.
- Tests: relay/test_load_routing.py (rule, recorder, bound, failure safety, and an end-to-end
  run proving the real admission order and what each worker was built with are identical with the
  mode off, shadow, and shadow with an unwritable log), tests/test_fanout_load_routing_agrees.py,
  scripts/test_load_routing_report.py, tools/test_a_setting_declares_when_it_takes_effect.py.
- Open: only producers carry a mean turn count; subtasks and aggregators have none, so their cost
  stays unknown and the rule can only compare a producer with another producer role. Expect a low
  comparable count until child turns are recorded per role.
