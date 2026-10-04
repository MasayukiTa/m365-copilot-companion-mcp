## 2026-10-05 - Nested group / task tree linkage from durable campaign headers

- Problem: on a real depth-2 fleet, status.json groups showed parent_group_id None / depth 0
  because the link came from live slot rows, which are archived once finished; scripts/tree_report.py
  showed max depth 1 and 155 orphans because producer rows have an empty task_id and children name
  their root parent by campaign id.
- Change: `relay.family_view._link_parents` also resolves the parent group from the campaign
  header (parent_campaign_id), even when the parent group has no live rows (ancestor chain is
  walked through headers). `relay.task_tree.build_tree` materialises missing parents as virtual
  nodes from the headers (root = campaign id, slot = parent_task_id of a nested header). Rows with
  no identity beyond a name are counted as unplaced, not roots or orphans. An orphan is reported
  only when a claimed parent is named by nothing durable.
- Numbers on an archived depth-2 run: tree max depth 1 -> 2, orphans 155 -> 0; with finished slot
  and parent rows pruned, groups with a parent 0 -> 23 of 34.
- Tests: relay/test_family_view_nesting.py, tests/test_task_tree.py (both already in ci.yml).
