# Facts with more than one reader

The four incidents below are the reason this document exists. Each is one fact — a
constant, a schema, a filename convention — that a writer changed while at least one
independent reader kept the old assumption:

| a writer grew a case | the reader nobody updated |
|---|---|
| transcripts started being gzipped (2026-09-01) | `ui/CopilotChat.cs` globbed only `*.jsonl` in three places — 1,741 of 1,747 conversations became invisible for two weeks |
| a third lock-refusal string was added (2026-08-18) | `relay/relay_fleet.py`'s comment still said "one of its two literal error strings" |
| `tools/lock_state.py` began recording the MCP session per refusal | `relay/relay_fleet.py::_looks_locked` still says "identity is the real fix and it is not available here" |
| an escalating retry nudge was added in `run_relay()` | the fleet path kept sending the raw constant — nine byte-identical nudges to one worker |

Two of these four are **already fixed in the current source** (rows 1 and 2 below say so
explicitly) — this repo moves fast enough that a fact found stale on Monday can be fixed
by Wednesday. That does not retire them from this list: a fix without a guard is a fact
that can drift right back, silently, the next time someone touches either side. The
question this document answers for each row is not "is it correct today" but "what stops
it from becoming wrong again, unnoticed."

Rows are sorted by **what breaks silently if the fact drifts** — a wrong classification a
human never sees before a decision is made on it, first; a cosmetic UI gap, last.

## The rows

### 1. MCP session identity on a lock refusal — currently unread, not just unguarded

- **The fact**: which MCP session a `require_unlocked()` refusal happened on.
- **Writer**: `tools/lock_state.py::record_locked` (`tools/lock_state.py:106-141`) —
  writes `payload["session"] = sess` at line 139 whenever `_session()` (`:67`) resolves
  one.
- **Readers**: `relay/relay_fleet.py::_looks_locked`'s "paraphrase" branch
  (`relay/relay_fleet.py:895-925`) carries a 2026-09-15 comment explaining that this field
  now exists and "discriminates" — but **the code in that branch never reads
  `record.get("session")`** (verified: no `.get("session")` or `["session"]` access
  anywhere in `relay/relay_fleet.py`). Eleven lines further down, the older "fallback"
  branch (`relay/relay_fleet.py:962-975`) still carries its original comment: *"Identity is
  the real fix and it is not available here."* That sentence is now false — the identity
  is available, two branches up, in the same file — and nothing enforces that the two
  comments (or the two branches' actual behavior) agree with each other or with
  `tools/lock_state.py`'s current schema.
- **Guard today**: none. No test asserts that a refusal's `session` field is ever
  consulted by the fleet's lock classifier.
- **What breaks silently if it drifts further**: under concurrency, several workers can be
  locked at once (`relay/relay_fleet.py:940-948` documents this as "the normal state of a
  fleet"); without session attribution, one worker's refusal can still colour another
  worker's unrelated long reply, exactly the failure mode row 2's dominance rule was built
  to bound. This is a live inconsistency, not a resolved incident — worth a maintainer's
  attention on its own, independent of this documentation task.

### 2. The three lock-refusal literal strings — the seed incident, now guarded (for one of three readers)

- **The fact**: the exact text `tools/security.py::require_unlocked()` returns for each of
  its three refusal shapes (no HTTP context, IP-unlocked-but-no-token, IP-never-unlocked).
- **Writer**: `tools/security.py:473-576` (three literal strings, ~line 493, ~562, ~570).
- **Readers**:
  - `relay/relay_fleet.py::LOCKED_MARKERS` (`relay/relay_fleet.py:826`) — **guarded**: see
    below.
  - `bridge/copilot_bridge.py:5857` keeps its **own** copy of just
    `NO_CONTEXT_REFUSAL = "[locked: no HTTP request context]"`, used in its own filter at
    `bridge/copilot_bridge.py:5847` — **not covered by the guard test**, which imports only
    `relay.relay_fleet` (`relay/test_every_refusal_the_server_can_speak_is_one_the_fleet_can_hear.py:55`).
- **Guard today**: `relay/test_every_refusal_the_server_can_speak_is_one_the_fleet_can_hear.py`
  re-reads `tools/security.py`'s source for every `"[locked"` literal and asserts
  `relay_fleet.LOCKED_MARKERS` covers each one
  (`test_every_locked_literal_in_security_py_is_covered_by_a_relay_marker`, line 95). This
  is the pattern the rest of this table is measured against — and it demonstrates the
  limit of a source-sweep guard: it swept one reader, not both. A fourth refusal string
  added to `tools/security.py` today would fail the relay-side test and pass silently on
  the bridge side.
- **What breaks silently if it drifts further**: `bridge/copilot_bridge.py`'s no-context
  filter stops recognizing the literal it filters on; a context-less refusal (the one this
  filter exists to discount as "not evidence about this worker") is no longer discounted
  there.

### 3. The `commands.d` / `commands.json` command protocol — three writers, one reader, no shared schema

- **The fact**: the shape of one command dict — top-level keys (`add_goal`, `steer`,
  `close`, `set_maxtabs`, `set_disk_floor_gb`, `set_ram_floor_mb`, `set_autoscale`, `ack`)
  and, within `add_goal`, item fields (`text`, `priority`, `jid`, `follow_up_to`,
  `resume_conv`, `cwd`, `checks`).
- **Writers**:
  - `relay/task_router.py::write_command` / `add_goal_to_live_fleet`
    (`relay/task_router.py:555-609, 632-660`).
  - `ui/CopilotChat.cs::AppendCommand` (`ui/CopilotChat.cs:4802-4820`) — `add_goal` and
    `steer`, plus `resume_conv`/`follow_up_to` for a follow-up
    (`ui/CopilotChat.cs:4855-4867`).
  - `ui/FleetCockpit.cs` (`close`, `set_maxtabs`, `set_disk_floor_gb`, `set_ram_floor_mb`,
    `set_autoscale`; header comment at `ui/FleetCockpit.cs:1-8` says only "release", but
    `_apply_command`'s key list below is wider than that comment describes).
- **Reader**: `relay/fleet_runner.py::read_commands` (`:1530`) and `_apply_command`
  (`:2225-2260+`) — the single place that must recognize every key any of the three
  writers might send.
- **Guard today**: none across the language boundary. The one-file-per-command scheme
  (`relay/task_router.py:555-575`) prevents two writers from **clobbering** each other's
  command, which is a different failure from a reader **not recognizing** a key a writer
  sends — `_apply_command` silently ignores an unrecognized top-level key
  (`cmd.get(...)` with no `else`), so a typo or a renamed key in either C# binary fails
  with no error anywhere.
- **What breaks silently if it drifts further**: a UI-side knob change (e.g. a renamed
  `set_ram_floor_mb`) would show as "the slider does nothing" with no exception in either
  process — this repo's own `feedback_always_go_through_the_gui` lesson (settings must be
  verified by going through the actual UI, not by reading the constant) is exactly the
  discipline that would catch this class, and exactly the one a schema test would make
  unnecessary to re-run by hand every time.

### 4. The escalating-nudge shape — two independent implementations of one contract

- **The fact**: "a retry/continue nudge sent 1-2 times uses the original constant
  unchanged; the 3rd and later sends rotate through distinct, count-tagged phrasing so no
  two consecutive sends are byte-identical."
- **Writers/readers** (each module both defines and consumes its own copy):
  - `relay/relay_fleet.py::_continue_nudge` (`:2399-2410`), for the fleet's per-worker
    `RelayWorker` loop.
  - `relay/copilot_autopilot_relay.py::_next_continue_job` (`:531-540`) and
    `_next_retry_job` (`:513-521`), for the single-conversation `run_relay()` loop — the
    seed incident in this document's header table.
- **Guard today**: each implementation has its **own** test
  (`relay/test_continue_escalation.py` for `relay_fleet.py`;
  `relay/test_stuck_tool_health.py:147-149` for `copilot_autopilot_relay.py`), and both
  currently pass. Neither test — nor anything else — asserts the two *stay the same
  shape* as each other. They could diverge (e.g. one drops the "never byte-identical"
  guarantee, or changes the count-2-vs-3 cutover) and both test suites would remain green.
- **What breaks silently if it drifts further**: exactly the seed incident recurs, but
  asymmetrically — one path keeps working, the other regresses, and nothing but a mined
  transcript count (as happened before) reveals it.

### 5. `status.json` snapshot schema — one writer, three readers, no cross-language check

- **The fact**: the field set `_snapshot()` produces — `running`, `updated`, `workers[]`,
  `queued`, `disk_floor_gb`, `ram_floor_mb`, `paused`, `directive`, `run_label`,
  `goal_count`, and per-worker fields including `plan`.
- **Writer**: `relay/fleet_runner.py::_snapshot` (`:1076`), called from `on_tick`
  (`:2306-2312`).
- **Readers**:
  - `ui/FleetCockpit.cs` — tails the file and renders one card per worker.
  - `relay/task_router.py::fleet_is_live` (`:522-537`) — reads `updated` (via mtime) and
    `running` to decide whether a new goal joins this run or waits.
  - `relay/fleet_runner.py`'s own watchdog thread (`:2391+`) — reads the same file back to
    detect a frozen sweep.
- **Guard today**: none that checks the C# side parses whatever the Python side currently
  writes. The two Python readers are at least in the same process family and would break
  loudly (an exception) on a missing key; the C# reader fails the way UI code usually
  fails a missing JSON field — silently blank, not a crash.
- **What breaks silently if it drifts further**: a renamed or restructured field in
  `_snapshot()` makes a cockpit card go blank for that field with no error surfaced
  anywhere a human would see it during normal operation.

### 6. Transcript filename convention — writer and reader agree only by comment

- **The fact**: `<run_id>_<agent>_<worker>.jsonl` (e.g. `r6aa8fc73_a0_w0.jsonl`), and its
  `.jsonl.gz` form once compressed.
- **Writer**: `relay/relay_fleet.py::_Transcript.__init__`
  (`relay/relay_fleet.py:1164-1184`) — `self.path = os.path.join(directory, key + ".jsonl")`
  where `key = "<run_id>_<name>"`.
- **Reader**: `ui/CopilotChat.cs` — matches by exact suffix, `"_" + worker + ".jsonl"`
  (`ui/CopilotChat.cs:1055`, with an explicit comment that `w1` must not match `w10`), and
  documents the naming convention in its own comment at `ui/CopilotChat.cs:1043`.
- **Guard today**: none automated; the convention is kept in sync only because both
  comments describe it in prose and nobody has changed one side yet. The `.gz` half of
  this exact fact (row 0 below) already drifted once.
- **What breaks silently if it drifts further**: a transcript silently stops appearing in
  the chat window for one worker, with no error — this is precisely the shape of the seed
  incident (row 0), just for the filename stem instead of the extension.

### 7. A fleet job's outcome — recorded in one ledger, joined by an id the ledger never carried

- **The fact**: whether an admitted fleet-bound job (`.fleet/tasks/done/<jid>.json`,
  `status="dispatched"` or `"awaiting_fleet"`) ever actually finished, and how.
- **Writer of the queue record**: `relay/task_router.py::run_job` / `fleet_handoff`
  (`relay/task_router.py`) — writes `done/<jid>.json` the instant a goal leaves the queue,
  with `status` frozen at "dispatched"/"awaiting_fleet" and `ts_done=None` forever after,
  because nothing revisited the record once the goal was handed to the fleet.
- **Writer of the actual outcome**: `relay/relay_fleet.py::RelayWorker.close()` — appends one
  line per finished worker (`outcome`, `turns`, `reason`, `status`) to
  `<state_dir>/socket_route.jsonl`, unconditionally, whether or not the C# cockpit is open to
  archive anything into `.fleet/history.json` (a *different* file that already carried the
  same join key — see below — but only when a person has the cockpit running to write it).
- **The join key that was missing**: `jid`, the admission-time id `task_router.py` mints per
  goal and already threads into the goal dict `RelayWorker` reads
  (`self.jid = goal.get("jid")`, `relay/relay_fleet.py:2559`). It already reached
  `history.json` and the final sweep snapshot (both added 2026-09-09, per the comment on
  `add_goal_to_live_fleet`), but the `_socket_route().record("worker_done", ...)` call at
  `relay/relay_fleet.py:3181` never carried it — measured directly: 0 of 4,035 `worker_done`
  rows in the live ledger had a `jid` field before this was fixed.
- **A sweep of the live queue, same day**: all 164 records in `.fleet/tasks/done/` read
  `status="dispatched"`, `ts_done=None` — including at least two goals delivered into a fleet
  run that was later stopped by hand and never finished. `done/`'s own module docstring
  already warned "DOES NOT MEAN THE WORK IS DONE"; the warning did not make the true outcome
  readable from anywhere the queue's own reader could reach.
- **Guard today**: `relay/test_a_job_marked_done_reads_as_dispatched_not_finished.py`, added
  in the same change that added `jid` to the `worker_done` record and taught
  `task_router.py::job_status()` / `_reconcile_outcomes()` to read it. Covers: a finished job
  reads as finished with its real outcome; a job whose run ended with no outcome ever
  recorded reads "unknown" rather than a false "dispatched" forever; a job still an active
  worker in the current run reads "in_flight"; and — the honest limit of the fix — a
  `worker_done` row with no `jid` (anything logged before this change, or a goal that never
  passed through admission) cannot be joined and is not guessed at via goal text, which is
  truncated to 600 characters in this ledger and duplicated across retries of one goal.
- **What breaks silently if it drifts further**: exactly the incident this row records — a
  new fleet-bound job type, or a new path that hands a goal to `RelayWorker` without setting
  `self.jid`, produces completions this join can never find, and `job_status()` degrades back
  to reporting "unknown" for jobs that actually finished, which is an availability problem
  rather than a wrong-answer one but is the same shape of silent drift as every other row
  here.

### 0. Transcript compression (`.jsonl` → `.jsonl.gz`) — the seed incident, now fixed, still unguarded

Listed last despite being the header incident, because it is **already fixed** and this
row exists to record that fixing it did not add a guard.

- **The fact**: a stored transcript may be a plain `.jsonl` file or, once
  `relay/fleet_retention.py::compress()` (`relay/fleet_retention.py:248`, triggered after
  `COMPRESS_AFTER_HOURS` = 6h by default, `:227`) has run, a `.jsonl.gz` file with the
  plain one deleted.
- **Writer**: `relay/fleet_retention.py::compress`.
- **Reader**: `ui/CopilotChat.cs` — **now merges both extensions** (verified current
  source): `ui/CopilotChat.cs:1005-1012` globs `*.jsonl` and `*.jsonl.gz` into one list,
  and `ui/CopilotChat.cs:5251-5252` carries a comment naming exactly this fix ("Merged
  glob: relay/fleet_retention.py gzips anything older than COMPRESS_AFTER_HOURS and
  deletes the plain file").
- **Guard today**: none. The fix is a merged glob in one place; nothing asserts every
  place that lists transcripts in `ui/CopilotChat.cs` (the docstring that reported the
  original bug said "in three places") still merges both extensions, and nothing on the
  Python side asserts a transcript reader outside this file (a mining script, say) does
  either.
- **What breaks silently if it drifts further**: exactly the original incident — a
  transcript disappears from the UI the moment it crosses the compression age, with the
  data still on disk and no error anywhere.

## Where this list is not exhaustive

This is a mechanical sweep for repeated literals/filenames/schemas across the modules
named in the flow map, not an exhaustive audit of the repository. Two candidates were
considered and left out for cause, recorded here so they are not silently reconsidered
later as "unexamined":

- `relay/fleet_toolset.py::FLEET_TOOLS` vs. the server's real ~167-tool catalogue **is**
  a duplicated fact (a manually maintained allowlist next to a much larger real registry),
  but it already has a working guard —
  `relay/fleet_toolset.py::unknown_tools()` (`:207-217`) is asserted empty (or fully
  accounted for) by `relay/test_fleet_toolset.py`. Not included as a row because the
  point of this table is the unguarded and under-guarded cases; this one is neither.
- `tools/fleet_intake.py::MAX_GOAL_CHARS` (4000, `tools/fleet_intake.py:49`) looked like a
  candidate duplicate against a UI-side input cap, but no cap on the cockpit composer's
  goal length was found in `ui/CopilotChat.cs` — there may be no second copy of this fact
  to drift.

## Staying current

- **Adding a new row**: when a fix for a "reader didn't know" bug lands, add a row here
  *in the same change* if nothing enforces the reader stays correct — a fixed bug with no
  guard is exactly what rows 0 and 6 above are.
- **Removing a row**: only when a guard is added, not when the immediate symptom is fixed
  (see row 0 — fixed on 2026-09-01, still listed because still unguarded).
- **The cheap check that exists**: `tests/test_flow_map_docs_name_files_that_exist.py`
  (see `system_flow_map.md`'s [Staying current](system_flow_map.md#staying-current)
  section) fails if any file path cited in this document no longer exists. It cannot
  detect a stale *line number* or a claim that is wrong about behavior — only a renamed
  or deleted file. Re-reading the cited source is still a human's job.

### 8. How a tool reports its own failure — ~160 writers, one reader, and the reader was wrong

- **The fact**: the shape of the string a tool returns when it fails. No tool in this
  repo raises; an MCP tool returns text, so a failure arrives as
  `[read_file error: FileNotFoundError: ...]`.
- **Writers**: every tool in `tools/`. Surveyed 2026-09-16: 314 returned literals of the
  form `[<name> error: ...]` across 40+ modules, plus `timeout` (4), `failed` (8),
  `refused` (8), `skipped` (12), `aborted` (2).
- **Reader**: `tools/tool_ledger.py::looks_failed` / `looks_unavailable`, and through
  `row_ok` everything computed over the ledger — including
  `tools/fleet_tool_health.py`, which colours a dot in the cockpit.
- **What broke**: the reader knew exactly one shape, `[locked`. Measured over the whole
  ledger, **2,041 of 37,018 rows filed as successes (5.5%) were failure reports** —
  1,789 `error`, 213 `timeout`, 37 `failed`, 2 `refused`. Every success rate ever computed
  over this file was inflated by them, and the health dot read greener the more tools failed.
- **What stops it now**: `tools/test_a_tool_that_returns_its_failure_still_failed.py`.
  It pins the recognised shapes, pins the 8,216 `[stdout]` rows that must stay green, and
  pins the `skipped`/`aborted` rows that are neither.
- **Still unguarded**: nothing stops a NEW tool inventing a sixth word. The survey above is
  a snapshot, not a check. A tool that writes `[foo broke: ...]` will read as a success and
  nothing will say so.

### 9. `TEMPLATE_MAX_AGE_S` — the route refuses what the cockpit was calling live

- **The fact**: how old a cached agent request-template may be and still count.
- **Writer**: `relay/profile_token.py:188` — `TEMPLATE_MAX_AGE_S`, 24 hours;
  `load_template` refuses and deletes anything older, and refuses one whose `gpt_id` is
  empty.
- **Reader**: `ui/FleetCockpit.cs::FleetAgentIsBound`, which decides whether the agent dot
  is green.
- **What broke**: the reader checked only that the file existed and contained the
  characters `gptId`. `.fleet/templates` held a template **211.7 hours old** — 8.8 days
  against a 24 hour cap — so the dot would have reported a binding the route evicts on
  first use, and gone on reporting it, because eviction only happens when a capture runs.
- **What stops it now**: `ui/test_a_green_dot_must_have_established_the_thing.py::
  test_the_cockpits_age_limit_is_not_looser_than_the_routes` parses the constant out of
  both files and fails if the cockpit's ever exceeds the route's. It also fails if either
  side stops declaring it in a form the other can be compared against, which is the
  failure mode a cross-language copy actually has.

