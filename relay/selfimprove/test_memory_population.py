"""メモリ計測の母集団。ここを間違えると、走行を何本足しても意味が出ない。

2026-08-24 実測: この端末に msedge.exe が45プロセス・合計6181MB あり、
フリートの Edge(:9222) は 1559MB、bridge の Edge(:9223) は 1002MB。
残る 59% はどちらでもなく、運用者自身の閲覧などで動く。
`_edge_mb` はその全部を合計していたので、タブを1枚も開かない socket 腕が
1070MB を示し、同一の腕どうしが 707MB 離れた。経路の性質ではない。
"""
import ast
import inspect

from relay.selfimprove import scheduler as S


def test_the_owner_of_a_port_nobody_listens_on_is_none_not_a_guess():
    assert S._cdp_owner_pid("http://127.0.0.1:1") is None
    assert S._cdp_owner_pid("not a url") is None


def test_the_resolver_returns_rather_than_raises():
    """例外で落ちると走行ごと止まる。解決できないことは結果として返し、
    どちらの母集団で測ったかは呼び出し側が記録する。"""
    # 本文の走査ではなく構文で見る。docstring の "raised" に当たって
    # 「raise が無いこと」の検査が消える -- 今日4回目。
    fn = ast.parse(inspect.getsource(S._cdp_owner_pid).lstrip()).body[0]
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    nones = [n for n in ast.walk(fn)
             if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
             and n.value.value is None]
    assert len(nones) >= 3, len(nones)


def test_the_sampler_walks_the_cdp_owner_tree_and_says_which_population_it_used():
    """絞れなかったときは黙って旧来の全 Edge 合計に戻る。それを記録しなければ、
    読む側はどちらの数字を見ているのか判別できない。"""
    src = inspect.getsource(S.route_evaluator_for)
    fn = next(n for n in ast.walk(ast.parse(src.lstrip()))
              if isinstance(n, ast.FunctionDef) and n.name == "_edge_mb")
    body = ast.unparse(fn) if hasattr(ast, "unparse") else src
    assert "_cdp_owner_pid" in body
    assert "children(recursive=True)" in body
    assert "fleet-edge-tree" in body and "all-edge-unscoped" in body
    # 記録に出ること
    assert 'out["memory_population"]' in src


def test_the_tree_is_rewalked_every_sample_not_cached():
    """Edge はレンダラを常に作っては捨てる。根だけ覚え、木は毎回歩き直す。"""
    src = inspect.getsource(S.route_evaluator_for)
    fn = next(n for n in ast.walk(ast.parse(src.lstrip()))
              if isinstance(n, ast.FunctionDef) and n.name == "_edge_mb")
    body = ast.unparse(fn) if hasattr(ast, "unparse") else src
    # 根はキャッシュ、木は毎サンプル
    assert "pid_exists" in body
    assert body.count("children(recursive=True)") == 1
