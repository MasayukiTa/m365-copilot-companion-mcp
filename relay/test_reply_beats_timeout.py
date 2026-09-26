import time

from relay import relay_fleet as RF
from relay import settle as S


class _Answers:
    def __init__(self, n):
        self.n = n
    def count(self):
        return self.n


class _Driver:
    def __init__(self, count, text='answer DONE'):
        self.count = count
        self.text = text
        self.accepted = []
    def _answers(self):
        return _Answers(self.count)
    def _is_generating(self):
        return False
    def read_last_response(self):
        return self.text
    def _is_stale_repeat(self, _text):
        return False
    def _accept_new_reply(self, text):
        self.accepted.append(text)


class _Worker:
    poll = RF.RelayWorker.poll
    status = 'waiting'
    per_turn_timeout_s = 240
    dwell_s = 4.0
    _count_before = 0
    transient = 0
    max_transient = 10
    last_response = ''

    def __init__(self, count):
        self.drv = _Driver(count)
        self._t_send = time.time() - 300
        self._last_text = None
        self._stable_since = None
        self._settle_state = S.SettleState()
        self.retried = False
        self.outcome = None
        self.reason = ''

    def _capture_url(self):
        pass

    def _retry_transient(self):
        self.retried = True
        self.status = 'ready'
        self.transient += 1
        return True

    def _note_timeout(self, *args, **kwargs):
        pass

    def _salvage_via_checks(self):
        return False

    def _decide(self, text):
        self.last_response = text
        self.status = 'done'
        self.outcome = 'DONE'


def test_a_reply_that_already_exists_beats_the_outer_timeout(monkeypatch):
    monkeypatch.setenv('MCP_SETTLE_UNIFIED', '0')
    w = _Worker(count=1)
    assert w.poll() is False
    assert w.retried is False, 'a reply already exists; retrying duplicates work instead of consuming it'
    assert w.status == 'waiting'
    assert w._last_text == 'answer DONE', 'the existing reply must enter the normal settle path'


def test_a_real_timeout_with_no_reply_still_retries(monkeypatch):
    monkeypatch.setenv('MCP_SETTLE_UNIFIED', '0')
    w = _Worker(count=0)
    assert w.poll() is False
    assert w.retried is True
    assert w.status == 'ready'
