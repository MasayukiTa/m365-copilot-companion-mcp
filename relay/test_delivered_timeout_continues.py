from relay import delivery_verify as DV
from relay import relay_fleet as RF
from relay.test_verify_delivery_before_giving_up import _Worker


class Route:
    def open(self):
        return True


def test_delivered_wait_then_continues_once(monkeypatch):
    monkeypatch.setattr(DV, 'verify_on', lambda *args: True)
    monkeypatch.setattr(RF, '_socket_route', lambda: Route())
    w = _Worker(partial='progress')
    w.socket = True
    w._report_socket_turns = lambda: None
    w._t_send = RF.time.time() - 2000
    calls = []
    def retry(reason, assume_landed=False):
        calls.append((reason, assume_landed))
        w.status = 'ready'
        w.job = 'CONTINUE with progress'
        return True
    w._retry_socket = retry
    assert w.poll() is False
    assert w.status == 'waiting' and w.outcome is None and not calls
    w._delivered_wait_deadline = 0
    assert w.poll() is False
    assert calls == [('turn timeout without a reply', True)]
    assert w.status == 'ready' and w.job == 'CONTINUE with progress'


def test_expired_wait_refusal_is_terminal_and_truthful(monkeypatch):
    monkeypatch.setattr(DV, 'verify_on', lambda *args: True)
    monkeypatch.setattr(RF, '_socket_route', lambda: Route())
    w = _Worker(partial='progress')
    w.socket = True
    w._report_socket_turns = lambda: None
    w._t_send = RF.time.time() - 2000
    w._retry_socket = lambda *a, **k: False
    assert w.poll() is False
    w._delivered_wait_deadline = 0
    assert w.poll() is True
    assert w.status == 'stuck' and 'waiting' not in w.reason
    assert w.refused[1] == 'delivered'
