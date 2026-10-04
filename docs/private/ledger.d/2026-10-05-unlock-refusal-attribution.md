## 2026-10-05 - Unlock refusal attributed only to the worker that was refused

- Change: `relay/relay_fleet.py::_looks_locked` prose and fallback branches now attribute a
  server refusal to a worker only when no other turn was in flight at the refusal's instant
  (`_refusal_candidates_exclude`, reusing `relay/turn_windows.py`). Otherwise nothing is
  injected, no `_unlock_attempts` is spent, and a `unlock_refusal_unattributed` mechanism row
  (registered in `relay/mechanism_telemetry.py`, carries run_id and candidate count) is
  written via `RelayWorker._note_unattributed_refusal`. `_decide_impl` also refuses to inject
  into a reply that ends DONE/CONTINUE and quotes no server refusal
  (`_reply_completed_without_claiming_lock`). STUCK/FAIL replies are still steered.
- Why: a measured 439-worker run showed 58 of 120 refusal records injected more than one worker;
  bystanders that had already replied DONE were filed STUCK after 4 sibling-triggered steers.
- Behaviour kept: verbatim `[locked ...]` marker branch, exclusive attribution, and the case
  where no turn window is open (single-shot callers) are unchanged; the budget cap of 4 still
  ends a truly stuck worker.
- Tests: relay/test_a_sibling_refusal_is_not_every_workers_lock.py (registered in ci.yml) plus
  the existing unlock/lock-classification suites.
- Open: a worker that truly needs unlock while 2+ turns are in flight and that neither pastes
  the server marker nor is exclusive gets no steer from this path; the mechanism row makes the
  count visible.
