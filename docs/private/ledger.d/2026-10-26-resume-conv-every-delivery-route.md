## 2026-10-26 - `resume_conv` survives every delivery route (the field that was carried on only some)

- Problem. `resume_conv` -- the durable conversation id the chat window reads off the transcript so
  the fleet CONTINUES a conversation instead of guessing one by goal text -- was honoured by the
  fleet side all along (`fleet_runner.goals_from_command` copies it onto the goal item, and the
  goals-file path in `_read_goals_file` appends the whole dict), but `task_router.fleet_handoff`
  only threaded it onto SOME of the three routes a goal can leave by. The cockpit path worked; the
  chat-window path dropped the field and quietly started a fresh conversation. This is the exact
  shape of the `priority` gap closed earlier in the same file (and the `gate_verdict`/`verdict` and
  `keep`/`kept` writer/reader mismatches already on this project's record): a field a writer emits
  and a reader honours, with no path actually joining them on one of the routes.

- The three routes a goal leaves `fleet_handoff` by (same inventory the `priority` work pinned):
    1. join a live run   -> `add_goal_to_live_fleet` add_goal item
    2. start a fleet     -> `autostart_fleet` goals dict
    3. no run, so wait   -> `for_fleet/<jid>.txt`, parked as JSON

- Change. `fleet_handoff(..., resume_conv="")` now threads the id onto routes 1 and 2 beside
  `priority`, and the parked-file carrier (`_write_for_fleet` / `read_for_fleet`) carries it on
  route 3. `read_for_fleet` now returns a 3-tuple `(goal, priority, resume_conv)` (was 2-tuple);
  `_deliver_waiting_goals` unpacks the third value and passes `resume_conv=` back into
  `fleet_handoff` when a fleet finally appears, so a goal that had to wait keeps its conversation id
  across the wait and is redelivered with it. The door (`fleet_intake.fleet_submit`) takes an
  optional `resume_conv`, trims it like the other relayed fields, and writes it onto the queued
  job's payload only when non-empty.

- Default unchanged = "start fresh". An empty/absent `resume_conv` is never written onto the goal
  item or the payload, so `resume_conv not in item` keeps meaning "new conversation" and no reader
  sees a stray empty id. Tests pin this on every route (route 1/2 "carries no field" cases, the
  plain parked file reading back as `(..., "")`, and an ordinary `fleet_submit` writing no key).

- Duplicate-check exemption. A follow-up is deliberately terse and reads as a near-duplicate of the
  turn still sitting in `pending/`. Without an exemption the continuation would be refused and the
  conversation it meant to resume would never be answered. A submission carrying a `resume_conv` is
  admitted as a continuation rather than refused as an overlap; its own id travels with it
  (`test_a_follow_up_is_not_refused_as_a_near_duplicate`).

- Tests. New `relay/test_resume_conv_survives_every_delivery_route.py` mirrors the structure of the
  `priority` suite -- one case per route, the wait->redeliver round trip, priority+resume_conv
  coexisting in the same parked JSON, and an end-to-end door->drain->live-fleet seam that reads the
  goal items through the fleet's own `goals_from_command`. New `resume_conv` cases added to
  `tools/test_fleet_intake.py`. Existing `test_priority_survives_every_delivery_route.py`
  assertions updated to the 3-tuple shape. Full related sweep green (40 passed for the
  intake+priority suites; 16 passed for the new route suite).

- Running note (not a code issue). The module import cost is ~24 s, and a second checkout at
  `C:\Users\<user>\the live checkout` shares the top-level `tools`/`relay` package names, so a naive
  `pytest` run either times out on repeated collection imports or picks up the other clone's
  same-named test modules. Pin both packages' `__path__` to this worktree and warm the imports once
  in-process before calling `pytest.main`; use `--import-mode=importlib`. The test LOGIC was never
  implicated -- every failure during bring-up was cross-clone import resolution, not the change.
