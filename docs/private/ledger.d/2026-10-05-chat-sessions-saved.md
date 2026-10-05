## 2026-10-05 - A chat turn is always stored, or the window says it was not

- Report (owner): chatting from the cockpit main window continued after 9/18, and the `sessions`
  table has no row after 9/18. That is a defect, not "no chats".
- Findings (counts only). `.fleet/sessions/sessions.sqlite3`: 558 `sessions` rows, newest created
  2026-09-18, per-day creations 9/6:5, 9/8:1, 9/9:1, 9/10:1, 9/13:1, 9/18:3; `turns` newest
  09-18 (and the 9/18 bridge fix notes `store.turns` already stuck at 09-10 for real chats).
  `%APPDATA%\copilot-bridge\chats\*.chat`: 3 files, newest 2026-06-18. Registry
  `.fleet/conversations.json`: 21 `chat` rows (7/7-9/18), the rest are fleet rows.
  The window's follow-ups into a past (fleet) conversation do NOT go through the bridge /stream:
  `ChatSend.SendToFleetConversation` writes an `add_goal` command, and the fleet records the
  turns in `fleet_turns` and the transcript files. `fleet_turns` holds follow-up goals with the
  window's framing on 9/24 (53 conversations), 9/25 (26), 9/26 (7), 9/28 (1), 9/12-9/16 earlier.
  Those words are on disk and absent from `sessions`.
- Cause (three independent gaps; no commit ever removed a writer - `git log -S` finds the
  writers unchanged since 2026-07-06):
  1. `_persist_exchange` wrote the user line and the reply as two autocommit appends inside
     `except Exception: logger.warning(...)`: a lock, a full disk or a bad id left the ledger short
     with one line in bridge.log; a turn that raised or came back empty recorded nothing at all.
  2. The window's `SaveConversation` was `catch { }` and ran only when an answer came back through
     /stream; a sent line was never saved on its own.
  3. Fleet follow-ups and capacity-rerouted sends reach the fleet, never `sessions`.
- Change: `session_store.record_exchange` (one transaction, session row first because turns.sid is
  a foreign key, idempotent by `expect_turn` / recent-duplicate); `_record_exchange_durably`
  (retry x3, spill to `.fleet/chat_persist_failures.jsonl`, ERROR log, `(ok, reason)` result);
  every /stream turn ends with a `persist` or `persist_error` event with asked/written/failed
  counts (self-check); a turn that raised or was empty still keeps the typed line. Window:
  `SaveConversation` returns bool and shows "この会話は保存されていません: <reason>" / "This
  conversation was not saved: <reason>" once per reason, logs `chat_save_errors.log`; the sent
  line is saved at send time; the bridge's `persist_error` is shown too.
- Recovery: `scripts/backfill_chat_sessions.py` (dry run by default, `--apply`, one transaction,
  skips existing ids, refuses under 1.5 GB free or a locked store, counts only). Run it on the
  live store only after the PR is merged and the product is idle.
- Tests: `bridge/test_a_chat_turn_is_always_stored.py`, `bridge/test_backfill_chat_sessions.py`,
  `ui/test_a_chat_that_was_not_saved_says_so.py` (all in CI).
- Open: retention. `session_retention_days` defaults to 90 (changed from "forever" on 2026-09-24):
  chats older than 90 days leave `sessions` on every pass. Recommend raising it, or exporting
  before pruning; the constant is NOT changed here. A fleet follow-up written into `sessions` by
  the backfill is a copy; the window still lists those from transcripts. Not covered: a
  capacity-rerouted plain `add_goal` (no follow-up framing) cannot be told apart from the fleet's
  own goals, so it is not backfilled.
