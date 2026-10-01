## 2026-10-02 - Per-tree fan-out budget (behaviour unchanged at default)

- Change: `relay/fanout_budget.py` adds `grant()` and `usage_from_status()` plus helpers. A tree
  (root campaign and descendants, keyed by `root_id`) is bounded by total workers, active
  workers, turns and wall-clock minutes. Four each_gate settings: `fanout_max_total` (24),
  `fanout_max_active` (3, absent follows `maxtabs`), `fanout_max_turns` (400),
  `fanout_max_wall_min` (120). The worker's split path asks the coordinator's `grant` before
  building children; a refusal takes the existing run-directly branch (not STUCK), a partial
  grant folds the tail steps into the last kept child (never fewer than two children).
  `status.json` gains `fanout_budget` (limits in force) and `tree_budget` (per-root usage,
  capped at 20). The cockpit gets four numeric boxes beside the fan-out selector, persisted via
  SaveKey and showing the limits the coordinator reports.
- Behaviour: at depth 1 a root splits once, so at the default limits every grant equals the
  request; `tests/test_fanout_budget.py` compares the steps and children before and after for
  every split size 2..12. Unknown usage (unreadable ledger, unreadable turn counts or start
  time) fails closed. An already-adopted family is not charged because it queues nothing.
- Tests: `tests/test_fanout_budget.py` (registered in ci.yml), reader entries in
  `tools/test_a_setting_declares_when_it_takes_effect.py`.
- Open: depth greater than 1 is not enabled; the budget is the guard that slice will rely on.
  The top-level parent's own turns are not charged to its tree (it carries no root id).
