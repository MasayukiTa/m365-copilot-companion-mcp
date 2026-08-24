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


def test_the_idle_case_says_what_happened_and_not_only_a_flag():
    """A boolean nobody renders is the same silence in a different shape."""
    body = _send_branch()
    assert "a turn was started for this" in body
    assert "it stays queued instead" in body


def test_an_idle_send_starts_a_turn():
    """Queuing was always correct; waiting to be noticed was not. Approved by the operator
    after being told what it changes: a turn can begin at the moment they send, on a page
    they may be using."""
    body = _send_branch()
    assert "if not consumer_running:" in body
    assert "run_on_page_thread(self._drain_pending_queue, sid)" in body
    assert '"promoted": promoted' in body


def test_the_promotion_is_bounded_to_the_moment_of_sending():
    """Not a background drainer: a daemon that dies leaves exactly today's silence while the
    interface reports itself healthy."""
    body = _send_branch()
    assert "while True" not in body
    assert "schedule" not in body.lower()
    assert "_drain_pending_queue" in body
    assert body.count("threading.Thread") == 1


def test_losing_the_race_for_the_page_leaves_it_queued():
    """Between the test above and the thread starting, a /goal or /stream may take the page.
    That is the old behaviour and it is safe; taking the page from them is not."""
    body = _send_branch()
    assert "if not PAGE_LOCK.acquire(blocking=False):" in body
    assert "PAGE_LOCK.release()" in body


def test_a_promoted_turn_that_throws_does_not_take_the_lock_with_it():
    body = _send_branch()
    i = body.index("run_on_page_thread(self._drain_pending_queue, sid)")
    tail = body[i:i + 400]
    assert "except Exception:" in tail
    assert "finally:" in tail


def test_the_reply_carries_the_queue_depth():
    """One stuck message and forty look identical without it."""
    body = _send_branch()
    assert '"queue_depth": depth' in body


def test_it_still_queues_and_still_returns_immediately():
    """The concurrency contract is why this endpoint exists: /send never touches PAGE, so it
    stays answerable while a run holds the lock."""
    body = _send_branch()
    assert "_queue_input_locked(sid, msg)" in body
    # The reply is sent before any turn begins: the promotion runs on its own thread, so this
    # endpoint stays answerable while a run holds the page, which is why it exists.
    # rfind: the FIRST self._json in this branch is the empty-message error, not the reply.
    i_reply = body.rfind("self._json({")
    i_thread = body.index("threading.Thread(target=_promote")
    assert i_thread < i_reply, "the thread is started first, but it must not block the reply"
    # `def _promote():` contains the same characters, so the check is on statements.
    assert not [l for l in body.splitlines() if l.strip() == "_promote()"],         "started on a thread, never called inline"


def test_it_reuses_the_existing_drain_rather_than_a_new_turn_path():
    """_drain_pending_queue already sends with no SSE consumer and persists the exchange the
    same way a normal turn does. A second way to run a turn is a second way to be wrong."""
    body = _send_branch()
    assert "_drain_pending_queue" in body
    for invented in ("_run_one_turn", "_stream_text", "_run_work_phase"):
        assert invented not in body, invented


def test_a_failure_to_count_does_not_fail_the_send():
    body = _send_branch()
    i = body.index('depth = len(')
    assert "except Exception:" in body[i:i + 260]
    assert "depth = -1" in body
