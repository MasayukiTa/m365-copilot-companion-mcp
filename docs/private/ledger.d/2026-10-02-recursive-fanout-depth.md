## 2026-10-02 - Nested fan-out depth plumbing (behaviour unchanged: effective depth stays 1)

- Change: new each_gate setting `fanout_max_depth` (1..3, default 1) read by
  `relay/fanout.py:configured_max_depth`. The three gates that forbade a child from splitting are
  now "depth below the effective maximum", read at each decision: `fanout.child_goals`
  (was `depth >= MAX_DEPTH`), the worker constructor's `_depth0`/`_fanout_capable`, and the
  mid-run split offer in `relay/relay_fleet.py` (re-reads the depth). A merge worker never splits
  (role guard). `aggregation_goal` takes the splitting worker's depth plus one. A nested split
  passes depth, `parent_campaign_id`, `parent_subtask_index`, `root_id` to `child_goals`, and its
  grant is charged to the tree's root with the requester excluded from the active count. A
  grandchild's goal carries its own scope block and only the immediate parent's step as
  reference; `_split_scope_block` takes the last block. A nested campaign header records
  `depth`; resume (`fleet_resume.py`) and the merge queue read it (absent means 1).
- Guard: `fanout.HIERARCHICAL_MERGE_READY = False`. A child that ends FANOUT counts as finished and
  its proposal text would be read as its answer by the parent merge, so `effective_max_depth()`
  is `min(setting, 1)` until that exists. `status.json` gains `fanout_depth`
  {configured, effective, reason} (written by `fleet_runner._snapshot`); the cockpit gets a 1/2/3
  selector beside the fan-out box and an "in effect" text that says when the setting is not active.
- Behaviour: at default settings, and at 2 or 3 with the guard off, every output equals the
  previous implementation (digest compare over child_goals, aggregation_goal, ledger reader).
- Tests: `relay/test_recursive_fanout_depth.py` (registered in ci.yml); reader entry in
  `tools/test_a_setting_declares_when_it_takes_effect.py`.
- Open: the hierarchical merge must exist before the guard is turned on; the nested merge's
  prompt still treats the splitting child's own text as the family goal.
