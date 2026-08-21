"""判定規則そのものを、生きたフリート抜きで確かめる。

`free_disk_gb` を明示するのは、preflight がディスクの床も見るようになったから。
世界に触る前提条件は、世界についてでないテストではスタブする -- トークン探査を
実物にした日にスイートが10分ハングしたのと同じ教訓。

この系がこれまで測定に到達できなかったのは、どの座標も「両腕が同じプログラムになる」
という理由で正当に拒否されたから。経路ポリシーは対照群が既に存在する唯一の族で、
socket と tab は同じ目標を同じフリートで走らせ、輸送だけが違う。

規則を純関数に分けてあるのは、**数値に対して**検証できるようにするため。生きた
フリートでしか試せない判定規則は、実質一度も検証されない。
"""
import pytest

from relay.selfimprove import route_evaluator as RE


# ---- 走らせてはいけない条件 ---------------------------------------------------------------------

def test_a_swapping_machine_is_refused_rather_than_measured():
    """測っている量そのものがメモリなので、スワップ中の腕はスワップを測る。
    2026-08-21 に実際これで片腕を落としている。"""
    reasons = RE.preflight(free_mb=300.0, token_ok=True, free_disk_gb=500.0)
    assert len(reasons) == 1
    assert "swap" in reasons[0]


def test_no_token_is_refused_because_the_arms_would_be_identical():
    """トークンが無ければ socket 腕は黙ってタブ腕になる。
    『実験は問題なく走った』という顔をして入ってくる、最悪の同一化。"""
    reasons = RE.preflight(free_mb=8000.0, token_ok=False, free_disk_gb=500.0)
    assert len(reasons) == 1
    assert "same program" in reasons[0]


def test_both_refusals_are_reported_together():
    """1つ直して走らせ、もう1つで落ちる、を避ける。"""
    assert len(RE.preflight(free_mb=100.0, token_ok=False, free_disk_gb=500.0)) == 2


def test_a_healthy_box_with_a_token_may_run():
    assert RE.preflight(free_mb=8000.0, token_ok=True, free_disk_gb=500.0) == []


# ---- 判定 ---------------------------------------------------------------------------------------

def _arm(done, peak_mb, goals=4):
    return {"done": done, "peak_mb": peak_mb, "goals": goals}


def test_losing_completion_is_a_reject_whatever_the_memory_says():
    """経路は速度であって能力ではない。完了が落ちたらメモリがいくら良くても駄目。"""
    got = RE.decide(_arm(4, 1653.0), _arm(3, 5.0))
    assert got["verdict"] == "reject"
    assert "never a capability" in got["why"]


def test_equal_completion_with_a_real_memory_gain_is_a_keep():
    got = RE.decide(_arm(4, 1653.0), _arm(4, 205.0))
    assert got["verdict"] == "keep"
    assert got["memory_gain_mb"] == 1448.0


def test_a_gain_inside_the_noise_is_inconclusive_and_says_so():
    """ここが要。REJECT と混ぜると、『何も分からなかった』が
    『悪かった』として最適化器に教えられる。台帳が INFRA_ABORT を
    verdict と分けているのと同じ規律を、結果の側にも当てる。"""
    got = RE.decide(_arm(4, 500.0), _arm(4, 400.0))
    assert got["verdict"] == "inconclusive"
    assert "not a finding that the route is worse" in got["why"]


def test_improving_completion_and_memory_is_still_a_keep():
    got = RE.decide(_arm(3, 1653.0), _arm(4, 205.0))
    assert got["verdict"] == "keep"


def test_the_third_verdict_is_the_ledgers_existing_one():
    """新しい語彙を作らない。台帳は既に inconclusive を持っており、
    増やせば読む側が分岐を1つ増やす。"""
    from relay.selfimprove.ledger import VERDICTS
    for v in ("keep", "reject", "inconclusive"):
        assert v in VERDICTS


# ---- 測る形 -------------------------------------------------------------------------------------

def test_the_arm_reports_a_rise_not_an_absolute():
    """絶対値はそのとき機械が他に何をしていたかを一緒に運んでくる。"""
    samples = iter([1000.0, 1400.0, 1250.0])
    seen = []

    def run_goals(goals, socket_on, sample):
        sample()
        sample()
        seen.append((tuple(goals), socket_on))
        return {"done": len(goals), "fallbacks": 0}

    clock = iter([100.0, 137.0])
    got = RE.measure_arm(run_goals, goals=["a", "b"], socket_on=True,
                         peak_sampler=lambda: next(samples, 1250.0),
                         now=lambda: next(clock, 137.0))
    assert got["peak_mb"] == 400.0        # 1400 のピーク - 1000 の開始
    assert got["start_mb"] == 1000.0
    assert got["done"] == 2 and got["wall_s"] == 37.0
    assert got["socket"] is True
    assert seen == [(("a", "b"), True)]


def test_the_arm_records_fallbacks_because_a_route_that_gave_up_is_not_the_route():
    def run_goals(goals, socket_on, sample):
        return {"done": 4, "fallbacks": 3}

    got = RE.measure_arm(run_goals, goals=[1, 2, 3, 4], socket_on=True,
                         peak_sampler=lambda: 0.0)
    assert got["fallbacks"] == 3


def test_the_floor_is_the_operators_number_and_says_so():
    """最初の 2000 は reviewer page の定数からの流用で、この測定用に較正されていない。
    運用者の箱では毎回 abort になった。床は借り物ではなく、
    ここで意図して置き、理由を隣に書く。"""
    import inspect
    assert RE.MIN_FREE_MB == 512.0
    src = inspect.getsource(RE)
    i = src.index("MIN_FREE_MB = 512.0")
    block = src[max(0, i - 1600):i]
    assert "NOT THE SAME QUANTITY" in block.upper(), "アドミッション床との違いが書かれていない"
    assert "trims working sets" in block, "床では消えない二次バイアスが書かれていない"
