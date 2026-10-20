## 2026-10-20 - Campaign board, read side and the coordinator's pointer rows (default off)

- Change: setting `agent_board_mode` (off|p0|p1, default off, sweep_start: read once when a
  coordinator starts). New `relay/agent_board.py` (schema validation, append, read, digest;
  stdlib only) and `tools/board_tool.py` (`board_digest`, read-only, no arguments; the campaign is
  decided from the calling session, never from an argument). `board_digest` is listed in
  `relay/fleet_toolset.py` with its reason and registered gateway-only in `main.py`.
- Behaviour: in p0, when a child ends DONE the coordinator appends one `result` row to
  `.fleet/board.jsonl` that points at where the answer is (a task id, or a path when one is
  known); no child text is copied. The merge prompt gets two pointer lines. In off nothing is
  read or written and the merge prompt is unchanged to the character. p1 is declared and not
  implemented (it behaves as p0); workers cannot write.
- Limits: four kinds (claim, result, fact, question), an unknown key is refused, 60 entries per
  campaign, 400 bytes per stored row, per-worker ceilings per kind (not applied to the
  coordinator), entries expire (claims sooner) and stay on disk for audit.
- Cockpit: an off/p0 selector in the Fan-out section (SaveKey only), in-effect line from the
  additive status.json field `agent_board: {mode, posted, refused, write_errors}`.
- Tests: relay/test_agent_board.py, tests/test_agent_board_agrees.py.
- Not built here: closing a campaign's entries when its merge finishes (expiry covers it), a
  setting for the digest length (it is a function argument, 600 characters), worker writes.
