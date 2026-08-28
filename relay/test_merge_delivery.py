"""分割した親には、**成功した統合**の答えを載せること。

## 何が起きていたか

キャンペーンには統合ワーカーが複数いることがある。統合が STUCK すると再試行が queue され、
家族は「失敗した試行」と「成功した試行」を両方持って終わる。そして
**失敗した方が先に走るので、ワーカー番号が小さい**。

親への書き戻しは `next(...)` で **workers 順の最初の1体**を取っていた。
記録に残る多重統合キャンペーン2件は、どちらもこうなっている:

| キャンペーン | 選ばれていた | 実際に成功していた |
|---|---|---|
| cee48d3a708dc | w13 STUCK(9ターン) | w17 DONE(4ターン) |
| c9d042fc4334a | w23 STUCK(14ターン) | w28 DONE(5ターン) |

しかも STUCK した統合は本文を持たないので、続く `if _merged:` で**書き戻し自体が skip** される。
成功した統合は同じリストの中にいて、一度も見られないまま、親は何も持たずに配送された。

## 直し方

DONE を優先し、無ければ最後の試行(最新の入力で走ったもの)。
本文の有無が最終判断であることは変えていないので、全試行が失敗した家族の挙動は従来どおり。
"""
import pytest


class _Env(object):
    def __init__(self, role="", campaign_id=""):
        self.role = role
        self.campaign_id = campaign_id


class _W(object):
    def __init__(self, name, outcome="", text="", role="", cid=""):
        self.name = name
        self.outcome = outcome
        self.status = "done"
        self.display_result = text
        self.last_response = text
        self.reason = "r"
        self.goal = "親ゴール"
        self.task_envelope = _Env(role, cid)


def _pick(workers, cid):
    """本番と同じ選び方。ここだけを取り出して確かめる。"""
    aggs = [x for x in workers
            if getattr(getattr(x, "task_envelope", None), "role", "") == "aggregator"
            and getattr(getattr(x, "task_envelope", None), "campaign_id", "") == cid]
    if not aggs:
        return None, ""
    agg = next((x for x in aggs if (x.outcome or "") == "DONE"), aggs[-1])
    merged = agg.display_result or agg.last_response
    if not merged and len(aggs) > 1:
        for alt in reversed(aggs):
            text = alt.display_result or alt.last_response
            if text:
                return alt, text
    return agg, merged


CID = "cdeadbeef1234"


def test_the_successful_merge_wins_over_an_earlier_failed_one():
    """本題。失敗が先に走っていても、成功した方を採る。"""
    workers = [
        _W("w13", "STUCK", "", "aggregator", CID),
        _W("w17", "DONE", "統合された答え", "aggregator", CID),
    ]
    agg, merged = _pick(workers, CID)
    assert agg.name == "w17", "先に走って失敗した統合を選んでいる: %s" % agg.name
    assert merged == "統合された答え"


def test_a_failed_first_merge_no_longer_silently_skips_the_backfill():
    """STUCK は本文を持たないので、それを選ぶと書き戻しごと消える。

    実走行で2回踏んだのはこの経路。選び直すだけでなく、**書き戻しが起きること**を見る。
    """
    workers = [
        _W("w23", "STUCK", "", "aggregator", CID),
        _W("w28", "DONE", "八つの報告の統合", "aggregator", CID),
        _W("w29", "STUCK", "", "aggregator", CID),
    ]
    _, merged = _pick(workers, CID)
    assert merged, "成功した統合があるのに親に載せる本文が無い"


def test_all_attempts_failed_still_delivers_nothing():
    """全部失敗した家族の挙動は変えない。無い答えを作らない。"""
    workers = [
        _W("w13", "STUCK", "", "aggregator", CID),
        _W("w18", "STUCK", "", "aggregator", CID),
    ]
    _, merged = _pick(workers, CID)
    assert merged == "", "失敗しかしていない家族に本文が生えている"


def test_a_lone_failed_merge_is_unchanged():
    workers = [_W("w13", "STUCK", "", "aggregator", CID)]
    agg, merged = _pick(workers, CID)
    assert agg.name == "w13" and merged == ""


def test_another_campaign_is_never_borrowed_from():
    """キャンペーンが違えば、成功していても使わない。"""
    workers = [
        _W("w13", "STUCK", "", "aggregator", CID),
        _W("w40", "DONE", "別キャンペーンの答え", "aggregator", "cotherfamily99"),
    ]
    _, merged = _pick(workers, CID)
    assert merged == "", "別の家族の統合結果を持ってきている"


def test_a_non_aggregator_is_never_chosen():
    workers = [
        _W("w2", "DONE", "子タスクの報告", "producer", CID),
        _W("w13", "STUCK", "", "aggregator", CID),
    ]
    agg, _ = _pick(workers, CID)
    assert agg.name == "w13", "統合でないワーカーを統合として採用している"


def test_no_aggregator_at_all_yields_nothing():
    workers = [_W("w2", "DONE", "子タスクの報告", "producer", CID)]
    agg, merged = _pick(workers, CID)
    assert agg is None and merged == ""


# ---- 本番のコードが同じ選び方をしていること ---------------------------------------------------

def test_the_product_prefers_done_and_falls_back_to_the_last_attempt():
    import io
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with io.open(os.path.join(root, "relay", "relay_fleet.py"), encoding="utf-8") as fh:
        src = fh.read()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert re.search(r'_aggs\s*=\s*\[x for x in workers', code), (
        "統合ワーカーを1体だけ取る書き方に戻っている")
    assert re.search(r'next\(\(x for x in _aggs if \(x\.outcome or ""\) == "DONE"\), _aggs\[-1\]\)',
                     code), "DONE を優先していない、または最後の試行に落ちていない"


# ---- 統合結果の受け入れ検査 -------------------------------------------------------------------

def test_gaps_become_acceptance_checks():
    """未完了スライスがあれば、その番号に触れることを検査条件にする。

    統合プロンプトは以前から「未取得を明示せよ」と**頼んで**いた。頼んだだけで、
    守られたかを誰も見ていなかった。記録に残るギャップ付き統合2件のうち、
    transcript が残る方は DONE で終わった2本とも「欠落なし」と書いていた。

    これは分割側の `subtasks_from`(解釈できない提案を拒否する)に対応する検査で、
    分割側には最初からあり、統合側には無かった。
    """
    from relay.fanout import merge_acceptance_checks, missing_slices

    recs = [{"subtask_index": 1, "outcome": "DONE"},
            {"subtask_index": 2, "outcome": "STUCK"},
            {"subtask_index": 3, "outcome": "DONE"},
            {"subtask_index": 4, "outcome": "STUCK"}]
    assert missing_slices(recs) == [2, 4]
    checks = merge_acceptance_checks(recs)
    assert len(checks) == 1
    assert "2, 4" in checks[0], "検査条件が欠落番号を名指ししていない: %r" % checks


def test_a_complete_sweep_has_nothing_to_check():
    """全部終わっていれば検査は空。**素通りする検査**を1本置くのとは違う。

    空リストなら「検査対象が無かった」と読める。常に通る検査を置くと
    「検査して問題なし」に見えてしまう。
    """
    from relay.fanout import merge_acceptance_checks

    assert merge_acceptance_checks([{"subtask_index": 1, "outcome": "DONE"}]) == []
    assert merge_acceptance_checks([]) == []


def test_the_merge_goal_carries_the_checks_and_the_cwd():
    from relay.fanout import aggregation_goal

    recs = [{"subtask_index": 1, "outcome": "DONE", "text": "a"},
            {"subtask_index": 2, "outcome": "STUCK", "text": ""}]
    goal = aggregation_goal("親ゴール", recs, campaign_id="cabc", cwd="C:/work")
    assert goal.get("cwd") == "C:/work", "子と同じ作業ディレクトリで走らない"
    assert goal.get("checks"), "ギャップがあるのに検査条件が付いていない"


def test_a_complete_merge_goal_has_no_checks_key():
    """検査が無いときはキー自体を置かない。空の checks を渡すと『検査あり』に見える。"""
    from relay.fanout import aggregation_goal

    goal = aggregation_goal("親ゴール", [{"subtask_index": 1, "outcome": "DONE", "text": "a"}],
                            campaign_id="cabc")
    assert "checks" not in goal


def test_the_campaign_remembers_the_working_directory():
    """親の cwd が campaign に残ること。残らなければ統合には届かない。"""
    import io
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with io.open(os.path.join(root, "relay", "relay_fleet.py"), encoding="utf-8") as fh:
        src = fh.read()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert '"cwd": (kids[0] or {}).get("cwd")' in code, (
        "campaign が cwd を覚えていない -- aggregation_goal に渡す値が常に None になる")
