"""Tests for the absolute witness. It must read, and only read."""
import pathlib

from scripts.win import watch_tree_ws as W

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_the_tree_walk_collects_descendants_and_stops_at_a_cycle():
    """ブラウザの費用は子プロセスに散っている。木を降りられなければ何も測れない。
    親子関係が輪になっていても止まること -- 実プロセス表は pid 再利用で輪を作りうる。"""
    procs = {
        1: (0, "root", 10.0),
        2: (1, "child", 20.0),
        3: (2, "grandchild", 30.0),
        4: (99, "unrelated", 40.0),
        5: (6, "cycle-a", 1.0),
        6: (5, "cycle-b", 1.0),
    }
    assert W._tree_pids(1, procs) == {1, 2, 3}
    assert 4 not in W._tree_pids(1, procs)
    assert W._tree_pids(5, procs) == {5, 6}          # 輪でも戻ってくること


def test_the_witness_never_starts_stops_or_drives_anything():
    """凍結判定器を疑うために置く道具が、疑う対象を動かしてはいけない。

    可視性を『確認』するはずの命令が起動器を呼ぶ形で書かれ、走れば監視対象の測定を
    殺すところだった -- 同じ形を二度やらない。この目撃者はプロセス表を読んで
    追記するだけで、起動も停止も入力送出も持たない。"""
    src = (ROOT / "scripts" / "win" / "watch_tree_ws.py").read_text(encoding="utf-8")
    for banned in ("subprocess", "Popen", "terminate()", "kill()", "os.system",
                   "start_eval_edge", "start_companion_edge", "Page.navigate", "websocket"):
        assert banned not in src, "目撃者が %s を持っている" % banned


def test_it_records_the_gap_when_the_browser_is_being_rebuilt():
    """走行間の再構築中は CDP の持ち主が居ない。そこを黙って飛ばすと、
    再構築が起きた事実が痕跡から消える -- 基準線が取られるのは正にそこ。"""
    src = (ROOT / "scripts" / "win" / "watch_tree_ws.py").read_text(encoding="utf-8")
    assert "if root is None:" in src
    i = src.index("if root is None:")
    assert "writerow" in src[i:i + 400], "持ち主が居ない標本を書き残していない"


def test_it_writes_absolute_totals_not_deltas():
    """差分は、それを取った基準線の下でしか意味を持たない。そして疑われているのが
    その基準線。後から別の統計量を当て直せるのは絶対値だけ。"""
    # 文字列走査ではなく振る舞いで見る。同じ木を二度撮って、合計が
    # 「その時点の実 working set」であって「前回との差」でないことを確かめる。
    procs = {1: (0, "root", 100.0), 2: (1, "child", 50.0)}
    seen = W._tree_pids(1, procs)
    total = round(sum(procs[p][2] for p in seen), 1)
    assert total == 150.0, "合計が絶対値になっていない"

    class _P:                                        # psutil の代わり
        def __init__(self, pid, ppid, mb):
            self.info = {"pid": pid, "ppid": ppid, "name": "msedge.exe",
                         "memory_info": type("M", (), {"rss": int(mb * 1024 * 1024)})()}

    import types
    fake = types.SimpleNamespace(
        process_iter=lambda attrs=None: [_P(1, 0, 100.0), _P(2, 1, 50.0)])
    import sys as _s
    old_mod = _s.modules.get("psutil")
    _s.modules["psutil"] = fake
    try:
        first, _ = W.snapshot(1)
        second, _ = W.snapshot(1)
    finally:
        if old_mod is None:
            _s.modules.pop("psutil", None)
        else:
            _s.modules["psutil"] = old_mod

    assert first == 150.0
    assert second == first, "2回目が差分になっている -- 絶対値なら同じ値が出る"
