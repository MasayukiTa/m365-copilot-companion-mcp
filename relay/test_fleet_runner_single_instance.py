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
    lock_i = main.index('_acquire_run_slot(')
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


def test_reused_pid_with_different_birth_is_not_a_live_marker_owner(tmp_path, monkeypatch):
    fr._write_atomic(str(tmp_path / fr.ACTIVE_MARKER), {
        "pid": 9876, "pid_birth": 111000, "start_ts": 1.0, "resume_argv": ["--resume"],
    })
    monkeypatch.setattr(fr, "_pid_alive", lambda pid: int(pid) == 9876)
    monkeypatch.setattr(fr, "_pid_birth_token", lambda pid: 222000 if int(pid) == 9876 else 0)
    assert fr._active_run_conflict_pid(str(tmp_path), self_pid=1234) == 0


def test_same_process_birth_keeps_the_active_marker_live(tmp_path, monkeypatch):
    fr._write_atomic(str(tmp_path / fr.ACTIVE_MARKER), {
        "pid": 9876, "pid_birth": 111000, "start_ts": 1.0, "resume_argv": ["--resume"],
    })
    monkeypatch.setattr(fr, "_pid_alive", lambda pid: int(pid) == 9876)
    monkeypatch.setattr(fr, "_pid_birth_token", lambda pid: 111000 if int(pid) == 9876 else 0)
    assert fr._active_run_conflict_pid(str(tmp_path), self_pid=1234) == 9876


def test_legacy_active_marker_without_birth_remains_conservative(tmp_path, monkeypatch):
    fr._write_atomic(str(tmp_path / fr.ACTIVE_MARKER), {
        "pid": 9876, "start_ts": 1.0, "resume_argv": ["--resume"],
    })
    monkeypatch.setattr(fr, "_pid_alive", lambda pid: int(pid) == 9876)
    monkeypatch.setattr(fr, "_pid_birth_token", lambda pid: 222000)
    assert fr._active_run_conflict_pid(str(tmp_path), self_pid=1234) == 9876


def test_boot_resumer_uses_the_same_process_instance_identity():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "win" / "resume_interrupted_fleet.py").read_text(encoding="utf-8")
    assert "marker_owner_alive(marker, pid_alive)" in src


def test_bounded_handoff_wait_retries_until_the_old_owner_releases(monkeypatch, tmp_path):
    owners = iter([4321, 4321, 0, 0])
    monkeypatch.setattr(fr, '_active_run_conflict_pid', lambda state_dir, self_pid=None: next(owners))
    token = object()
    monkeypatch.setattr(fr, '_acquire_run_lock', lambda state_dir: token)
    monkeypatch.setattr(fr.time, 'sleep', lambda seconds: None)
    lock, conflict = fr._acquire_run_slot(str(tmp_path), wait_seconds=5.0, poll_seconds=0.0)
    assert lock is token
    assert conflict == 0


def test_default_run_slot_acquisition_remains_fail_fast(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(fr, '_active_run_conflict_pid', lambda state_dir, self_pid=None: 9876)
    monkeypatch.setattr(fr, '_acquire_run_lock', lambda state_dir: calls.append(state_dir))
    lock, conflict = fr._acquire_run_slot(str(tmp_path), wait_seconds=0.0, poll_seconds=0.0)
    assert lock is None
    assert conflict == 9876
    assert calls == []


def test_lock_contention_can_clear_inside_explicit_handoff_window(monkeypatch, tmp_path):
    attempts = iter([None, None, 'LOCK'])
    monkeypatch.setattr(fr, '_active_run_conflict_pid', lambda state_dir, self_pid=None: 0)
    monkeypatch.setattr(fr, '_acquire_run_lock', lambda state_dir: next(attempts))
    monkeypatch.setattr(fr.time, 'sleep', lambda seconds: None)
    lock, conflict = fr._acquire_run_slot(str(tmp_path), wait_seconds=5.0, poll_seconds=0.0)
    assert lock == 'LOCK'
    assert conflict == 0


def test_handoff_wait_is_not_persisted_into_resume_argv():
    argv = ['--wait-for-state-dir-seconds', '60', '--effort', 'auto', '--goal', 'x']
    got = fr._resume_argv(argv)
    assert '--wait-for-state-dir-seconds' not in got
    assert '60' not in got
    assert got == ['--effort', 'auto']


def test_main_exposes_opt_in_handoff_wait_but_keeps_zero_default():
    src = Path(fr.__file__).read_text(encoding='utf-8')
    main = src[src.index('def main():'):]
    assert '"--wait-for-state-dir-seconds"' in main
    assert 'default=0.0' in main
    assert '_acquire_run_slot(' in main
    assert 'wait_seconds=args.wait_for_state_dir_seconds' in main
