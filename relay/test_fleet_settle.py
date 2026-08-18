"""フリートワーカーの settle 経路を、実際に駆動して確かめる。

この経路にはテストが一つも無かった -- 4実装のうち最弱のものが、最もテストされていない。
`_stable_since` しか持たず、サンプル要求が無いので、dwell より長いストリーミングの一時停止は
そのまま「安定した最終回答」になる。3,931返信の実測が正当化したガードは、ここには一度も
適用されていない。

ゲート `MCP_SETTLE_UNIFIED` がその差そのものなので、両側を同じ入力で走らせて比べる。
片側だけ通しても「新経路が走った」証拠にはならない -- そのテストが settle ループに
触れてすらいない可能性がある。
"""
import time

import pytest

from relay import relay_fleet as RF
from relay import settle as S

TERMINAL_DECIDED = object()


class _Drv:
    """必要最小限のドライバ。read_last_response が台本を順に返す。"""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0
        self.accepted = []

    def _answers(self):
        class _L:
            def count(_self):
                return 99
        return _L()

    def _is_generating(self):
        return False

    def read_last_response(self):
        text = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return text

    def _is_stale_repeat(self, _t):
        return False

    def _accept_new_reply(self, t):
        self.accepted.append(t)


class _Worker:
    """`poll` が settle 部分へ到達するのに要る属性だけを備えた器。

    実 `_FleetWorker` を構築するとフリート一式が要るので、束縛されていないメソッドを
    この器に付けて回す。器が足りなければ AttributeError で即座に分かる -- 黙って
    別経路へ落ちるより良い。
    """

    status = "waiting"
    per_turn_timeout_s = 10_000
    dwell_s = 2.0
    _count_before = 0
    transient = 0
    max_transient = 3
    last_response = ""

    def __init__(self, script):
        self.drv = _Drv(script)
        self._t_send = time.time()
        self._last_text = None
        self._stable_since = None
        self._settle_state = S.SettleState()
        self.decided = []

    def _capture_url(self):
        pass

    def _decide(self, t):
        self.decided.append(t)
        self.status = "done"

    poll = RF.RelayWorker.poll


def _run(script, *, unified, monkeypatch, polls=8, tick=1.5):
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "1" if unified else "0")
    w = _Worker(script)
    base = [time.time()]

    def fake_time():
        base[0] += tick
        return base[0]

    monkeypatch.setattr(RF.time, "time", fake_time)
    for _ in range(polls):
        if w.poll():
            break
    return w


# ---- 差そのもの ---------------------------------------------------------------------------------

def test_the_legacy_path_accepts_a_paused_partial_that_outlasts_the_dwell(monkeypatch):
    """旧経路にはサンプル要求が無い。dwell を超える一時停止があれば、
    途中で切れた本文がそのまま最終回答として確定する -- これが直したい失敗。"""
    partial = "回答の途中で切れて"
    w = _run([partial], unified=False, monkeypatch=monkeypatch, polls=2, tick=5.0)
    assert w.decided == [partial], "旧経路が途中形を確定しなかった -- 前提が変わっている"


def test_the_unified_path_refuses_the_same_partial(monkeypatch):
    """同じ入力で、サンプル要求とマーカー倍化が効く。"""
    partial = "回答の途中で切れて"
    # 同じ回数・同じ間隔。旧経路が確定するちょうどその時点で比べないと、
    # 「厳しくなった」ではなく「ポーリング回数が違った」を測ることになる。
    w = _run([partial], unified=True, monkeypatch=monkeypatch, polls=2, tick=5.0)
    assert w.decided == [], "統一経路が途中形を受理した"


def test_the_unified_path_still_accepts_a_genuinely_settled_reply(monkeypatch):
    """厳しくするだけなら簡単。完成した返信は従来どおり確定しなければ意味が無い。"""
    final = "完成した回答です DONE"
    w = _run([final] * 8, unified=True, monkeypatch=monkeypatch, tick=2.0)
    assert w.decided == [final]
    assert w.drv.accepted == [final], "_accept_new_reply が呼ばれていない"


def test_a_markerless_reply_needs_longer_but_is_not_refused_forever(monkeypatch):
    """マーカー無しは遅延であって禁止ではない。永久に確定しないなら、
    プロトコル語を吐かないエージェントが全員 STUCK になる。"""
    plain = "マーカーの無い普通の答え"
    short = _run([plain] * 4, unified=True, monkeypatch=monkeypatch, polls=4, tick=1.0)
    assert short.decided == []
    long_enough = _run([plain] * 20, unified=True, monkeypatch=monkeypatch, polls=20, tick=2.0)
    assert long_enough.decided == [plain]


# ---- 統一経路でも壊してはいけないもの -----------------------------------------------------------

def test_a_stale_repeat_is_still_refused_in_the_unified_path(monkeypatch):
    """前ターンの回答と同一の本文を今ターンの答えとして採らない
    （idle tool probe 事故の署名）。"""
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "1")
    w = _Worker(["同じ本文 DONE"] * 10)
    w.drv._is_stale_repeat = lambda _t: True
    base = [time.time()]
    monkeypatch.setattr(RF.time, "time", lambda: base.__setitem__(0, base[0] + 2.0) or base[0])
    for _ in range(10):
        if w.poll():
            break
    assert w.decided == [] and w.drv.accepted == []


def test_a_placeholder_does_not_destroy_stability_in_the_unified_path(monkeypatch):
    """旧経路は placeholder ごとにカウンタを捨てる。統一経路は skip する
    -- 2026-08-10 の実測に基づく正典側の知見。"""
    final = "完成した回答です DONE"
    script = [final, "処理中です。", final, final, final, final]
    w = _run(script, unified=True, monkeypatch=monkeypatch, polls=6, tick=2.0)
    assert w.decided == [final]


def test_the_legacy_fields_stay_in_step_so_the_cockpit_does_not_look_stalled(monkeypatch):
    """cockpit と resume 経路が `_last_text` / `_stable_since` を読む。
    統一経路でそこが凍ると、走っているワーカーが永久停止に見える。"""
    final = "答え DONE"
    w = _run([final] * 3, unified=True, monkeypatch=monkeypatch, polls=2, tick=1.0)
    assert w._last_text == final and w._stable_since is not None


def test_the_gate_selects_the_path_rather_than_both_running(monkeypatch):
    """両方走ると、遅いほうの結論が勝つ位置次第で説明できない挙動になる。"""
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "0")
    assert S.unified() is False
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "1")
    assert S.unified() is True
