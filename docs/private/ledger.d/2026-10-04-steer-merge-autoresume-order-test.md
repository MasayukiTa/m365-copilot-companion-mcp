## 2026-10-04 - Durable operator steer: merge with main, autoresume ordering test

- Change: merged origin/main into the durable-operator-steer-phase3 branch (no conflicts). The
  new `--operator-steer-*` mode in `relay/local_loop_controller.py` main() projects a status
  snapshot before the controller run path, so
  `relay/test_local_loop_autoresume.py::test_main_owns_lock_and_marker_before_browser_start`
  now looks for the first snapshot projection AFTER the job lock instead of the first one in
  main(). Test-only; no production code changed by this entry.
- Behaviour: unchanged. The ordering the test guards (lock, then marker, then snapshot, then
  browser start on the run path) is still asserted.
- Tests: relay/test_local_loop_*, relay/test_local_operator_steer.py,
  ui/test_durable_operator_steer.py and the cockpit/local-named ui tests.
- Open: none.
