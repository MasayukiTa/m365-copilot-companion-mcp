## 2026-10-02 - Fan-out identity on turn windows and tool events

- Change: the coordinator's turn_context rows (open/close) now also carry campaign_id, task_id,
  parent_task_id, root_id and role when the worker is a fan-out child (tools/turn_context.py
  fanout_identity, written from relay/relay_fleet.py). tools/tool_ledger.py copies them onto a
  call row labelled by window as campaign_id, subtask_id, parent_task_id, root_id, role.
  scripts/sibling_dup_report.py joins on them first and falls back to the status.json join.
- Behaviour: existing task/worker/attr semantics unchanged; ambiguous windows stay ambiguous and
  get no identity; a merge worker's calls are not sibling calls. Size caps unchanged.
- Tests: tools/test_job_to_campaign_identity.py (registered in ci.yml).
- Open: re-run the report on a live fleet once enough campaigns accumulate.
