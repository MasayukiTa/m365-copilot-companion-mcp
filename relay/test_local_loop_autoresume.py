from pathlib import Path
from types import SimpleNamespace
import os

from relay import local_loop_controller as ll


def test_local_loop_job_lock_is_exclusive_and_reusable(tmp_path):
    a = ll._acquire_job_lock(tmp_path, 'job1')
    assert a is not None
    try:
        assert ll._acquire_job_lock(tmp_path, 'job1') is None
        b = ll._acquire_job_lock(tmp_path, 'job2')
        assert b is not None
        ll._release_job_lock(b)
    finally:
        ll._release_job_lock(a)
    c = ll._acquire_job_lock(tmp_path, 'job1')
    assert c is not None
    ll._release_job_lock(c)


def test_old_controller_cannot_clear_new_owners_marker(tmp_path):
    ll._write_controller_marker(tmp_path, 'job1', ['--job-id','job1'], pid=222, started=2.0)
    assert ll._clear_controller_marker(tmp_path, 'job1', owner_pid=111) is False
    assert ll._read_controller_marker(tmp_path, 'job1')['pid'] == 222
    assert ll._clear_controller_marker(tmp_path, 'job1', owner_pid=222) is True
    assert ll._read_controller_marker(tmp_path, 'job1') is None


def test_resume_argv_uses_durable_job_id_and_omits_goal_and_agent_secret(tmp_path):
    args = SimpleNamespace(
        state_dir=str(tmp_path / 'fleet'), db=str(tmp_path / 'jobs.sqlite3'),
        cdp_url='http://localhost:9222', agent_url='https://secret.example/?token=do-not-store',
        poll_seconds=1.5, turn_timeout=901.0, ui_idle_timeout=77.0,
        rotate_after_turns=9, js_heap_limit_mb=111.0, dom_node_limit=222,
        edge_mb_limit=333.0,
    )
    argv = ll._controller_resume_argv(args, 'job1')
    joined = ' '.join(argv)
    assert argv[:2] == ['--job-id', 'job1']
    assert '--state-dir' in argv and str((tmp_path/'fleet').resolve()) in argv
    assert '--db' in argv and str(tmp_path/'jobs.sqlite3') in argv
    assert '--cdp-url' in argv
    assert '--goal' not in joined and '--goal-file' not in joined and '--job-file' not in joined
    assert '--agent-url' not in joined and 'do-not-store' not in joined
    assert '--turn-timeout' in argv and '901.0' in argv


def test_main_owns_lock_and_marker_before_browser_start():
    src = Path(ll.__file__).read_text(encoding='utf-8')
    main = src[src.index('def main(argv=None):'):]
    lock = main.index('_acquire_job_lock(')
    project = main.index('_project_job_snapshot(')
    marker = main.index('_write_controller_marker(')
    browser = main.index('connect_over_cdp(')
    assert lock < marker < project < browser
    assert '_clear_controller_marker(' in main
    assert '_release_job_lock(' in main


def test_child_rewrite_preserves_supervisor_backoff(tmp_path):
    ll._write_controller_marker(
        tmp_path, 'job1', ['--job-id', 'job1'], pid=111, started=1.0,
        restart_count=4, retry_after=12345.0,
    )
    marker = ll._write_controller_marker(
        tmp_path, 'job1', ['--job-id', 'job1'], pid=222, started=2.0,
    )
    assert marker['restart_count'] == 4
    assert marker['retry_after'] == 12345.0


def test_main_clears_marker_only_after_controller_returns_normally():
    src = Path(ll.__file__).read_text(encoding='utf-8')
    main = src[src.index('def main(argv=None):'):]
    assert 'completed_normally = False' in main
    assert 'completed_normally = True' in main
    finally_block = main[main.index('finally:', main.index('job_lock = _acquire_job_lock')):]
    assert 'if marker_written and completed_normally and not keep_marker_for_runtime:' in finally_block
    assert '_clear_controller_marker(' in finally_block


def test_crash_marker_comment_explains_why_exception_must_leave_it():
    src = Path(ll.__file__).read_text(encoding='utf-8')
    assert 'leaves the marker behind while releasing the kernel lock' in src
    assert 'unexpected exception' in src


def test_waiting_runtime_is_the_only_normal_result_kept_for_autoresume():
    src = Path(ll.__file__).read_text(encoding='utf-8')
    main = src[src.index('def main(argv=None):'):]
    assert 'keep_marker_for_runtime = (result == "WAITING_RUNTIME")' in main
    assert 'if marker_written and completed_normally and not keep_marker_for_runtime:' in main
    # Human/consent/routing waits are ordinary returns and therefore clear their marker.
    assert 'completed_normally = True' in main


def test_main_crash_waiting_runtime_and_done_have_distinct_marker_semantics(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    import pytest

    monkeypatch.setenv("MCP_EXECUTION_PROFILES", "1")
    monkeypatch.setenv("MCP_FLEET_AGENT_URL", "http://127.0.0.1/fake-agent")

    class FakePage:
        def is_closed(self):
            return False
        def close(self):
            pass

    class FakeDriver:
        def __init__(self):
            self.page = FakePage()

    class FakePW:
        def __init__(self):
            self.chromium = SimpleNamespace(
                connect_over_cdp=lambda url, timeout=0: SimpleNamespace(
                    contexts=[SimpleNamespace()]
                )
            )
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    monkeypatch.setitem(
        sys.modules, "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakePW()),
    )
    monkeypatch.setitem(
        sys.modules, "relay.project_memory",
        SimpleNamespace(record_task=lambda *a, **k: None, theme_from_goal=lambda x: "test"),
    )
    monkeypatch.setattr(ll, "_open_driver", lambda context, agent_url: FakeDriver())

    state = tmp_path / "state"
    db = tmp_path / "jobs.sqlite3"

    def argv(job_id):
        return [
            "--goal", "exercise marker semantics",
            "--job-id", job_id,
            "--state-dir", str(state),
            "--db", str(db),
            "--cdp-url", "http://127.0.0.1:9222",
        ]

    class FakeController:
        result = "DONE"
        exc = None
        def __init__(self, store, job_id, driver, **kwargs):
            self.driver = driver
        def run(self):
            if self.exc is not None:
                raise self.exc
            return self.result

    monkeypatch.setattr(ll, "LocalLoopController", FakeController)

    # Unexpected crash: marker is the recovery proof, lock must still be released.
    FakeController.exc = RuntimeError("synthetic crash")
    with pytest.raises(RuntimeError, match="synthetic crash"):
        ll.main(argv("job_crash"))
    assert ll._read_controller_marker(state, "job_crash")["pid"] == os.getpid()
    h = ll._acquire_job_lock(state, "job_crash")
    assert h is not None
    ll._release_job_lock(h)
    assert ll._clear_controller_marker(state, "job_crash", owner_pid=os.getpid())

    # Runtime unavailable is a normal controller return, but intentionally auto-resumable.
    FakeController.exc = None
    FakeController.result = "WAITING_RUNTIME"
    assert ll.main(argv("job_runtime")) == 2
    assert ll._read_controller_marker(state, "job_runtime")["pid"] == os.getpid()
    assert ll._clear_controller_marker(state, "job_runtime", owner_pid=os.getpid())

    # True terminal completion removes the recovery marker.
    FakeController.result = "DONE"
    assert ll.main(argv("job_done")) == 0
    assert ll._read_controller_marker(state, "job_done") is None


def test_marker_and_lock_paths_reject_unsafe_job_ids(tmp_path):
    import pytest
    from relay.local_job_store import JobStoreError

    for bad in ("../escape", "..\\escape", "a/b", "a\\b", "", "x" * 129):
        with pytest.raises(JobStoreError) as exc:
            ll._controller_marker_path(tmp_path, bad)
        assert exc.value.code == "INVALID_JOB_ID"
        with pytest.raises(JobStoreError) as exc:
            ll._controller_lock_path(tmp_path, bad)
        assert exc.value.code == "INVALID_JOB_ID"


def test_resume_job_id_is_rejected_before_any_lock_or_marker_path_escape(tmp_path, monkeypatch):
    import pytest
    from relay.local_job_store import JobStoreError

    monkeypatch.setenv("MCP_EXECUTION_PROFILES", "1")
    monkeypatch.setenv("MCP_FLEET_AGENT_URL", "http://127.0.0.1/fake-agent")
    state = tmp_path / "state"
    db = tmp_path / "jobs.sqlite3"
    with pytest.raises(JobStoreError) as exc:
        ll.main([
            "--job-id", "../outside",
            "--state-dir", str(state),
            "--db", str(db),
        ])
    assert exc.value.code == "INVALID_JOB_ID"
    assert not (tmp_path / "outside.json").exists()
    assert not (tmp_path / "outside.lock").exists()
