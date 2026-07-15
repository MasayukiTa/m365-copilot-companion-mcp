import json

from relay.local_job_store import LocalJobStore
from relay.local_loop_controller import (
    LocalLoopController,
    _AUTH_ACTION_SELECTOR,
    _CREDENTIAL_INPUT_SELECTOR,
    _close_driver_page,
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
