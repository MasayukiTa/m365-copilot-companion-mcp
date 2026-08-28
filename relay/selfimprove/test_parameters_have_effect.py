"""進化ループが調整できる各パラメータが、実際に挙動を変えるか。

manifest.py は既に「Every parameter here MUST have a production reader」と書いており、
過去に読み手のない3つが削除されている。それでも `max_retries` は今日まで無効だった --
**読み手はあった**（`_genome_default("max_transient")` → `RelayWorker(max_transient=...)`）が、
その読み手は値を表示文字列にしか使っていなかった。`retry 3/2` と印字しながら、2 は
何も制限していない。

つまり「reader がある」は満たされたまま、座標は不活性でありうる。だからここで測るのは
存在ではなく**効果**: 値を変えたら観測可能な何かが変わること。不活性な座標は欠けている
座標より悪い -- ループはそれを調整し、ノイズを測り、KEEP しうる。
"""
import pytest

from relay.selfimprove import manifest as M


def test_every_evolvable_parameter_is_named_in_an_effect_test():
    """パラメータを増やして効果テストを書き忘れると、また不活性な座標が生える。"""
    covered = {"max_retries", "max_refute_passes", "memory_max_items",
               "max_research", "review_lens_count"}
    assert set(M.DEFAULT_PARAMETERS) == covered, (
        "manifest のパラメータ集合が変わった。増えたものには、値を変えると観測可能な "
        "何かが変わることを示すテストを、下に書くこと")


# ---- max_retries: 今日直したもの ---------------------------------------------------------------

def _stuck_worker(budget):
    from relay.relay_fleet import RelayWorker
    w = RelayWorker("g", "w", max_transient=budget)
    w.last_response = ""
    return w


def test_max_retries_bounds_how_often_an_agent_stuck_is_re_prompted():
    """予算1なら1回で終端、予算5ならまだ走っている -- 同じ入力で。"""
    tight = _stuck_worker(1)
    for text in ("STUCK: a", "STUCK: b"):
        tight._decide(text)
    assert tight.status == "stuck" and tight.outcome == "STUCK"

    loose = _stuck_worker(5)
    for text in ("STUCK: a", "STUCK: b"):
        loose._decide(text)
    assert loose.status == "ready" and loose.outcome is None


def test_the_counter_never_exceeds_the_budget_it_prints_against():
    """`retry 3/2` は読み手に対する嘘であり、上限が無いことの症状でもあった。"""
    w = _stuck_worker(2)
    for text in ("STUCK: a", "STUCK: b", "STUCK: c", "STUCK: d"):
        w._decide(text)
    assert w.transient <= 2, "予算を超えて再試行している (%d)" % w.transient


def test_transport_failures_are_deliberately_not_bounded_by_this_count():
    """短い回数予算は瞬断で尽き、全ワーカーを終わらせた。送信・タイムアウトは
    時間窓のまま -- 直したことで巻き添えに変えていないことを固定する。"""
    import time

    from relay.relay_fleet import NET_RETRY_WINDOW_S, RelayWorker
    w = RelayWorker("g", "w", max_transient=1)
    w.first_transient_ts = time.time()          # 窓の中
    for _ in range(4):
        assert w._retry_transient() is True
    assert w.transient == 4, "回数上限が transport 経路にまで及んでいる"

    w.first_transient_ts = time.time() - (NET_RETRY_WINDOW_S + 60)
    assert w._retry_transient() is False, "窓を超えても諦めていない"


# ---- 他の2つ -----------------------------------------------------------------------------------

def test_memory_max_items_changes_what_is_primed_into_a_goal():
    import os
    import tempfile

    from relay import project_memory as PM
    state = tempfile.mkdtemp(prefix="pmeff_")
    for i in range(8):
        PM.record_task("t", "作業%d" % i, "DONE", note="メモ%d" % i,
                       state_dir=state, ts=100 + i)
    few = PM.load_notes("t", state_dir=state, max_items=2)
    many = PM.load_notes("t", state_dir=state, max_items=8)
    assert few != many and len(few) < len(many), (
        "memory_max_items を変えても goal に差し込まれる本文が変わらない")
    assert os.path.isdir(state)


def test_max_refute_passes_is_compared_not_merely_printed():
    """反証ループを実際に回すのは重いので、ここでは**今日の欠陥の形**を検査する。

    `max_transient` は「読み手がある」を満たしながら不活性だった。読み手が値を
    `"retry %d/%d"` にしか使っていなかったから。つまり値が**比較か算術に現れるか**が、
    存在確認より一段強い問い -- そしてそれが今日の欠陥をそのまま落とす検査。
    """
    assert _appears_in_a_comparison("relay/copilot_autopilot_relay.py", "max_refute"), (
        "max_refute がどの比較にも現れない -- 表示だけの座標になっている疑い")


def test_the_parameter_i_fixed_today_would_now_be_caught_by_that_check():
    """検査が本物であることの確認。修正前の relay_fleet では max_transient は
    フォーマット文字列の中にしかなく、この検査は False を返したはず。"""
    assert _appears_in_a_comparison("relay/relay_fleet.py", "max_transient")
    assert not _appears_in_a_comparison("relay/relay_fleet.py", "max_transient",
                                        source='self.reason = "retry %d/%d" % (a, self.max_transient)')


def _appears_in_a_comparison(path, name, source=None):
    """Whether `name` is read into a comparison or arithmetic, rather than only formatted."""
    import ast
    import io as _io
    tree = ast.parse(source if source is not None
                     else _io.open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Compare, ast.BinOp)):
            continue
        # `x % (…)` on a string literal is formatting, not arithmetic.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            continue
        for sub in ast.walk(node):
            got = sub.attr if isinstance(sub, ast.Attribute) else getattr(sub, "id", "")
            if got == name:
                return True
    return False


# ---- review_lens_count: `ultra` を harness に表現させる座標 -------------------------------------

def _with_parameters(monkeypatch, **params):
    """指定パラメータだけ差し替えた manifest を active にする。"""
    import json
    import os
    import tempfile

    from relay.selfimprove import runtime_config as RC

    man = M.base_manifest()
    man["parameters"].update(params)
    d = tempfile.mkdtemp(prefix="peff_")
    path = os.path.join(d, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(man, fh)
    monkeypatch.setenv(RC.OVERRIDE_ENV, path)
    RC.active_manifest(refresh=True)
    return path


def test_review_lens_count_decides_whether_a_run_gets_a_panel_at_all(monkeypatch):
    """0 でパネル無し、3 で3枚。呼び手は同じ（何も渡していない）。

    これが効かなければ、ベンチの子は refuter しか渡さない設計なので `ultra` を要求しても
    `auto` が走り、両者は同じ harness_id で記録される -- 比較そのものが成立しない。
    """
    from relay.relay_fleet import _resolve_review_lenses

    _with_parameters(monkeypatch, review_lens_count=0)
    assert _resolve_review_lenses(None) is None

    _with_parameters(monkeypatch, review_lens_count=3)
    assert _resolve_review_lenses(None) == ["correctness", "edge", "security"]


def test_review_lens_count_is_a_ladder_each_step_adding_one_reviewer(monkeypatch):
    """段ごとに1枚増えること。増えないなら「2枚と3枚」は別のパネルの比較になる。"""
    from relay.relay_fleet import _resolve_review_lenses

    seen = []
    for n in (1, 2, 3):
        _with_parameters(monkeypatch, review_lens_count=n)
        seen.append(_resolve_review_lenses(None))
    assert [len(x) for x in seen] == [1, 2, 3]
    for lo, hi in zip(seen, seen[1:]):
        assert lo == hi[:len(lo)]


def test_an_explicit_lens_list_still_wins_over_the_harness(monkeypatch):
    """明示引数を渡した呼び手はそれを意図している -- 既存契約を壊さないこと。"""
    from relay.relay_fleet import _resolve_review_lenses

    _with_parameters(monkeypatch, review_lens_count=3)
    assert _resolve_review_lenses(["edge"]) == ["edge"]


# ---- max_research: 側ページ予算 ----------------------------------------------------------------

def test_max_research_reaches_the_fleet_from_the_active_genome(monkeypatch):
    """署名の直書き 3 では、genome を変えても調査予算は動かなかった。"""
    import relay.relay_fleet as F

    _with_parameters(monkeypatch, max_research=0)
    assert F._genome_default("max_research", -1) == 0

    _with_parameters(monkeypatch, max_research=9)
    assert F._genome_default("max_research", -1) == 9


def test_the_research_budget_is_no_longer_a_literal_in_the_signature():
    """直書きされた既定値は、読み手があっても不活性な座標を作る。"""
    import inspect

    import relay.relay_fleet as F
    assert inspect.signature(F.run_relay_fleet).parameters["max_research"].default is None
