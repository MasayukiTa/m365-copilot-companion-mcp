"""空のレポートには必ず理由が付く。理由の無い空は、調べようがない障害になる。

実測 (2026-08-21): 実機で research を1本通したところ、承認は21秒で自動送信され、
93秒で計画ブロックが1529文字まで育ち、103秒で**空**を返して終了した。
タイムアウトは1500秒。つまり例外が握り潰され、「クラッシュした research」と
「何も見つからなかった research」が外から完全に同じ見た目になっていた。
"""
import types

import pytest

from relay import agent_profiles as AP


def _session():
    s = AP.ResearchSession.__new__(AP.ResearchSession)
    s._done = None
    s.error = ""
    s.tx_dir = None
    s.parent_key = ""
    s._report_full = ""
    s.page = None
    s.drv = None
    return s


def test_an_empty_report_always_carries_a_reason():
    s = _session()
    s._fail("timeout: 1500s without a finished report")
    assert s._done == ""
    assert "timeout" in s.error


def test_a_swallowed_exception_is_no_longer_swallowed():
    """poll() の except 節がここに来る。理由が消えると、1500秒の予算に対して
    103秒で空を返した事実が『調査が何も見つけなかった』に化ける。"""
    s = _session()
    s._t_send = 0.0
    s.timeout_s = 1500
    s._pending_open = False

    class _Boom:
        def _answers(self):
            raise RuntimeError("Target page, context or browser has been closed")

    s.drv = _Boom()
    import time as _t
    s._t_send = _t.time()
    out = s.poll()
    assert out == ""
    assert "RuntimeError" in s.error
    assert "closed" in s.error


def test_the_reason_distinguishes_the_ways_of_failing():
    """RAM が無かった / 時間切れ / 落ちた は運用上まったく別の話で、
    同じ '' で表現されていると、どれを直せばよいか決められない。"""
    reasons = []
    for r in ("no tab within 600s: the box had no RAM for a side page",
              "timeout: 1500s without a finished report",
              "TargetClosedError: page closed"):
        s = _session()
        s._fail(r)
        reasons.append(s.error)
    assert len(set(reasons)) == 3


def test_a_session_that_succeeded_carries_no_reason():
    s = _session()
    s._finish("本文" * 100)
    assert s.error == ""
    assert s._done.startswith("本文")
