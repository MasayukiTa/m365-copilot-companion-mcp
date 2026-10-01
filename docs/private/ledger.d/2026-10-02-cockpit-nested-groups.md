## 2026-10-02 - Cockpit shows nested split groups, tree usage and the depth cap

- Change: `GroupTreeView` (ui/EffortPolicy.cs, WPF-free) words the split-group line on a fan-out
  parent card from status.json `groups`; `ui/FleetCockpit.cs` only calls it. A nested group's line
  is indented by `depth`; the line adds `sub-groups: N` (and the count below it) and the
  `waiting_on_subgroups` / `unknown` merge words, and says when a parent group was not found. A
  root group gets a second collapsible line with used/limit for total, active, turns and minutes
  from `tree_budget` (warning colour at 80% or more, error colour at 100% or more) and, when
  `fanout_depth.effective` is below `configured`, "depth capped at N: hierarchical merge not enabled".
- Behaviour: every new field is optional. A flat status.json renders the same text as before and
  gets no second line. The goal text is never shown (only the capped ledger strings already used).
  No popup, no toast.
- Tests: compiled-harness cases in ui/test_the_effort_policy_says_what_is_in_effect.py (flat
  unchanged, two-level nesting, orphan, usage at 50/85/100%, capped depth, missing fields) plus a
  source check on the card builder.
- Open: none.
