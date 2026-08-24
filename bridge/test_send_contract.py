"""/send must not report success about somebody else's message.

The endpoint queues and returns immediately, which is correct: it is store-only by design so
it stays responsive while a long run holds the page. What it used to return was
{"ok": true, "queued": true} -- the truth about the endpoint, and a lie about the operator's
message. The queue is drained only by a running /goal loop at its next turn boundary, or by a
/stream turn right after that turn completes. With neither running, a message sits there
indefinitely: one has been in this machine's store since 07-07, in a session still marked
active, which is exactly what "I typed into main and nothing happened" looks like from inside.

Source-level, like the sibling bridge suites: copilot_bridge.py imports Playwright at module
scope and cannot be imported on a runner.

Run: pytest -q bridge/test_send_contract.py
"""
from pathlib import Path

SOURCE = (Path(__file__).with_name("copilot_bridge.py")).read_text(encoding="utf-8")


def _send_branch() -> str:
    body = SOURCE[SOURCE.index('if parsed.path == "/send":'):]
    return body[:body.index('if parsed.path == "/history":')]


def test_the_reply_says_whether_anyone_is_coming_for_it():
    body = _send_branch()
    assert '"consumer_running": consumer_running' in body
    assert "PAGE_LOCK.locked()" in body


def test_the_idle_case_says_so_in_words_and_not_only_in_a_flag():
    """A boolean nobody renders is the same silence in a different shape."""
    body = _send_branch()
    assert "NOTHING IS RUNNING" in body
    assert "stays queued until then" in body


def test_the_reply_carries_the_queue_depth():
    """One stuck message and forty look identical without it."""
    body = _send_branch()
    assert '"queue_depth": depth' in body


def test_it_still_queues_and_still_returns_immediately():
    """The concurrency contract is why this endpoint exists: /send never touches PAGE, so it
    stays answerable while a run holds the lock."""
    body = _send_branch()
    assert "_queue_input_locked(sid, msg)" in body
    # The comments discuss both of these by name; the CODE must contain neither.
    code = chr(10).join(l.split("#")[0] for l in body.splitlines())
    assert "run_on_page_thread" not in code
    assert "PAGE_LOCK.acquire" not in code


def test_it_reports_rather_than_acts():
    """Promoting an idle /send into a turn means touching the page while a person may be using
    it -- a separate decision with its own risks, not a detail of this one."""
    body = _send_branch()
    for started in ("_run_one_turn", "_stream_text", "_run_work_phase"):
        assert started not in body, started


def test_a_failure_to_count_does_not_fail_the_send():
    body = _send_branch()
    i = body.index('depth = len(')
    assert "except Exception:" in body[i:i + 260]
    assert "depth = -1" in body
