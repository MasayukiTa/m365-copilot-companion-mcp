"""選ぶことと記録することは別の問い。同じスイッチだったので、片方しか選べなかった。

メモリは adaptive 選択が ON のときだけ書かれていた。つまり**自分が選んだレンズについてしか
学習できない**。探索スロットが他を生かしてはいる（実測: 30候補あたり、選ばれたレンズ30回に
対し選ばれなかったレンズ各15回）が、**フルパネルから温める方法が存在しなかった**。

これは §18 に直接効く。全レンズ走行コーパス上で方策を比較するのに、メモリが空なら
adaptive はパネルの並び順を返す — それは fixed そのもので、frontier に同じ方策が
2点並ぶ。
"""
import os

from relay import relay_fleet as RF
from relay.refuter import PANEL_LENSES
from relay.refuter_memory import RefuterMemory


def _source():
    import inspect
    return inspect.getsource(RF.RelayWorker)


# ---- 2つのフラグが別の仕事をすること -------------------------------------------------------------

def test_recording_and_selecting_are_separate_switches():
    src = _source()
    assert 'os.environ.get("MCP_REFUTER_MEMORY_RECORD")' in src
    assert 'adaptive = os.environ.get("MCP_ADAPTIVE_REFUTER") == "1"' in src


def test_selection_still_only_happens_under_the_adaptive_flag():
    """記録フラグが走行内容を変えてはいけない。変えたら、温めるための走行が
    温めたい対象と別のものを測ることになる。"""
    src = _source()
    i = src.index('if adaptive:')
    j = src.index("self._panel_queue = lenses", i)
    assert "select_lenses" in src[i:j], "選択が adaptive 分岐の外に出ている"
    before = src[:i]
    assert "select_lenses" not in before.split("_adaptive_features = None")[-1], (
        "記録だけのつもりの経路が選択もしている")


def test_recording_alone_leaves_the_panel_intact():
    """フルパネルのまま記録できること -- これが無いと §18 のコーパスが取れない。"""
    src = _source()
    # 記録フラグの分岐は memory と features を作るだけで、lenses に触れない
    i = src.index('if adaptive or os.environ.get("MCP_REFUTER_MEMORY_RECORD")')
    j = src.index("if adaptive:", i)
    assert "lenses" not in src[i:j], "記録経路がパネルを書き換えている"


# ---- なぜ空メモリが §18 を壊すか -----------------------------------------------------------------

def test_an_empty_memory_makes_adaptive_indistinguishable_from_fixed(tmp_path):
    """実測に基づく。空の store で `select_lenses` はパネルの並び順を返す。"""
    mem = RefuterMemory(path=str(tmp_path / "m.json"))
    chosen = mem.select_lenses({"kind": "code"}, list(PANEL_LENSES), 2)
    assert chosen == list(PANEL_LENSES)[:2]


def test_a_full_panel_run_can_warm_every_lens(tmp_path):
    """記録経路の目的そのもの: 3レンズ全部に観測が入ること。
    adaptive 選択下では、選ばれなかったレンズは探索スロットでしか増えない。"""
    mem = RefuterMemory(path=str(tmp_path / "m.json"))
    feats = {"kind": "code"}
    for _ in range(10):
        for lens in PANEL_LENSES:          # full panel, as MCP_REFUTER_MEMORY_RECORD gives
            mem.record(feats, lens, refuted=(lens == "correctness"))
    seen = {lens: 0 for lens in PANEL_LENSES}
    for key, cell in mem.data["cells"].items():
        for lens in PANEL_LENSES:
            # セル鍵は "<bucket>::<lens>"。区切りを憶測で書くと、
            # 「全部ゼロ」というテストが通らないのではなく、通ってしまう形もある。
            if key.endswith("::" + lens):
                seen[lens] += int(cell.get("total", 0))
    assert all(n == 10 for n in seen.values()), seen


def test_the_adaptive_path_observes_its_own_choices_more_than_the_others(tmp_path):
    """偏りは実在するが限定的、という測定をそのまま固定する。
    『探索があるから大丈夫』でも『完全に偏る』でもない。"""
    mem = RefuterMemory(path=str(tmp_path / "m.json"))
    feats = {"kind": "code"}
    seen = {lens: 0 for lens in PANEL_LENSES}
    for _ in range(30):
        for lens in mem.select_lenses(feats, list(PANEL_LENSES), 2):
            seen[lens] += 1
            mem.record(feats, lens, refuted=(lens == "correctness"))
    top = max(seen.values())
    bottom = min(seen.values())
    assert top == 30, seen
    assert 0 < bottom < top, "探索スロットが効いていないか、偏りが消えている: %s" % seen
