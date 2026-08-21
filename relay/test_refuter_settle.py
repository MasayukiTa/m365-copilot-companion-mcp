"""Refuter の settle 経路。4実装のうち最弱で、best-of-N のセレクタでもある。

旧経路は dwell を1回待つだけ -- サンプル要求もマーカー概念も無い。だから書きかけの
反証文でも、その時点までの文字列から判定語が抽出できてしまえば確定する。
プロジェクト自身の記述が「セレクタの質が出力品質の上限」と認めている当の場所。

統一経路のマーカーは「判定語が抽出できたか」。判定語が読めない返信は preamble か
未完成のどちらかで、どちらも長い settle を望む。既存の UNCLEAR→nudge 経路とは
別の問いに答えている点に注意 -- nudge は「モデルが断定を避けた」、マーカーは
「本文が動き止まったか」。
"""
import time

from relay import refuter as R
from relay import settle as S


class _Drv:
    def __init__(self, script):
        self.script = list(script)
        self.i = 0
        self.accepted = []

    def _answers(self):
        class _L:
            def count(_self):
                return 99
        return _L()

    def read_last_response(self):
        t = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return t

    def _is_stale_repeat(self, _t):
        return False

    def _accept_new_reply(self, t):
        self.accepted.append(t)

    def send(self, _text):
        pass


class _Session:
    """`poll` が settle 部分へ届くのに要る最小の器。"""

    timeout_s = 10_000
    dwell_s = 2.0
    max_nudges = 0
    page = None

    def __init__(self, script):
        self.drv = _Drv(script)
        self._count_before = 0
        self._t_send = time.time()
        self._last = None
        self._stable_since = None
        self._settle_state = S.SettleState()
        self._nudges_used = 0
        self._done = None          # 実物と同じ契約: _finish が入れるまで None
        self._pending_open = False
        # A TAB stand-in: these tests are about settle rules on DOM text, and a socket review
        # ends by protocol instead of by settling.
        self.socket = False
        self._socket_tried = True
        self.finished = []

    def _finish(self, verdict):
        self.finished.append(verdict)
        self._done = verdict

    def _schedule_network_reopen(self, _why):
        pass

    poll = R.RefuterSession.poll
    # 実物から借りる。束縛し忘れると poll の包括 except が AttributeError を
    # 拾って UNCLEAR に変えるので、器の不備がテストの結論に化ける。
    _nudge = R.RefuterSession._nudge


def _run(script, *, unified, monkeypatch, polls=6, tick=3.0):
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "1" if unified else "0")
    s = _Session(script)
    base = [time.time()]
    monkeypatch.setattr(time, "time",
                        lambda: base.__setitem__(0, base[0] + tick) or base[0])
    for _ in range(polls):
        s.poll()
        if s._done is not None:
            break
    return s


def test_a_verdict_that_parses_is_still_the_marker_and_the_reply_settles(monkeypatch):
    """統一しても、完成した反証文は従来どおり確定する。"""
    text = "REFUTED: the patch drops the trailing newline"
    s = _run([text] * 6, unified=True, monkeypatch=monkeypatch)
    assert s.finished and s.finished[0][0] == "REFUTED"


def test_the_legacy_path_commits_a_half_written_refutation(monkeypatch):
    """1 dwell 待っただけで、まだ書きかけの本文が判定になる -- 直したい失敗。"""
    half = "REFUTED: the patch drops the trai"
    s = _run([half], unified=False, monkeypatch=monkeypatch, polls=2, tick=5.0)
    assert s.finished, "旧経路が確定しなかった -- 前提が変わっている"


def test_the_unified_path_does_not_commit_it_at_the_same_poll(monkeypatch):
    """同じ入力・同じ回数で比べる。回数を変えると『厳しくなった』ではなく
    『ポーリング数が違った』を測ることになる。"""
    half = "REFUTED: the patch drops the trai"
    s = _run([half], unified=True, monkeypatch=monkeypatch, polls=2, tick=5.0)
    assert s.finished == []


def test_a_preamble_gets_the_longer_settle_because_no_verdict_parses(monkeypatch):
    """判定語が読めない本文はマーカー無し扱い -- サンプル数も dwell も倍。"""
    preamble = "Let me look at the diff and check whether it holds."
    short = _run([preamble], unified=True, monkeypatch=monkeypatch, polls=3, tick=1.0)
    assert short.finished == []
    long_enough = _run([preamble], unified=True, monkeypatch=monkeypatch, polls=12, tick=2.0)
    assert long_enough.finished and long_enough.finished[0][0] == "UNCLEAR", (
        "マーカー無しは遅延であって永久拒否ではない -- でなければ preamble で必ず宙吊り")


def test_a_stale_repeat_is_still_refused(monkeypatch):
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "1")
    s = _Session(["UPHELD: it is fine"] * 10)
    s.drv._is_stale_repeat = lambda _t: True
    base = [time.time()]
    monkeypatch.setattr(time, "time",
                        lambda: base.__setitem__(0, base[0] + 3.0) or base[0])
    for _ in range(10):
        s.poll()
        if s._done is not None:
            break
    assert s.finished == [] and s.drv.accepted == []


def test_a_nudge_restarts_the_settle_rather_than_carrying_it(monkeypatch):
    """nudge は新しいターン。跨いで安定を持ち越すと、まだ訊いていない質問への
    回答を受理するのに preamble で貯めた settle が使われる。"""
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "1")
    # `_is_processing("thinking...")` は True -- 短すぎる読みは「まだ生成中」扱い。
    # processing の文字列だと settle 以前で skip され続け、nudge の検証にならない。
    s = _Session(["Let me look at the diff and check whether it holds."] * 30)
    s.max_nudges = 1
    base = [time.time()]
    monkeypatch.setattr(time, "time",
                        lambda: base.__setitem__(0, base[0] + 3.0) or base[0])
    for _ in range(30):
        s.poll()
        if s._done is not None:
            break
    assert s._nudges_used == 1
    assert s._settle_state.stable_count < 30, "nudge をまたいで蓄積が持ち越されている"
