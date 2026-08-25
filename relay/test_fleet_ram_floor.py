"""RAM の下限は1つ。auto 系がそれより上の数字を自前で持っていたのが不具合だった。

3つのゲートが 2048 / 1400 / 2000 という別々のリテラルを抱えていて、どれも運用者が
定めた 512 を読んでいなかった。症状は無言で、空き 2454MB の端末で
`auto_concurrency` が 1 を返し、自動設定のフリートは全部を1件ずつ走らせていた。
"""
import ast
import inspect

import pytest

from relay import relay_fleet as RF


def test_the_floor_the_operator_set_is_the_floor_the_fleet_uses():
    """評価器と艦隊で別々の数字を持てば、また黙って離れる。
    ここが落ちたら、離れたほうではなく『どちらが正か』を決め直すこと。"""
    from relay.selfimprove import planner_evaluator as PE
    from relay.selfimprove import route_evaluator as RE
    assert RF.FLEET_RAM_FLOOR_MB == RE.MIN_FREE_MB == PE.MIN_FREE_MB


@pytest.mark.parametrize("floor", [512.0, 2048.0])
def test_every_gate_moves_with_the_one_setting(monkeypatch, floor):
    """3つとも同じ設定を読んでいること。1つでも自前の数字に戻れば、
    下限を上げたつもりの操作が一部にしか効かない。"""
    monkeypatch.setattr(RF, "FLEET_RAM_FLOOR_MB", floor)
    monkeypatch.setattr(RF, "FLEET_PER_TAB_MB", 700.0)
    monkeypatch.setattr(RF, "avail_phys_mb", lambda: floor + 1400.0)

    # 空きは 下限+1400。1タブ700なので、ちょうど2枚ぶん。
    assert RF.auto_concurrency(8) == 2
    assert RF.ram_target_cap(0, 1, 8) == 2        # 現在値1から1枚だけ増える
    # RAM が潤沢でも1回に1枚。下限を読んでいなければこの段差は出ない。
    monkeypatch.setattr(RF, "avail_phys_mb", lambda: floor + 700.0 * 20)
    assert RF.ram_target_cap(0, 1, 8) == 2
    monkeypatch.setattr(RF, "avail_phys_mb", lambda: floor + 1400.0)
    assert RF.ram_room_for_tab() is True

    monkeypatch.setattr(RF, "avail_phys_mb", lambda: floor + 100.0)
    assert RF.auto_concurrency(8) == 1            # 下限は割らない
    assert RF.ram_room_for_tab() is False         # 開けば下限を割る


def test_the_side_page_gate_is_derived_and_not_a_fourth_literal():
    """側ページの閾値は『残すべき下限 + これから開くタブ1枚』。
    そこに置かれていた 2000 は、そのどちらでもなかった。"""
    assert RF.ram_room_for_tab.__defaults__ == (None,)
    src = inspect.getsource(RF.ram_room_for_tab)
    assert "FLEET_RAM_FLOOR_MB + FLEET_PER_TAB_MB" in src


def test_no_gate_carries_its_own_ram_number_as_a_default():
    """既定値に数字が戻っていないこと。戻っていれば、設定を変えてもそこだけ効かない。"""
    tree = ast.parse(inspect.getsource(RF).lstrip())
    checked = 0
    for fn in ("auto_concurrency", "ram_target_cap", "ram_room_for_tab"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        names = [a.arg for a in node.args.args]
        for arg, default in zip(names[len(names) - len(node.args.defaults):],
                                node.args.defaults):
            if arg in ("per_tab_mb", "headroom_mb", "floor_mb"):
                checked += 1
                assert isinstance(default, ast.Constant) and default.value is None, \
                    "%s の %s に数字が戻っている" % (fn, arg)
    assert checked >= 5, checked


# ---- 待機中のワーカーを「これからなるもの」として量ること ------------------------------------------

def test_a_pending_worker_is_weighed_as_the_route_it_will_take():
    """`self.socket` は attach() の中で立つが、アドミッションは attach の前に量る。
    そのため socket を取る予定でタブを1枚も持たないワーカーが、タブとして課金されていた。
    予算2ではこれで両経路とも厳密に1本ずつになる: 入った1本は attach 後に重み1へ下がるが、
    次の待機ワーカーは2で課金され 1+2 が 2 を超える。
    2026-08-24 実測: 上限2の socket 走行で、4ゴールが 43/39/56 秒ずつずれて開始していた。
    """
    w = RF.RelayWorker("goal", "w0")
    assert w.socket is False
    # 絶対値ではなく関係で見る。この試験の主旨は「これからなる経路で量れること」で、
    # 1枠あたりの内訳(side-page を予約するかどうか)は別の判断であり、そちらが変わる
    # たびにこの試験が落ちるのは、測っている対象を取り違えている。
    tabs = w.tab_weight(assume_socket=False)
    sock = w.tab_weight(assume_socket=True)
    assert w.tab_weight() == tabs, "既定は現状(socket=False)のまま量ること"
    assert sock == tabs - 1, "socket は本体タブの分だけ軽いこと"


def test_admission_asks_for_the_route_it_will_take():
    """引数を足しただけで呼び出し側が渡していなければ、直っていない。"""
    import ast
    import inspect
    src = inspect.getsource(RF.run_relay_fleet)
    assert "tab_weight(assume_socket=_socket_open_now())" in src
    tree = ast.parse(src.lstrip())
    assert any(isinstance(n, ast.FunctionDef) and n.name == "_socket_open_now"
               for n in ast.walk(tree)), "判定関数が無い"


def test_the_per_tab_budget_is_the_fresh_tab_cost_and_says_so():
    """400 は運用者の実測で、しかも『開いた直後の』値であることが要点。
    長く開いたままのタブは 1,000MB を超え、1,340.9MB まで測られている。
    アドミッションが決めているのは『開くかどうか』なので、新規の値が正しい問い。
    伸びるほうは会話長を縛る側の問題で、この数字を膨らませて2枚目が入らなくする
    のは筋が違う。置き換えた 700 は継承した値で、この端末で測ったものではなかった。"""
    import inspect
    assert RF.FLEET_PER_TAB_MB == 400.0
    src = inspect.getsource(RF)
    head = src[:src.index("FLEET_PER_TAB_MB = float(")]
    # 固定幅で切り出すと、注記が伸びただけで落ちる -- 実際そうなった。
    # 見るべきは「この定数の説明の中に根拠が在るか」であって、末尾何文字かではない。
    note = head[head.rindex("#: What ONE Copilot tab is budgeted to cost"):]
    assert "1,340.9" in note or "1340.9" in note, "根拠が隣に書かれていない"


def test_the_per_tab_budget_is_recorded_as_measured_not_inherited():
    """400 は長らく『運用者から。この箱では未測定』だった。測ったので、根拠を隣に置く。

    3回の冷えた走行(新品の headless、warm-up 無し、goal 4本、同時3)で、tabs アーム全体が
    新品ブラウザ比 564.1MB、sd 27.2。--process-per-site が同一サイトのページを1レンダラに
    まとめるので、タブは各自プロセスを買わない -- 同時1タブあたり約190MB。400 はその約2倍を
    請求しており、開くか否かを決める門としては安全側。だから数字は動かさない。

    このテストは 400 を守るためではなく、根拠がコードの隣から消えないためにある。"""
    from relay import relay_fleet as RF

    assert hasattr(RF, "FLEET_MEASURED_TABS_ARM_MB")
    assert hasattr(RF, "FLEET_MEASURED_SOCKET_ARM_MB")
    # 実測が予算を上回るなら、予算はもう安全側ではない
    per_concurrent = RF.FLEET_MEASURED_TABS_ARM_MB / 3.0
    assert per_concurrent < RF.FLEET_PER_TAB_MB, (
        "実測のタブ単価 %.1fMB が予算 %.1fMB を超えている -- 予算はもう保守的ではない"
        % (per_concurrent, RF.FLEET_PER_TAB_MB))


def test_a_socket_worker_weighs_no_tab_and_that_is_documented_as_a_tab_claim():
    """socket ワーカーはタブを開かないので tab_weight 0 は正しい。
    しかし『メモリも 0』ではない -- 実測でブラウザ側 428.8MB。
    その区別がコードから消えると、socket 中心のフリートで予算が意味を失う。"""
    import inspect

    from relay import relay_fleet as RF

    src = inspect.getsource(RF.RelayWorker.tab_weight)
    assert "428.8" in src or "FLEET_MEASURED_SOCKET_ARM_MB" in src, (
        "socket が無料ではないことが、判定の隣に書かれていない")
    assert RF.FLEET_MEASURED_SOCKET_ARM_MB < RF.FLEET_MEASURED_TABS_ARM_MB


def test_side_pages_are_not_reserved_at_admission(monkeypatch):
    """使うとも限らないものを全ワーカーに前払いさせ、フリートを直列化していた。

    運用者の実走行で観測(2026-08-25): 5ゴール中2本だけ入り、3本が16分 pending。
    空きRAM 3106MB で ram_target_cap の予算は 2〜3、一方 既定の auto effort では
    1ワーカーが socket で2枠・タブで3枠を請求していた。予算2に単価2なら1人しか入らない。

    side-page は必要になって初めて開かれ、開く時点で ram_room_for_tab を通り、
    research は socket で走ればページすら要らない。使う場所で守られている資源を
    受け入れ時にもう一度請求するのは二重計上。"""
    from relay import relay_fleet as RF

    monkeypatch.delenv("SWE_SIDEPAGE_RESERVE", raising=False)

    class W:
        socket = None
        max_research = 3
        refuter = True

    assert RF.RelayWorker.tab_weight(W, assume_socket=False) == 1, "タブ以外も予約している"
    assert RF.RelayWorker.tab_weight(W, assume_socket=True) == 0, "socket が枠を取っている"


def test_the_old_reservation_is_still_reachable(monkeypatch):
    """遅延オープンの門が足りない箱のための逃げ道は残す。"""
    from relay import relay_fleet as RF

    monkeypatch.setenv("SWE_SIDEPAGE_RESERVE", "1")

    class W:
        socket = None
        max_research = 3
        refuter = True

    assert RF.RelayWorker.tab_weight(W, assume_socket=False) == 3


def test_a_worker_without_side_work_never_paid_for_it(monkeypatch):
    """refuter も research も無いワーカーは、どちらの設定でも1枠。
    ここが変わっていたら、直したつもりで別のものを壊している。"""
    from relay import relay_fleet as RF

    class W:
        socket = None
        max_research = 0
        refuter = False

    for value in ("0", "1"):
        monkeypatch.setenv("SWE_SIDEPAGE_RESERVE", value)
        assert RF.RelayWorker.tab_weight(W, assume_socket=False) == 1
