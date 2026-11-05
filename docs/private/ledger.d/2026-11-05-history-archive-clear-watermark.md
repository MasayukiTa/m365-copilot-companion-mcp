## 2026-11-05 - archiving completed history must also stamp the clear watermark (the trim that came back)

- Problem. The owner asked for the 500+ completed fleet runs to be moved out of the on-screen
  history into the archive and kept out. Moving them is not sufficient: `.fleet/history.json` is
  NOT the source of truth -- it is rebuilt from the append-only `.fleet/socket_route.jsonl` by the
  WPF cockpit at startup (`ui/FleetCockpit.History.cs` ArchiveTerminal, each idle tick) and by
  `python -m tools.rebuild_history`. Measured 2026-11-05: 504 completed rows pulled out of
  `history.json` were back ~45 seconds later. The live writer is `FleetCockpit.exe` (pid 8596),
  and `socket_route.jsonl` (75 MB) was still being appended to while this was diagnosed.

- Root cause. `relay/fleet_retention.history_archive` wrote the gzip archive under `.fleet/archive`
  and trimmed `history.json` to the most recent `keep` (default 200) -- but recorded nothing in
  `.fleet/history_cleared.jsonl`. That file is the cockpit's durable record of every 履歴を空にする,
  and `tools.rebuild_history.cleared_through` withholds a reconstructed row only when its `ts` is
  at or before the newest entry there. With no watermark, every archived row is reconstructed from
  the append-only ledger on the next rebuild. The cockpit protected the BYTES (rename-aside) and
  the DECISION (the watermark) for a hand clear; the retention trim protected neither.

- Why ts, not a per-row delete. The watermark is a wall-time mark: everything a clear discarded had
  already finished when it fired, so "older than the mark" is the same set as "was on screen then".
  `history_archive` moves the OLDEST terminal rows, so the max `ts` among the moved rows sits
  exactly between what left (<= mark, withheld) and what stayed (> mark, survives). This mirrors the
  rule the cockpit's own clear uses and needs no new field or format.

- Change. After the atomic `os.replace(tmp, history.json)` succeeds, `history_archive` appends one
  record `{ts, iso, rows, by:"relay/fleet_retention.history_archive"}` to `history_cleared.jsonl`,
  where `ts` is the max of the moved rows' `ts`. Same basename (`HISTORY_CLEARED_LOG_NAME =
  "history_cleared.jsonl"`, matching `tools.rebuild_history.CLEARED_LOG_NAME`), same one-JSON-per-
  line shape, same no-BOM UTF-8 + `\n` as cockpit/clear_history. Guards: no move -> no watermark
  (under-cap and dry_run write nothing); no moved row with a usable `ts` -> no watermark rather than
  a false `0.0`; the append failing (OSError) does not fail the trim -- the archive and trim already
  succeeded, the next sweep re-stamps.

- Invariants preserved. `_hiddenKeys` / the 完了 cards are untouched, so clearing/trimming history
  never retires a completed card (consistent with `ui/test_clear_history_keeps_the_completed_cards`).
  A later hand clear still wins: `cleared_through` takes the newest entry whichever writer wrote it,
  so a clear after an archive withholds the whole store. The two writers coexist in one append-only
  log.

- Tests. New `relay/test_history_archive_stamps_the_clear_watermark.py` (registered in ci.yml):
  archived rows do NOT return on a rebuild from a socket ledger that still holds them while the kept
  rows do; the watermark is the max moved `ts` in the existing jsonl/no-BOM shape and APPENDS to a
  prior cockpit clear; under-cap and dry_run write no watermark; a later clear withholds everything.
  Local: 64 passed (new + test_completed_jobs_move_to_archive_then_leave_history + test_fleet_
  retention + test_a_cleared_history_stays_cleared). PR #455.

- Not done here, on purpose. The running cockpit was not stopped and the live `.fleet` was read
  only -- no production history was hand-edited, because the append-only ledger would undo it; the
  watermark is the only durable lever and it lands through this retention path once merged and the
  cockpit is restarted (ui/rebuild_ui.ps1 / relaunch FleetCockpit.exe).
