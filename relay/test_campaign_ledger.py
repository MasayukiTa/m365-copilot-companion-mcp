"""分割した家族の台帳が、読み戻せる形で書かれていること。

## 何が起きていたか

`relay_fleet` は分割のたびに `.fleet/campaigns.jsonl` へ子1件ずつ書いていた。理由もコメントに
ある:「add_box lives in memory: if the run dies here the children vanish while the parent is
already recorded finished, so the work would be lost without a trace of what it was.」

**その理由が成立していなかった。** 2026-08-28 実測: リポジトリ全体で**書き手1箇所・読み手ゼロ**。
そして書かれていた行は `campaign_id` / `task_id` / `subtask_index` / `text` だけで、
**親ゴールを持っていない** —— 統合が必要とする唯一のものが入っていなかった。
つまり読もうとしても読めない形だった。

## それが効く場面

`FleetContextLost` で fleet は新しいプロセスで `run_relay_fleet` に入り直す。
`campaigns` はメモリ上の dict なので空になり、`_unfinished()` はゴールしか返さない。
**クラッシュ前に分割した家族は二度と統合されない** —— 子が全部終わっても、
それを集めるために作られた答えは組み立てられないまま終わる。

## このテストが見張るもの

書く側が家族の行(親ゴール・件数・cwd)を出すこと、読む側がそれを復元すること、
そして**途中で切れた行があっても上の家族を落とさない**こと —— 途中で切れるのは、
まさにこのファイルが書かれた理由である「走行が死んだ」ときの形だから。
"""
import io
import json
import os

from relay.fanout import campaigns_from_ledger

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _header(cid="c1", goal="親ゴール", n=2, cwd="C:/work"):
    return json.dumps({"kind": "campaign", "campaign_id": cid, "goal": goal,
                       "n": n, "cwd": cwd}, ensure_ascii=False)


def _child(cid="c1", idx=1):
    return json.dumps({"campaign_id": cid, "task_id": "%s-%d" % (cid, idx),
                       "subtask_index": idx, "text": "子%d" % idx}, ensure_ascii=False)


def test_a_family_round_trips():
    fam = campaigns_from_ledger([_header(), _child(idx=1), _child(idx=2)])
    assert list(fam) == ["c1"]
    assert fam["c1"]["goal"] == "親ゴール"
    assert fam["c1"]["n"] == 2
    assert fam["c1"]["cwd"] == "C:/work"
    assert len(fam["c1"]["children"]) == 2


def test_a_truncated_last_line_does_not_lose_the_families_above_it():
    """途中で切れた行を捨てて、その上は残すこと。

    走行が書き込み中に死ぬのは、このファイルが存在する理由そのもの。そこで全部落とすなら
    台帳を持つ意味が無い。
    """
    lines = [_header(), _child(idx=1), '{"campaign_id": "c1", "subtask']
    fam = campaigns_from_ledger(lines)
    assert "c1" in fam and len(fam["c1"]["children"]) == 1


def test_a_family_without_its_header_is_not_returned():
    """親ゴールが無い家族は返さない。

    返すと goal が空文字のまま統合に回り、**何も無いものを統合**することになる。
    `ready_to_aggregate` が子ゼロを ready にしないのと同じ理由。
    """
    fam = campaigns_from_ledger([_child(cid="c9", idx=1)])
    assert fam == {}


def test_children_from_several_families_do_not_mix():
    lines = [_header("c1", "親A", 1), _child("c1", 1),
             _header("c2", "親B", 2), _child("c2", 1), _child("c2", 2)]
    fam = campaigns_from_ledger(lines)
    assert sorted(fam) == ["c1", "c2"]
    assert len(fam["c1"]["children"]) == 1
    assert len(fam["c2"]["children"]) == 2
    assert fam["c2"]["goal"] == "親B"


def test_a_header_after_its_children_still_binds_them():
    """順序に依存しないこと。追記ファイルなので順序は保証されるが、
    その保証に寄りかかると、resume で再度書かれたときに崩れる。"""
    fam = campaigns_from_ledger([_child(idx=1), _child(idx=2), _header()])
    assert fam["c1"]["goal"] == "親ゴール"
    assert len(fam["c1"]["children"]) == 2


def test_empty_and_garbage_are_survivable():
    assert campaigns_from_ledger([]) == {}
    assert campaigns_from_ledger(None) == {}
    assert campaigns_from_ledger(["", "   ", "not json at all"]) == {}


# ---- 書く側 -----------------------------------------------------------------------------------

def _fleet_source():
    with io.open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8") as fh:
        src = fh.read()
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def test_the_writer_records_the_parent_goal():
    """子だけ書いても復元できない。家族の行が要る。"""
    code = _fleet_source()
    assert '"kind": "campaign"' in code, "家族の行を書いていない"
    assert '"goal": parent_goal' in code, "親ゴールを記録していない -- 統合が必要とする唯一のもの"


def test_the_writer_records_the_size_and_the_directory():
    code = _fleet_source()
    i = code.index('"kind": "campaign"')
    blk = code[i:i + 260]
    assert '"n": len(kids)' in blk, "件数を記録していない(完全性ゲートが使う)"
    assert '"cwd"' in blk, "作業ディレクトリを記録していない"
