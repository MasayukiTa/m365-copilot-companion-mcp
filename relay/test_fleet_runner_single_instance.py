import os
from pathlib import Path

from relay import fleet_runner as fr


def test_old_runner_cannot_clear_a_new_runners_marker(tmp_path):
    fr._write_active_marker(str(tmp_path), argv=['--resume'], pid=2222, start_ts=2.0)
    cleared = fr._clear_active_marker(str(tmp_path), owner_pid=1111)
    assert cleared is False
    assert fr._read_active_marker(str(tmp_path))['pid'] == 2222

    cleared = fr._clear_active_marker(str(tmp_path), owner_pid=2222)
    assert cleared is True
    assert fr._read_active_marker(str(tmp_path)) is None


def test_state_dir_run_lock_is_exclusive_and_released_by_the_kernel_file_lock(tmp_path):
    first = fr._acquire_run_lock(str(tmp_path))
    assert first is not None
    try:
        second = fr._acquire_run_lock(str(tmp_path))
        assert second is None, 'a second coordinator must not own the same state-dir'
    finally:
        fr._release_run_lock(first)

    third = fr._acquire_run_lock(str(tmp_path))
    assert third is not None
    fr._release_run_lock(third)


def test_startup_exclusion_happens_before_any_durable_run_state_is_rewritten():
    src = Path(fr.__file__).read_text(encoding='utf-8')
    main = src[src.index('def main():'):]
    lock_i = main.index('_acquire_run_lock(')
    ledger_i = main.index('_write_goals_ledger(')
    done_reset_i = main.index('_write_atomic(os.path.join(args.state_dir, LAST_RUN_DONE), {})')
    marker_i = main.index('_write_active_marker(')
    assert lock_i < ledger_i
    assert lock_i < done_reset_i
    assert lock_i < marker_i


def test_live_marker_conflict_is_detected_before_lock_race_fallback(tmp_path, monkeypatch):
    fr._write_active_marker(str(tmp_path), argv=['x'], pid=9876, start_ts=1.0)
    monkeypatch.setattr(fr, '_pid_alive', lambda pid: int(pid) == 9876)
    assert fr._active_run_conflict_pid(str(tmp_path), self_pid=1234) == 9876
    assert fr._active_run_conflict_pid(str(tmp_path), self_pid=9876) == 0


def test_unreadable_existing_active_marker_fails_closed(tmp_path):
    """PR47 #4111676515: a legacy owner's unreadable marker is not evidence of no owner."""
    (tmp_path / fr.ACTIVE_MARKER).write_text('{broken', encoding='utf-8')
    assert fr._active_run_conflict_pid(str(tmp_path), self_pid=1234) != 0


def test_noninteger_active_marker_pid_fails_closed(tmp_path):
    fr._write_atomic(str(tmp_path / fr.ACTIVE_MARKER), {'pid': 'not-a-pid'})
    assert fr._active_run_conflict_pid(str(tmp_path), self_pid=1234) != 0
