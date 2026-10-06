## 2026-10-02 - Sibling write scope, shadow only

- Change: setting `fanout_write_scope` (off|shadow, default off, each_gate). New pure module
  `relay/write_scope.py`: `declared_scope` reads the explicit paths / file names a child's slice
  step names; `overlaps` finds a write by one sibling to a path another sibling's step names, or
  a path two siblings both wrote. Write-class calls are found by tool name in tool_events rows
  (write_file, append_file, replace_in_file, multi_edit, edit_and_verify); run_python /
  shell_exec are counted as unknown writes and never flagged. Rows whose attribution is not
  trustworthy, or that resolve to no single child, are skipped and counted.
- Behaviour: SHADOW ONLY. In shadow, when a child finishes, the coordinator sweep scans that
  campaign's recent tool_events and records one `scope_overlap` mechanism row per new overlap.
  Nothing is blocked; prompts and outputs are unchanged. In off nothing is read or recorded.
  There is no enforcing value: enforcement would pass through the folder policy, which is
  frozen, and needs its own approval. Cockpit: an off/shadow selector next to the split depth
  (SaveKey only), with an in-effect line from the additive status.json field
  `fanout_write_scope: {mode, overlaps_seen}`.
- Tests: relay/test_write_scope.py, tests/test_fanout_write_scope_agrees.py (registry / cockpit
  parity, GUI wiring source checks, cockpit build), tools/test_a_setting_declares_when_it_takes_effect.py.
- Open: attribution is limited by the turn-window labels; most calls in a busy fleet stay
  ambiguous and are skipped, so the counts are a lower bound. Campaign identity fields on
  tool_events rows are used when present (none are written yet).
