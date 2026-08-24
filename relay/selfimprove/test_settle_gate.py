"""整定ゲートは、遅い漏れを見抜けなければならない。

旧い判定は「0.5秒間隔で3回読んで幅 25MB 未満」= 毎秒 16.7MB 未満。捕まえたかった漏れは
毎秒約 3.2MB。tabs 走行の後に完全に放置したブラウザは最初の60秒で 190MB を手放したが、
この判定は 24アーム中央値 1.1秒で通過し、一度も 2.6秒を超えて待たなかった。

結果として全アームが「1分続く減衰の1秒目」で基準線を取り、その後 105〜236秒走った。
socket アームはレンダラを維持しないので自分の基準線の下で走り続け(終了時 -32〜-216MB)、
凍結判定器は開始値から上げるだけなので 0.0 を報告する。tabs は補充するので落ちない。
つまり比較は、ブラウザがたまたま漏らしていた量だけ候補側にボーナスを払っていた。
"""
import types

from relay.selfimprove import scheduler as S


class _Clock:
    """time を制御して、実時間を待たずに漏れを再現する。"""

    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


def _run_settle(series_fn, clock):
    """本体の settle_baseline を、制御された時計と合成した木の合計で回す。

    ループを再実装しない -- 再実装したテストは、本体と食い違ったまま緑であり続ける。
    前の版がまさにそれで、自分のコメントで『静止を待つ』と主張しながら待っていなかった。"""
    t0 = clock.time()
    level, waited, settled = S.settle_baseline(
        lambda: series_fn(clock.time() - t0),
        sleep=clock.sleep, now=clock.time)
    return waited, settled


MB = 1024.0 * 1024.0


def test_a_steady_drain_is_not_mistaken_for_rest():
    """実測された漏れ: 毎秒 3.2MB。これを『静止』と呼んではいけない。"""
    clock = _Clock()
    waited, settled = _run_settle(lambda dt: (1231.0 - 3.2 * dt) * MB, clock)
    assert not settled, "毎秒3.2MB 漏れているブラウザを整定と判定した"
    assert waited >= S.SETTLE_MAX_S - S.SETTLE_STEP_S


def test_the_old_criterion_would_have_passed_that_drain():
    """旧判定(0.5秒×3回、幅25MB)が本当に素通りだったことを、数として残す。
    直したつもりで直っていなかった経緯があるので、比較対象を明示しておく。"""
    drain_per_s = 3.2
    old_span_s = 2 * 0.5                      # 3標本 = 2間隔
    moved_mb = drain_per_s * old_span_s
    assert moved_mb < 25.0, "前提が違う"
    # 新判定は同じ漏れを見る窓が長い
    assert drain_per_s * S.SETTLE_WINDOW_S > S.SETTLE_TOLERANCE_MB


def test_a_browser_actually_at_rest_settles_quickly():
    """厳しくしすぎれば毎アーム2分待つことになる。静止しているものは速やかに通すこと。"""
    clock = _Clock()
    waited, settled = _run_settle(lambda dt: 900.0 * MB, clock)
    assert settled
    assert waited <= S.SETTLE_WINDOW_S + 2.0, "静止していても待ちすぎている"


def test_it_waits_out_a_drain_that_then_stops():
    """実際の形: 60秒ほど漏れて、そこで止まる。止まってから基準線を取れること。"""
    clock = _Clock()

    def series(dt):
        return (1231.0 - 3.2 * min(dt, 60.0)) * MB

    waited, settled = _run_settle(series, clock)
    assert settled, "漏れが止まった後も整定と認めていない"
    assert waited >= 60.0, "漏れが止まる前に基準線を取っている"


def test_the_window_must_be_long_enough_to_see_the_measured_drain():
    """定数そのものの健全性。窓×許容が漏れの速度を上回れば、また素通りする。"""
    assert S.SETTLE_TOLERANCE_MB / S.SETTLE_WINDOW_S < 3.2, (
        "許容速度が実測の漏れ以上 -- 直す前と同じ穴")
    assert S.SETTLE_MAX_S > 60.0, "漏れが止まる前に諦める上限"
