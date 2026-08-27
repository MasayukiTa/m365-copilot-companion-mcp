"""閉じた経路が開き直せるか -- 「関数が呼ばれたか」ではなく「ワーカーがどちらの経路を取ったか」で見る。

このファイルの各テストが答えている問いは一つだけ:

    3回の失敗で閉じたあと、次のワーカーは socket driver を受け取るのか、None を受け取って
    タブを開くのか。

`consider()` が True を返したかを見るテストは書かない。それは実装が「呼ばれた」ことしか
証明せず、今日の教訓そのもの -- 定義文字列に一致して合格していたテストと同じ形になる。
だからここでは毎回 SocketRoute を実物で組み、driver_for の戻り値で判定する。
"""
import time

import pytest

from relay.route_reopen import ReopenPolicy, reopenable
from relay.socket_route import SocketRoute


@pytest.fixture(autouse=True)
def _isolate_log(monkeypatch, tmp_path):
    """本物の .fleet/socket_route.jsonl を汚さない。合成行は学習データを静かに壊す。"""
    from relay import socket_route as SR
    monkeypatch.setattr(SR, "DEFAULT_LOG", str(tmp_path / "isolated.jsonl"))


class _Tpl:
    gpt_id = "T_agent.x"


def _token(seconds=3600):
    import base64
    import json

    body = json.dumps({"oid": "o", "tid": "t", "exp": time.time() + seconds}).encode()
    return "x." + base64.urlsafe_b64encode(body).decode().rstrip("=") + ".y"


def _live_route():
    """トークンを積んだ、実際に driver を配れる経路。"""
    r = SocketRoute(enabled=True, connect_fn=object(),
                    capture_fn=lambda *a, **k: (_token(), _Tpl()), log=lambda m: None)
    assert r.refresh(object(), "https://agent.example/x") is True
    assert r.driver_for("w0") is not None, "前提が壊れている: 閉じる前から driver が出ていない"
    return r


PROXY_502 = ("w5: ChatHubError: could not open the socket: "
             "InvalidProxyStatus: proxy rejected connection: HTTP 502")


def _close_over_transport(route):
    for _ in range(route.max_consecutive):
        route.note_failure(PROXY_502)
    assert route.driver_for("w6") is None, "前提が壊れている: 3連続失敗で閉じていない"


def _reopen(route_box, policy):
    """本番の reset_socket_route と同じこと -- 経路を捨て、次の呼び出しが新しいのを作る。"""
    if policy.consider(route_box[0]):
        route_box[0] = _live_route()
    return route_box[0]


# ---- 中心の問い ------------------------------------------------------------------------------

def test_a_worker_gets_the_socket_back_after_a_transport_close():
    """502で閉じ、プローブが通り、次のワーカーは**タブではなく socket** を受け取る。"""
    box = [_live_route()]
    _close_over_transport(box[0])

    clock = [1000.0]
    p = ReopenPolicy(now=lambda: clock[0], log=lambda m: None,
                     probe=lambda route: True, spawn=lambda fn: fn())

    assert _reopen(box, p).driver_for("w7") is None, "閉じた直後に開くのは早すぎる"
    clock[0] += p.wait_s + 1
    _reopen(box, p)                       # ここでプローブが走る(spawn が即実行)
    drv = _reopen(box, p).driver_for("w7")  # 次の通過で答えを読む

    assert drv is not None, "プローブが通ったのにワーカーはまだタブを開いている"
    assert p.reopens == 1


def test_a_worker_still_gets_a_tab_while_the_transport_is_down():
    """プローブが通らない間は閉じたまま。ここが緩むと『失敗ターンを毎回払う』に戻る。"""
    box = [_live_route()]
    _close_over_transport(box[0])

    clock = [1000.0]
    p = ReopenPolicy(now=lambda: clock[0], log=lambda m: None,
                     probe=lambda route: False, spawn=lambda fn: fn())
    for _ in range(6):
        clock[0] += p.wait_s + 1
        _reopen(box, p)
        _reopen(box, p)

    assert box[0].driver_for("w7") is None, "落ちたままの transport に socket を配っている"
    assert p.reopens == 0
    assert p.wait_s > p.__class__(now=lambda: 0).wait_s, "待ち時間が伸びていない"


def test_a_task_caused_close_stays_shut_even_though_the_socket_answers():
    """握手は通る。それでも開けてはいけない -- 閉じた理由が transport ではないから。

    握手が証明するのは『繋がる』だけで『backend がこの要求を受ける』ではない。
    ここを reason で止めなければ、片方向ブレーカが書かれた理由そのものを壊す。
    """
    box = [_live_route()]
    for _ in range(box[0].max_consecutive):
        box[0].note_failure("w2: the turn completed but carried no text")
    assert box[0].driver_for("w6") is None

    clock = [1000.0]
    p = ReopenPolicy(now=lambda: clock[0], log=lambda m: None,
                     probe=lambda route: True, spawn=lambda fn: fn())
    for _ in range(4):
        clock[0] += 10_000
        _reopen(box, p)

    assert box[0].driver_for("w7") is None, "task 由来の閉鎖を握手の成功で開けてしまった"
    assert p.probes == 0, "reason で止まるべきところで握手を試している"


def test_an_unclassified_close_stays_shut():
    """`unknown` は『まだ誰も分類していない』であって『一時的』ではない。

    ここを開けると、分類器を伸ばす代わりに未分類を無罪放免にする癖がつく。
    """
    assert reopenable("3 consecutive failures, last: w1: something nobody has classified") is False


def test_the_flap_guard_close_stays_shut():
    """散発的な失敗が積み上がって閉じた場合は開け直さない。

    これは一つの事故ではなく『この経路は割に合わない』という判断で、握手はそれに答えない。
    """
    r = SocketRoute(enabled=True, connect_fn=object(), max_fallbacks=3,
                    capture_fn=lambda *a, **k: (_token(), _Tpl()), log=lambda m: None)
    r.refresh(object(), "https://agent.example/x")
    for _ in range(3):
        r.note_failure(PROXY_502)
        r.note_success()               # 連続カウンタは毎回リセット -> flap guard 側で閉じる
    assert "not reliable enough" in r.closed_reason, "この試験が狙った閉じ方をしていない"
    assert reopenable(r.closed_reason) is False


# ---- 計測できなかった、を計測して悪かった、と混同しない --------------------------------------

def test_a_probe_that_could_not_run_is_not_reported_as_a_dead_transport():
    """プローブ自身が落ちたときは、そう言う。

    websocket_connect は自前の event loop を張るので、Playwright の sync API が回っている
    スレッドから呼ぶと "Cannot run the event loop while another loop is running" で落ちる。
    それを except でまとめて False にすると、**transport が死んでいる証拠として記録される**。
    今日だけで3回踏んだ形なので、文言で区別できることをテストで固定する。
    """
    said, clock = [], [1000.0]
    p = ReopenPolicy(now=lambda: clock[0], log=said.append, spawn=lambda fn: fn(),
                     probe=lambda route: (_ for _ in ()).throw(
                         RuntimeError("Cannot run the event loop while another loop is running")))
    box = [_live_route()]
    _close_over_transport(box[0])
    p.consider(box[0])                 # 閉鎖を見る
    clock[0] += p.wait_s + 1
    p.consider(box[0])                 # プローブ実行
    p.consider(box[0])                 # 答えを読む

    joined = " ".join(said)
    assert "the probe itself failed" in joined, "プローブの故障を transport の故障として記録した"
    assert box[0].driver_for("w7") is None


def test_a_probe_is_not_started_twice_while_one_is_in_flight():
    """握手は最大15秒かかる。毎回の通過で新しいスレッドを撒いてはいけない。"""
    started, clock = [], [10_000.0]
    p = ReopenPolicy(now=lambda: clock[0], log=lambda m: None,
                     probe=lambda route: True, spawn=started.append)  # 実行しない = 飛行中のまま
    box = [_live_route()]
    _close_over_transport(box[0])
    p.consider(box[0])
    for _ in range(5):
        clock[0] += p.wait_s + 1       # 待ち時間を過ぎても、飛行中なら撒かないこと
        p.consider(box[0])
    assert len(started) == 1, "飛行中のプローブに重ねて %d 本撒いた" % len(started)


def test_consider_returns_promptly_when_the_real_probe_is_used():
    """admission ループを塞がないこと。spawn を渡さなければスレッドに出る。"""
    box = [_live_route()]
    _close_over_transport(box[0])
    clock = [10_000.0]
    p = ReopenPolicy(now=lambda: clock[0], log=lambda m: None,
                     probe=lambda route: time.sleep(5) or True)
    p.consider(box[0])
    clock[0] += p.wait_s + 1
    t0 = time.time()
    p.consider(box[0])                 # 本物の spawn = スレッド
    assert time.time() - t0 < 1.0, "呼び出し側スレッドでプローブを走らせている"


# ---- 支援シグナル(D) -------------------------------------------------------------------------

def test_a_turn_completed_elsewhere_counts_as_evidence():
    """別サーフェスがソケットでターンを完了していれば、握手より強い証拠。

    握手は『繋がる』止まりだが、完了したターンは要求が丸ごと通ったことを意味する。
    ただし副エージェントが動いていたときにしか手に入らないので、機構にはできない。
    """
    box = [_live_route()]
    _close_over_transport(box[0])

    clock = [1000.0]
    boom = lambda route: pytest.fail("支援シグナルがあるのに握手を試した")  # noqa: E731
    p = ReopenPolicy(now=lambda: clock[0], log=lambda m: None,
                     probe=boom, spawn=lambda fn: fn())
    p.consider(box[0])                 # 閉鎖を見て turns を記憶
    box[0].note_success()              # 別サーフェスが1ターン通した
    clock[0] += p.wait_s + 1
    _reopen(box, p)
    assert _reopen(box, p).driver_for("w7") is not None


def test_the_route_reopens_only_once_per_close():
    """開き直したら状態は初期化される -- 次の閉鎖はまた最初の待ち時間から始まる。"""
    box = [_live_route()]
    _close_over_transport(box[0])
    clock = [1000.0]
    p = ReopenPolicy(now=lambda: clock[0], log=lambda m: None,
                     probe=lambda route: True, spawn=lambda fn: fn())
    _reopen(box, p)                    # 閉鎖を見る
    clock[0] += p.wait_s + 1
    _reopen(box, p)                    # プローブ
    _reopen(box, p)                    # 答えを読む
    assert p.reopens == 1
    assert p.closed_at is None and p.next_probe_at is None

    _close_over_transport(box[0])
    p.consider(box[0])
    assert p.wait_s == ReopenPolicy(now=lambda: 0).wait_s, "待ち時間が前の閉鎖を引きずっている"


# ---- 実運用の理由文字列 ----------------------------------------------------------------------

def test_every_close_on_record_classifies_the_way_the_gate_expects():
    """記録に残っている5件の閉鎖 -- 4件が transport、1件は私が仕込んだ強制失敗。

    これは分類器のテストではなく、**この門が実際の文字列に対して意図通り倒れるか**の固定。
    """
    assert reopenable("3 consecutive failures, last: " + PROXY_502) is True
    assert reopenable("3 consecutive failures, last: w9: ConnectionClosedError: "
                      "no close frame received") is True
    assert reopenable("3 consecutive failures, last: refuter: ChatHubError: "
                      "forced failure (MCP_SOCKET_FORCE_FAIL), 3 left") is False
    assert reopenable("") is False
    assert reopenable(None) is False
