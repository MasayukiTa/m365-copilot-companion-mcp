## 2026-11-09 - Socket capture failure says which stage it was in, and how long each took

- Change: relay/socket_capture_async.py `_worker_main` previously logged only
  `type(exc).__name__: str(exc)` on failure, and the parent `_wait_one` raised a bare
  `capture helper exceeded 180s`, so a timed-out token refresh never said whether the CDP
  connect hung, the page was never found, the token read stalled, or the write failed. The
  worker now times each of the four stages -- connect, page found, token read, write -- with a
  small `_StageTimer`, names the in-flight stage in a `STAGE=<name> timings[...]` prefix on its
  stderr line, and carries the per-stage timings in the success payload (`stages`). The parent
  recovers that stage name from the worker's stderr tail (`_stage_from_tail`) and lifts it to
  the FRONT of both the timeout message (`exceeded 180s in stage 'token read'`) and the
  nonzero-exit message, so the stage reaches the failure log and `note_capture_failure` result,
  not just a bare exit code. `STAGE=` is kept first so even the 200-char truncated tail the
  timeout path keeps still carries the stage. No browser behaviour changed and no credential is
  put on argv or stdout; stdout stays the private token protocol.
- Tests: relay/test_socket_capture_async.py (already in ci.yml) gains three cases --
  `test_stage_timer_records_each_stage_and_names_the_inflight_one` (per-stage elapsed and the
  in-flight name, with an injected clock), `test_stage_name_is_recovered_from_a_worker_stderr_tail`
  (parses the four stage names, and returns `(unknown)` on a missing marker rather than crashing),
  and `test_parent_failure_result_names_the_stage_from_a_nonzero_exit` (a staged-tail proc proves
  the stage name reaches `note_capture_failure`). Full file: 14 passed. F821 clean; CI manifest
  and integration-evidence gates pass; the committed bytes are LF (index `i/lf`).
- Conclusion: the four-stage instrumentation lands on both the failure log and the result as
  required. A capture timeout now names the stage it hung in and the time spent per stage, which
  is exactly the signal the 180 s bare-timeout was missing -- the 2026-10-26 mass-expiry class of
  incident can now be attributed to a stage instead of guessed at. Verified green on the branch
  `capdiag2-stage-timing`; not yet merged to main.
- Open: the parent only reports the stage the worker managed to print; a worker killed mid-connect
  before flushing still reads as stage `(unknown)`, which is honest but coarse. A future slice
  could have the parent record its own coarse connect/await boundary so even a pre-print kill is
  attributable.
