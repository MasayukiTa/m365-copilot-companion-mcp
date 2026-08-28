"""使われていないフリート用ブラウザを手放してよいか、の判定。

## なぜ手放したいか(実測 2026-08-28、走行なしの時点)

| | プロセス | RSS | 保持ページ |
|---|---|---|---|
| fleet Edge (:9222) | 8 | 273 MB | about:blank 1枚 |
| bridge Edge (:9223) | 8 | 249 MB | about:blank 1枚 |

空ページ自体は数MB。**費用はそのページが生かしている browser 本体**。

## なぜ fleet だけか

フリートは「死んだブラウザからの起動」が既に正規経路(`cdp_alive` を見て hard_reset)。
前回の Edge が残っていることに何も依存していない。

bridge にはその再入場点が無く、CDP watchdog は「Edge が死んだ」を自殺の理由と読む。
さらにコールドスタートがログイン壁に落ちると、対話認証は foreground 必須なので、
249MB の節約が「気づかれるまで対話経路が止まる」に化ける。**割に合わない**。

## 不明は「使用中」に倒す

`edge_recover.other_fleet_runs` は列挙に失敗すると「兄弟なし」と答える。復旧用途では正しい
—— psutil が無いという理由で詰まったブラウザを直せない方が害が大きいから。
ここは逆。任意で・後回しにできて・間違えると隣の走行を壊す判断なので、
**プロセス一覧が読めない = 誰か作業中かもしれない**に倒す。

2026-08-25、4本の走行が同じプロファイルを共有し、1本が reset して隣の走行が
ターン途中で文脈を失った実害がある。
"""
import time

import pytest

from relay.idle_edge import FLEET_CDP_PORT, may_release


def _status(**kw):
    base = {"running": False, "workers": [], "updated": time.time() - 10_000}
    base.update(kw)
    return base


def _no_siblings():
    return [], True


def _unknown_siblings():
    return [], False


def _one_sibling():
    return [4242], True


# ---- 手放してよい場合 -------------------------------------------------------------------------

def test_an_idle_browser_with_no_siblings_may_go():
    ok, why = may_release(_status(), sibling_fn=_no_siblings, pages=1)
    assert ok, why


# ---- 手放してはいけない場合 -------------------------------------------------------------------

def test_a_live_run_keeps_it():
    ok, why = may_release(_status(running=True), sibling_fn=_no_siblings, pages=1)
    assert not ok and "in flight" in why


def test_an_unfinished_worker_keeps_it():
    """`running` が false でも、終わっていないワーカーが残っていれば使用中。

    最後のワーカーが terminal になったスイープで running は false になる —— 履歴の欠落で
    今日直したのと同じ境界。ここで status だけ信じると、片付け中のブラウザを抜く。
    """
    s = _status(workers=[{"status": "done"}, {"status": "waiting"}])
    ok, why = may_release(s, sibling_fn=_no_siblings, pages=1)
    assert not ok and "not finished" in why


def test_an_unreadable_process_list_keeps_it():
    """**不明は使用中**。ここが本題。"""
    ok, why = may_release(_status(), sibling_fn=_unknown_siblings, pages=1)
    assert not ok, "プロセス一覧が読めないのに手放そうとしている"
    assert "process list" in why


def test_a_sibling_run_keeps_it():
    ok, why = may_release(_status(), sibling_fn=_one_sibling, pages=1)
    assert not ok and "other fleet run" in why


def test_an_extra_page_keeps_it():
    """keep-alive 以外にページが開いていれば、status が追いついていないだけ。"""
    ok, why = may_release(_status(), sibling_fn=_no_siblings, pages=3)
    assert not ok and "page(s) open" in why


def test_a_recent_finish_keeps_it():
    """終わった直後は待つ。ゴールはまとめて投げられるし、resume はすぐ繋ぎ直す。"""
    s = _status(updated=time.time() - 5)
    ok, why = may_release(s, sibling_fn=_no_siblings, pages=1)
    assert not ok and "grace" in why


def test_a_missing_status_file_keeps_it():
    """status が無いのは「暇」の証拠ではなく「誰も書いていない」の証拠。

    走行が最初の書き込みをする直前も、同じ見た目になる。
    """
    ok, why = may_release(None, sibling_fn=_no_siblings, pages=1)
    assert not ok and "no run status" in why


def test_an_unknown_finish_time_keeps_it():
    s = {"running": False, "workers": []}
    ok, why = may_release(s, sibling_fn=_no_siblings, pages=1)
    assert not ok and "finish time" in why


# ---- 触ってよいのは1つのポートだけ ------------------------------------------------------------

@pytest.mark.parametrize("port", [9223, 9224, 9222 + 1, 0])
def test_no_other_port_is_ever_released(port):
    """bridge(:9223) と eval(:9224) には絶対に触らない。

    これは性能の話ではない。bridge を落とすと、無人時間帯にログイン壁へ落ちたとき
    対話経路が朝まで止まる可能性がある。
    """
    ok, why = may_release(_status(), sibling_fn=_no_siblings, pages=1, port=port)
    assert not ok, "フリート以外のポートを解放しようとしている: %s" % port
    assert str(FLEET_CDP_PORT) in why


def test_the_fleet_port_is_the_one_the_fleet_uses():
    """定数が実際のフリート用ポートと一致していること。ずれれば何も解放されない(安全側)、
    あるいは別のブラウザを解放する(危険側)。"""
    assert FLEET_CDP_PORT == 9222


# ---- 実行部は、頼まれない限り動かない -----------------------------------------------------

def _watch_stack_source():
    import io
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with io.open(os.path.join(root, "scripts", "win", "watch_stack.py"), encoding="utf-8") as fh:
        return fh.read()


def test_the_reaper_is_off_unless_asked():
    """既定で発火しないこと。

    これはブラウザを終了させる。判定は status とプロセス一覧を正しく読めることに賭けていて、
    idle_edge はあらゆる疑わしさで拒否する —— それでも、置きっぱなしの guard が黙って
    ブラウザを閉じ始めるのは、誰も頼んでいない驚きである。人が有効にする。
    """
    src = _watch_stack_source()
    assert "release_idle_edge: bool = False" in src, "既定が有効になっている"
    assert "--release-idle-edge" in src, "明示フラグが無い"
    assert "if release_idle_edge:" in src, "フラグを見ずに呼んでいる"


def _executable(src):
    """docstring とコメントを落としたソース。

    生ソースに断言すると、**使わない理由を説明している文**に一致して落ちる。実際に落ちた:
    「hard_reset は使わない」と書いた docstring が "hard_reset" に一致した。
    同じ形は今日7回目なので、ここでも実行されるコードだけを見る。
    """
    import ast

    tree = ast.parse(src)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body.pop(0)
    return ast.unparse(tree)


def test_the_reaper_does_not_relaunch():
    """hard_reset を使わないこと。あれは起動し直すので、目的と正反対になる。"""
    src = _executable(_watch_stack_source())
    body = src[src.index("def _kill_edge_on("):]
    body = body[:body.index("\ndef ", 10)]
    assert "hard_reset" not in body, "停止のつもりで再起動している"
    assert "terminate()" in body


def test_the_reaper_identifies_the_browser_by_its_port():
    """名前一致ではなくポートで特定すること。別用途の Edge を巻き込まないため。"""
    src = _executable(_watch_stack_source())
    body = src[src.index("def _kill_edge_on("):]
    body = body[:body.index("\ndef ", 10)]
    assert "--remote-debugging-port=%d" in body, "ポートで特定していない"
    # ast.unparse は引用符を正規化するので、引用符に依存しない形で見る。
    assert "--type=" in body, "子プロセスを root と取り違える(--type= を除外していない)"
