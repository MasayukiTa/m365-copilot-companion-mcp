from relay import relay_fleet as RF
from relay.test_verify_delivery_before_giving_up import _Worker


class Thread:
    alive = True
    def is_alive(self):
        return self.alive


def worker(monkeypatch):
    w = _Worker()
    w.socket = True
    w.drv._thread = Thread()
    w.drv.generation_idle_s = lambda: w.idle
    w.idle = 10
    w._retry_socket = RF.RelayWorker._retry_socket.__get__(w)
    w._journal_shows_executed_calls = lambda: False
    w._refuse_continue = RF.RelayWorker._refuse_continue.__get__(w)
    w._plan_continue = lambda *a: (_ for _ in ()).throw(AssertionError('planned while alive'))
    monkeypatch.setattr(RF, '_socket_route', lambda: object())
    monkeypatch.setattr(RF.RelayWorker, '_socket_tool_idle_s', lambda *a: None)
    return w


def test_live_turn_is_waited_without_planning_or_resending(monkeypatch):
    w = worker(monkeypatch)
    assert w._retry_socket('turn timeout without a reply', assume_landed=True)
    assert w.poll() is False and w.status == 'waiting'
    w.idle = RF.LIVE_TURN_SILENCE_S - 1
    assert w.poll() is False
    w.idle = 0  # new meaningful progress resets the silence clock
    assert w.poll() is False and w.goal == 'send an email'


def test_silent_live_thread_refuses_honestly(monkeypatch):
    w = worker(monkeypatch)
    w._retry_socket('turn timeout without a reply', assume_landed=True)
    w.idle = RF.LIVE_TURN_SILENCE_S
    assert w.poll() is True and w.status == 'stuck'
    assert 'still alive' in w.reason and 'outcome is unknown' in w.reason


def test_dead_thread_continues_once(monkeypatch):
    w = worker(monkeypatch)
    w._retry_socket('turn timeout without a reply', assume_landed=True)
    w.drv._thread.alive = False
    calls = []
    def retry(reason, assume_landed=False):
        calls.append((reason, assume_landed))
        w.status = 'ready'
        return True
    w._retry_socket = retry
    assert w.poll() is False
    assert calls == [('turn timeout without a reply', True)]
    assert w._live_turn_wait is None


def test_reply_wins_when_thread_finishes(monkeypatch):
    w = worker(monkeypatch)
    w._retry_socket('turn timeout without a reply', assume_landed=True)
    w.drv._thread.alive = False
    class Answers:
        def count(self): return 1
    w.drv._answers = lambda: Answers()
    w._report_socket_turns = lambda: None
    # Reaching the ordinary reply handling proves no recovery was started.
    monkeypatch.setattr(RF.RelayWorker, '_check_tools_attached',
                        lambda self: setattr(self, 'status', 'done'))
    assert w.poll() is True
    assert w._live_turn_wait is None and w.status == 'done'
