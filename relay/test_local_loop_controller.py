import json
from pathlib import Path

from relay.local_job_store import LocalJobStore
from relay.local_loop_controller import (
    LocalLoopController,
    _AUTH_ACTION_SELECTOR,
    _CREDENTIAL_INPUT_SELECTOR,
    _close_driver_page,
    _job_from_goal,
    _new_companion_job_id,
    _controller_resume_argv,
    _project_job_snapshot,
    _read_goal_file,
    _write_atomic,
    probe_browser_interaction,
)


def _job(job_id="job_1", max_turns=10):
    return {
        "job_id": job_id,
        "execution_profile": "LOCAL_LOOP",
        "data_location": "LOCAL",
        "requires_local_tool": True,
        "task": {"instruction": "Do the work"},
        "constraints": {"max_turns": max_turns},
        "acceptance_checks": [{"type": "file_exists", "path": "out.txt"}],
    }


class CommitOnSendDriver:
    def __init__(self, store, statuses):
        self.store = store
        self.statuses = iter(statuses)
        self.sent = []
        self.answer_content_reads = 0
        self.idle = True

    def send(self, text, track_answer=True):
        assert track_answer is False
        self.sent.append(text)
        parts = dict(part.split("=", 1) for part in text.split()[2:])
        seq = int(parts["seq"])
        worker = parts["worker"]
        claim = self.store.claim_turn("job_1", seq, worker)
        status = next(self.statuses)
        kwargs = {"status": status, "summary": f"summary {seq}"}
        if status == "CONTINUE":
            kwargs["next_instruction"] = "continue locally"
        self.store.commit_turn(
            "job_1", seq, claim["lease_id"], claim["fencing_token"], **kwargs,
        )

    def _is_generating(self):
        return not self.idle

    def _wait_generation_idle(self, timeout_s):
        return self.idle

    def _page_alive(self):
        return True


class NoCommitDriver(CommitOnSendDriver):
    def send(self, text, track_answer=True):
        assert track_answer is False
        self.sent.append(text)


class FinishedWithoutCommitDriver(NoCommitDriver):
    def __init__(self, store):
        super().__init__(store, [])
        self.responses = 0

    def send(self, text, track_answer=True):
        assert track_answer is False
        self.sent.append(text)
        parts = dict(part.split("=", 1) for part in text.split()[2:])
        self.store.claim_turn("job_1", int(parts["seq"]), parts["worker"])
        self.responses += 1

    def response_block_count(self):
        return self.responses


class SendFailureDriver(NoCommitDriver):
    def send(self, text, track_answer=True):
        raise RuntimeError("composer cleared without a conversation receipt")


class RetryAbortThenCommitDriver(CommitOnSendDriver):
    def __init__(self, store):
        super().__init__(store, [])
        self.calls = 0

    def send(self, text, track_answer=True):
        assert track_answer is False
        self.sent.append(text)
        parts = dict(part.split("=", 1) for part in text.split()[2:])
        seq = int(parts["seq"])
        worker = parts["worker"]
        claim = self.store.claim_turn("job_1", seq, worker)
        self.calls += 1
        if self.calls == 1:
            self.store.abort_turn(
                "job_1", seq, claim["lease_id"], claim["fencing_token"],
                "POLICY_RETRY", "visible confirmation required", True,
            )
        else:
            self.store.commit_turn(
                "job_1", seq, claim["lease_id"], claim["fencing_token"],
                status="CANDIDATE_DONE", summary="confirmed",
            )


def test_controller_completes_two_turns_without_reading_response_content(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job())
    store.create_job(_job("unrelated_shared_db_job"))
    driver = CommitOnSendDriver(store, ["CONTINUE", "CANDIDATE_DONE"])
    status_path = tmp_path / "status.json"
    controller = LocalLoopController(
        store, "job_1", driver, status_path=status_path,
        poll_seconds=.01, rotate_after_turns=0,
        acceptance_runner=lambda job: (True, "verified"),
        metrics_probe=lambda drv: {"js_heap_mb": 12, "dom_nodes": 100},
    )
    assert controller.run() == "DONE"
    assert len(driver.sent) == 2
    assert driver.answer_content_reads == 0
    projected = json.loads(status_path.read_text(encoding="utf-8"))
    assert projected["local_loop_answer_content_reads"] == 0
    assert projected["total"] == 1
    assert [worker["name"] for worker in projected["workers"]] == ["job_1"]
    assert projected["workers"][0]["outcome"] == "DONE"


def test_fixed_plan_trigger_is_short_and_transparent(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    job = _job()
    job["constraints"]["read_only"] = True
    job["turn_plan"] = [
        {"instruction": "Inspect the first file"},
        {"instruction": "Inspect the second file"},
    ]
    store.create_job(job)
    driver = CommitOnSendDriver(store, ["CONTINUE", "CANDIDATE_DONE"])
    controller = LocalLoopController(
        store, "job_1", driver, rotate_after_turns=0, poll_seconds=.01,
        acceptance_runner=lambda current: (True, "verified"),
    )
    assert controller.run() == "DONE"
    assert driver.sent[0].startswith("RUN job_1 seq=1 worker=local_")
    assert driver.sent[0].endswith(" plan=1/2 mode=read-only")
    assert driver.sent[1].startswith("RUN job_1 seq=2 worker=local_")
    assert driver.sent[1].endswith(" plan=2/2 mode=read-only")


def test_failed_acceptance_becomes_next_seq_not_done(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job())
    driver = CommitOnSendDriver(store, ["CANDIDATE_DONE", "CANDIDATE_DONE"])
    results = iter([(False, "test failed"), (True, "test passed")])
    controller = LocalLoopController(
        store, "job_1", driver, rotate_after_turns=0,
        acceptance_runner=lambda job: next(results), poll_seconds=.01,
    )
    assert controller.run() == "DONE"
    assert len(driver.sent) == 2
    assert any(
        event["event"] == "VERIFICATION_FAILED"
        and "test failed" in event["payload"].get("detail", "")
        for event in store.get_job_status("job_1", event_limit=20)["events"]
    )


def test_turn_threshold_rotates_and_preserves_external_job_state(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job())
    first = CommitOnSendDriver(store, ["CONTINUE"])
    second = CommitOnSendDriver(store, ["CANDIDATE_DONE"])
    rotations = []

    def rotate(old, reason):
        rotations.append(reason)
        return second

    controller = LocalLoopController(
        store, "job_1", first, rotate_after_turns=1, rotate_driver=rotate,
        acceptance_runner=lambda job: (True, "verified"), poll_seconds=.01,
    )
    assert controller.run() == "DONE"
    assert rotations == ["turn threshold"]
    assert len(first.sent) == 1 and len(second.sent) == 1
    assert first.answer_content_reads == second.answer_content_reads == 0


def test_ui_idle_failure_forces_rotation_after_commit(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job())
    first = CommitOnSendDriver(store, ["CONTINUE"])
    first.idle = False
    second = CommitOnSendDriver(store, ["CANDIDATE_DONE"])
    rotations = []
    controller = LocalLoopController(
        store, "job_1", first, rotate_after_turns=0,
        rotate_driver=lambda old, reason: rotations.append(reason) or second,
        acceptance_runner=lambda job: (True, "verified"), poll_seconds=.01,
    )
    assert controller.run() == "DONE"
    assert rotations == ["commit received but UI did not become idle"]


def test_consent_wait_is_observable_and_resumes_same_seq(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job())
    first = NoCommitDriver(store, [])
    waiting = LocalLoopController(
        store, "job_1", first, rotate_after_turns=0, poll_seconds=.01,
        consent_probe=lambda driver: "WAITING_CONSENT",
    )
    assert waiting.run() == "WAITING_CONSENT"
    assert store.get_job_status("job_1")["current_seq"] == 1

    second = CommitOnSendDriver(store, ["CANDIDATE_DONE"])
    resumed = LocalLoopController(
        store, "job_1", second, rotate_after_turns=0, poll_seconds=.01,
        consent_probe=lambda driver: "CLEAR",
        acceptance_runner=lambda job: (True, "verified"),
    )
    assert resumed.run() == "DONE"
    assert second.sent[0].startswith("RUN job_1 seq=1 ")


def test_ui_idle_failure_without_replacement_waits_and_resumes_safely(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job())
    first = CommitOnSendDriver(store, ["CONTINUE"])
    first.idle = False
    stopped = LocalLoopController(
        store, "job_1", first, rotate_after_turns=0,
        acceptance_runner=lambda job: (True, "verified"), poll_seconds=.01,
    )
    assert stopped.run() == "WAITING_RUNTIME"
    assert store.get_job_status("job_1")["status"] == "WAITING_RUNTIME"

    second = CommitOnSendDriver(store, ["CANDIDATE_DONE"])
    resumed = LocalLoopController(
        store, "job_1", second, rotate_after_turns=0,
        acceptance_runner=lambda job: (True, "verified"), poll_seconds=.01,
    )
    assert resumed.run() == "DONE"
    assert second.answer_content_reads == 0


def test_console_stop_cancels_job_without_browser_response(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job())
    driver = CommitOnSendDriver(store, [])
    commands = tmp_path / "commands.json"
    _write_atomic(commands, {"stop": True})
    controller = LocalLoopController(store, "job_1", driver, commands_path=commands)
    assert controller.run() == "CANCELLED"
    assert driver.sent == []


def test_retryable_abort_is_retried_without_waiting_for_commit_timeout(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job())
    driver = RetryAbortThenCommitDriver(store)
    controller = LocalLoopController(
        store, "job_1", driver, rotate_after_turns=0, poll_seconds=.01,
        acceptance_runner=lambda job: (True, "verified"),
    )
    assert controller.run() == "DONE"
    assert len(driver.sent) == 2
    assert store.get_job_status("job_1")["retry_count"] == 1


def test_finished_response_without_commit_rotates_immediately_and_retries(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job())
    first = FinishedWithoutCommitDriver(store)
    second = CommitOnSendDriver(store, ["CANDIDATE_DONE"])
    rotations = []
    controller = LocalLoopController(
        store, "job_1", first, rotate_after_turns=0, poll_seconds=.005,
        no_commit_idle_seconds=.01,
        rotate_driver=lambda old, reason: rotations.append(reason) or second,
        acceptance_runner=lambda current: (True, "verified"),
    )

    assert controller.run() == "DONE"
    assert rotations == ["response finished without commit"]
    assert len(first.sent) == 1 and len(second.sent) == 1
    status = store.get_job_status("job_1", event_limit=30)
    assert status["retry_count"] == 1
    assert any(event["event"] == "TURN_FINISHED_WITHOUT_COMMIT"
               for event in status["events"])


def test_send_failure_rotates_instead_of_terminating_controller(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job())
    first = SendFailureDriver(store, [])
    second = CommitOnSendDriver(store, ["CANDIDATE_DONE"])
    rotations = []
    controller = LocalLoopController(
        store, "job_1", first, rotate_after_turns=0, poll_seconds=.01,
        rotate_driver=lambda old, reason: rotations.append(reason) or second,
        acceptance_runner=lambda current: (True, "verified"),
    )

    assert controller.run() == "DONE"
    assert rotations == ["send failed"]
    assert any(
        event["event"] == "UI_TRIGGER_FAILED"
        for event in store.get_job_status("job_1", event_limit=30)["events"]
    )


def test_retry_attempt_does_not_consume_logical_turn_budget(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    job = _job(max_turns=1)
    job["constraints"]["max_attempts"] = 2
    store.create_job(job)
    driver = RetryAbortThenCommitDriver(store)
    controller = LocalLoopController(
        store, "job_1", driver, rotate_after_turns=0, poll_seconds=.01,
        acceptance_runner=lambda current: (True, "verified"),
    )
    assert controller.run() == "DONE"
    assert len(driver.sent) == 2
    assert store.get_job_status("job_1")["current_seq"] == 1


def test_thirty_turn_smoke_rotates_without_any_response_content_reads(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job(max_turns=35))
    statuses = iter(["CONTINUE"] * 29 + ["CANDIDATE_DONE"])
    drivers = [CommitOnSendDriver(store, statuses)]
    rotations = []

    def rotate(old, reason):
        rotations.append(reason)
        drivers.append(CommitOnSendDriver(store, statuses))
        return drivers[-1]

    controller = LocalLoopController(
        store, "job_1", drivers[0], rotate_after_turns=5, rotate_driver=rotate,
        acceptance_runner=lambda job: (True, "verified"), poll_seconds=.01,
    )
    assert controller.run() == "DONE"
    assert sum(len(driver.sent) for driver in drivers) == 30
    assert len(rotations) == 5
    assert all(driver.answer_content_reads == 0 for driver in drivers)
    assert store.get_job_status("job_1", event_limit=50)["status"] == "DONE"


class _AuthItem:
    def __init__(self, click=None):
        self._click = click

    def is_visible(self):
        return True

    def click(self):
        if self._click:
            self._click()


class _AuthLocator:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _AuthPage:
    def __init__(self, actions_per_step, credential=False):
        self.url = "https://login.microsoftonline.com/signin"
        self.actions_per_step = list(actions_per_step)
        self.credential = credential
        self.step = 0

    def locator(self, selector):
        if selector == _CREDENTIAL_INPUT_SELECTOR:
            return _AuthLocator([_AuthItem()] if self.credential else [])
        if selector == _AUTH_ACTION_SELECTOR:
            count = self.actions_per_step[min(self.step, len(self.actions_per_step) - 1)]

            def clicked():
                self.step += 1
                if self.step >= len(self.actions_per_step):
                    self.url = "https://m365.cloud.microsoft/chat"

            return _AuthLocator([_AuthItem(clicked) for _ in range(count)])
        return _AuthLocator([])

    def wait_for_timeout(self, _milliseconds):
        return None


class _AuthDriver:
    def __init__(self, page):
        self.page = page


def test_auth_probe_follows_only_single_choice_chain(monkeypatch):
    monkeypatch.setattr("relay.edge_reconnect.click_through_consent", lambda page: False)
    page = _AuthPage([1, 1])
    assert probe_browser_interaction(_AuthDriver(page)) == "CLEAR"
    assert page.step == 2


def test_auth_probe_stops_for_multiple_accounts(monkeypatch):
    monkeypatch.setattr("relay.edge_reconnect.click_through_consent", lambda page: False)
    page = _AuthPage([2])
    assert probe_browser_interaction(_AuthDriver(page)) == "WAITING_AUTH"
    assert page.step == 0


def test_auth_probe_never_submits_visible_credentials(monkeypatch):
    monkeypatch.setattr("relay.edge_reconnect.click_through_consent", lambda page: False)
    page = _AuthPage([1], credential=True)
    assert probe_browser_interaction(_AuthDriver(page)) == "WAITING_AUTH"
    assert page.step == 0


def test_close_driver_page_closes_only_owned_page():
    class Page:
        closed = False

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

    page = Page()
    driver = type("Driver", (), {"page": page})()
    _close_driver_page(driver)
    assert page.closed is True
    _close_driver_page(driver)  # idempotent


def test_plain_goal_builds_a_durable_companion_job(tmp_path):
    job = _job_from_goal(
        "Research the supplier change and prepare the meeting pack",
        job_id="companion_test_1", cwd=str(tmp_path), max_turns=37, read_only=True,
    )
    assert job["job_id"] == "companion_test_1"
    assert job["execution_profile"] == "LOCAL_LOOP"
    assert job["requires_local_tool"] is True
    assert job["task"]["type"] == "companion_task"
    assert job["task"]["instruction"] == "Research the supplier change and prepare the meeting pack"
    assert job["constraints"]["max_turns"] == 37
    assert job["constraints"]["read_only"] is True
    assert job["constraints"]["allowed_base"] == str(tmp_path)
    assert job["acceptance_checks"] == []


def test_plain_goal_without_cwd_does_not_invent_a_workspace():
    job = _job_from_goal("Summarize this quarter", job_id="companion_test_2")
    assert "allowed_base" not in job["constraints"]


def test_companion_job_id_is_safe_and_goal_scoped():
    a = _new_companion_job_id("same goal", now=1234567890.0, nonce="abcd")
    b = _new_companion_job_id("different goal", now=1234567890.0, nonce="abcd")
    assert a != b
    assert a.startswith("companion_20090213_233130_")
    assert len(a) < 128
    assert all(ch.isalnum() or ch in "_.-" for ch in a)


def test_plain_goal_rejects_empty_instruction():
    import pytest
    with pytest.raises(ValueError, match="goal"):
        _job_from_goal("  ", job_id="companion_test_3")


def test_goal_file_preserves_multiline_task_text(tmp_path):
    path = tmp_path / "goal.txt"
    path.write_text("Make the pack\nUse the latest figures\nKeep citations", encoding="utf-8")
    assert _read_goal_file(path) == "Make the pack\nUse the latest figures\nKeep citations"


def test_job_projection_is_scoped_to_the_controller_job(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job("job_1"), now=1)
    store.create_job(_job("job_2"), now=2)
    status_path = tmp_path / "status.json"

    _project_job_snapshot(store, "job_2", status_path)

    projected = json.loads(status_path.read_text(encoding="utf-8"))
    assert projected["total"] == 1
    assert [w["name"] for w in projected["workers"]] == ["job_2"]
    assert projected["running"] is True
    assert projected["execution_mode"] == "LOCAL_LOOP"


def test_controller_marker_preserves_supervisor_backoff_reservation(tmp_path):
    from relay.local_loop_controller import _write_controller_marker, _read_controller_marker

    state = tmp_path / "state"
    state.mkdir()
    job_id = "backoff-preserve"
    _write_controller_marker(
        state, job_id, ["--job-id", job_id], pid=111, started=10.0,
        restart_count=7, retry_after=12345.0,
    )
    marker = _write_controller_marker(
        state, job_id, ["--job-id", job_id], pid=222, started=20.0,
    )
    assert marker["pid"] == 222
    assert marker["started"] == 20.0
    assert marker["restart_count"] == 7
    assert marker["retry_after"] == 12345.0
    assert _read_controller_marker(state, job_id) == marker


def test_max_attempts_is_durable_across_controller_restart(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    job = _job(max_turns=5)
    job["constraints"]["max_attempts"] = 2
    store.create_job(job)
    # Simulate two RUN attempts made by an earlier controller process.
    store.record_event("job_1", "UI_TRIGGER_ATTEMPT", {"seq": 1, "worker_id": "old-a"}, 1)
    store.record_event("job_1", "UI_TRIGGER_ATTEMPT", {"seq": 1, "worker_id": "old-b"}, 1)

    class MustNotSendDriver:
        answer_content_reads = 0
        sent = []
        def send(self, *args, **kwargs):
            raise AssertionError("a restarted controller reset the durable attempt budget")

    driver = MustNotSendDriver()
    controller = LocalLoopController(
        store, "job_1", driver, rotate_after_turns=0, poll_seconds=.01,
    )
    assert controller.run() == "CANCELLED"
    assert driver.sent == []
    status = store.get_job_status("job_1", event_limit=30)
    assert status["status"] == "CANCELLED"
    assert "max_attempts=2 reached" in status["verification_detail"]


def test_each_run_trigger_records_durable_attempt_before_send(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    job = _job(max_turns=1)
    job["constraints"]["max_attempts"] = 2
    store.create_job(job)
    driver = RetryAbortThenCommitDriver(store)
    controller = LocalLoopController(
        store, "job_1", driver, rotate_after_turns=0, poll_seconds=.01,
        acceptance_runner=lambda current: (True, "verified"),
    )
    assert controller.run() == "DONE"
    assert store.ui_trigger_attempt_count("job_1") == 2


def test_projection_aggregates_marker_owned_jobs_in_same_state_dir(tmp_path):
    from relay.local_loop_controller import _write_controller_marker

    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job("job_1"), now=1)
    store.create_job(_job("job_2"), now=2)
    store.create_job(_job("unrelated_shared_db_job"), now=3)
    _write_controller_marker(tmp_path, "job_1", ["--job-id", "job_1"], pid=101, started=1)
    _write_controller_marker(tmp_path, "job_2", ["--job-id", "job_2"], pid=202, started=2)
    status_path = tmp_path / "status.json"

    _project_job_snapshot(store, "job_1", status_path, now=10)

    projected = json.loads(status_path.read_text(encoding="utf-8"))
    assert [w["name"] for w in projected["workers"]] == ["job_1", "job_2"]
    assert projected["total"] == 2
    assert projected["open_tabs"] == 2
    assert projected["running"] is True
    assert projected["started"] == 1
    assert "unrelated_shared_db_job" not in {w["name"] for w in projected["workers"]}


def test_projection_keeps_recent_terminal_job_visible_across_other_controller_refresh(tmp_path):
    from relay.local_loop_controller import _write_controller_marker, _clear_controller_marker

    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job("job_1"), now=1)
    store.create_job(_job("job_2"), now=2)
    _write_controller_marker(tmp_path, "job_1", ["--job-id", "job_1"], pid=101, started=1)
    _write_controller_marker(tmp_path, "job_2", ["--job-id", "job_2"], pid=202, started=2)
    status_path = tmp_path / "status.json"
    _project_job_snapshot(store, "job_1", status_path, now=90)

    store.cancel_job("job_1", "finished for projection test", now=100)
    assert _clear_controller_marker(tmp_path, "job_1", owner_pid=101) is True

    # job_2 refreshes the shared status after job_1 has cleared its recovery marker. The recent
    # terminal row must remain visible long enough for Cockpit/history to observe completion.
    _project_job_snapshot(store, "job_2", status_path, now=110)
    recent = json.loads(status_path.read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in recent["workers"]}
    assert set(by_name) == {"job_1", "job_2"}
    assert by_name["job_1"]["closed"] is True
    assert by_name["job_1"]["outcome"] == "CANCELLED"
    assert recent["open_tabs"] == 1

    # The completion grace is display-only; old terminal jobs must eventually leave live status.
    _project_job_snapshot(store, "job_2", status_path, now=1000)
    later = json.loads(status_path.read_text(encoding="utf-8"))
    assert [w["name"] for w in later["workers"]] == ["job_2"]


def test_atomic_projection_writer_uses_unique_temp_files_under_concurrency(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from relay.local_loop_controller import _write_atomic

    target = tmp_path / "status.json"

    def writer(worker_id):
        for seq in range(80):
            _write_atomic(target, {"worker": worker_id, "seq": seq, "payload": "x" * 2000})

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(writer, i) for i in range(8)]
        for future in futures:
            future.result()

    final = json.loads(target.read_text(encoding="utf-8"))
    assert 0 <= final["worker"] < 8
    assert 0 <= final["seq"] < 80
    assert not list(tmp_path.glob(".status.json.*.tmp"))


def test_concurrent_job_projections_keep_every_active_job_visible(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from relay.local_loop_controller import _write_controller_marker

    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job("job_1"), now=1)
    store.create_job(_job("job_2"), now=2)
    _write_controller_marker(tmp_path, "job_1", ["--job-id", "job_1"], pid=101, started=1)
    _write_controller_marker(tmp_path, "job_2", ["--job-id", "job_2"], pid=202, started=2)
    status_path = tmp_path / "status.json"

    def project(job_id, offset):
        for seq in range(40):
            _project_job_snapshot(store, job_id, status_path, now=100 + offset + seq / 1000.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(project, "job_1", 0.0)
        b = pool.submit(project, "job_2", 0.5)
        a.result()
        b.result()

    final = json.loads(status_path.read_text(encoding="utf-8"))
    assert {w["name"] for w in final["workers"]} == {"job_1", "job_2"}
    assert final["total"] == 2
    assert final["open_tabs"] == 2
    assert final["running"] is True
    assert final["started"] == 1


def test_campaign_manifest_projects_queued_jobs_without_counting_them_as_open_tabs(tmp_path):
    from relay.local_loop_controller import (
        LOCAL_LOOP_CAMPAIGN_MANIFEST, _write_controller_marker,
    )

    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job("job_1"), now=1)
    store.create_job(_job("job_2"), now=2)
    store.create_job(_job("unrelated"), now=3)
    (tmp_path / LOCAL_LOOP_CAMPAIGN_MANIFEST).write_text(json.dumps({
        "version": 1,
        "started": 1,
        "entries": [{"job_id": "job_1"}, {"job_id": "job_2"}],
    }), encoding="utf-8")
    _write_controller_marker(tmp_path, "job_1", ["--job-id", "job_1"], pid=101, started=1)
    status_path = tmp_path / "status.json"

    _project_job_snapshot(store, "job_1", status_path, now=10)

    projected = json.loads(status_path.read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in projected["workers"]}
    assert set(by_name) == {"job_1", "job_2"}
    assert by_name["job_2"]["status"] == "ready"
    assert projected["total"] == 2
    assert projected["open_tabs"] == 1
    assert projected["running"] is True
    assert "unrelated" not in by_name


def test_stale_campaign_manifest_is_ignored_by_an_unrelated_standalone_job(tmp_path):
    from relay.local_loop_controller import (
        LOCAL_LOOP_CAMPAIGN_MANIFEST, _write_controller_marker,
    )

    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    store.create_job(_job("old_campaign_job"), now=1)
    store.create_job(_job("standalone"), now=2)
    (tmp_path / LOCAL_LOOP_CAMPAIGN_MANIFEST).write_text(json.dumps({
        "version": 1, "entries": [{"job_id": "old_campaign_job"}],
    }), encoding="utf-8")
    _write_controller_marker(tmp_path, "standalone", ["--job-id", "standalone"], pid=202, started=2)
    status_path = tmp_path / "status.json"

    _project_job_snapshot(store, "standalone", status_path, now=10)

    projected = json.loads(status_path.read_text(encoding="utf-8"))
    assert [w["name"] for w in projected["workers"]] == ["standalone"]
    assert projected["open_tabs"] == 1


def test_controller_resume_argv_preserves_an_explicit_commands_file(tmp_path):
    from types import SimpleNamespace

    commands = tmp_path / "child-commands.json"
    args = SimpleNamespace(
        state_dir=str(tmp_path), db=str(tmp_path / "jobs.sqlite3"),
        cdp_url="http://localhost:9222", commands_file=str(commands),
        poll_seconds=1.0, turn_timeout=1800.0, ui_idle_timeout=300.0,
        rotate_after_turns=5, js_heap_limit_mb=0.0, dom_node_limit=0, edge_mb_limit=0.0,
    )
    argv = _controller_resume_argv(args, "job_1")
    i = argv.index("--commands-file")
    assert Path(argv[i + 1]) == commands.resolve()


def test_controller_resume_argv_omits_commands_file_when_using_the_default(tmp_path):
    from types import SimpleNamespace

    args = SimpleNamespace(
        state_dir=str(tmp_path), db=None, cdp_url="http://localhost:9222", commands_file=None,
        poll_seconds=1.0, turn_timeout=1800.0, ui_idle_timeout=300.0,
        rotate_after_turns=5, js_heap_limit_mb=0.0, dom_node_limit=0, edge_mb_limit=0.0,
    )
    argv = _controller_resume_argv(args, "job_1")
    assert "--commands-file" not in argv


def test_controller_cli_has_a_separate_child_command_channel_with_root_default():
    source = Path(__file__).with_name("local_loop_controller.py").read_text(encoding="utf-8")
    assert '"--commands-file"' in source
    assert 'commands_path=(Path(args.commands_file) if args.commands_file else' in source
    assert 'Path(args.state_dir) / "commands.json")' in source
