"""枝を生やす前に潰しておく2件。どちらも「枝が1本のうちは症状が出ない」種類の欠陥。

1. 同一性を genome_id で判定していた。既定値を明示した genome は別 id を持ちながら
   同一 manifest に materialize する。実測: genome_id 7a9b1bb314fe と 21a42e331dc2 が
   どちらも harness 942eb26c19d2f138a688 になる。A/A 比較が2つの名前を着て通る。
2. plateaued の母集団が archive 全体だった。枝が2本になった瞬間、
   片方の KEEP がもう片方の停滞判定を延命させる。
"""
import pytest

from relay.selfimprove import archive as A
from relay.selfimprove import manifest as M
from relay.selfimprove import scheduler as S


# ---- 同一性は materialize 後のハーネスで決める --------------------------------------------------

def test_two_genome_ids_can_be_the_same_program():
    """直す前の欠陥そのものを固定する。id が違うことは別物であることを意味しない。"""
    default = M.base_manifest()["parameters"]["max_retries"]
    explicit = {"parameters": {"max_retries": default}}
    assert A.genome_id({}) != A.genome_id(explicit), "前提が崩れた: id が同じなら穴も無い"
    assert M.same_program({}, explicit), "同じプログラムなのに別物と判定された"


def test_a_real_change_is_not_the_same_program():
    assert not M.same_program({}, {"components": {"transport": "transport/v2"}})


def test_materialize_returns_the_harness_the_genome_actually_produces():
    manifest, hid = M.materialize({"components": {"transport": "transport/v2"}})
    assert manifest["components"]["transport"] == "transport/v2"
    assert hid == M.harness_id(manifest)


def test_the_identity_rule_lives_in_the_frozen_constitution():
    """『両腕が同じプログラム』を防ぐ規則は審判側の規則。
    凍結の外に置くと、祝福を受けずに緩められる。"""
    from relay.selfimprove import frozen as F
    assert "relay/selfimprove/manifest.py" in F.FROZEN_MANIFEST
    assert hasattr(M, "same_program") and hasattr(M, "materialize")


def test_the_proposer_rejects_a_no_op_decided_on_the_harness(monkeypatch):
    """propose.py の (d) は genome_id 比較だった。既定値を明示した候補が
    フィルタを通り、評価器へ行き、両腕が同じプログラムの実験として採点された。"""
    from relay.selfimprove.propose import _same_program
    default = M.base_manifest()["parameters"]["max_retries"]
    assert _same_program({"parameters": {"max_retries": default}}, {"genome": {}})
    assert not _same_program({"components": {"transport": "transport/v2"}}, {"genome": {}})


def test_a_thin_archive_row_does_not_reject_everything():
    """genome を持たない行に対して True を返すと、候補が全部消える。
    弱い検査に落ちるほうが、黙って全滅させるよりよい。"""
    from relay.selfimprove.propose import _same_program
    assert not _same_program({"parameters": {"max_retries": 1}}, {"id": "x"})
    assert not _same_program({"parameters": {"max_retries": 1}}, None)


# ---- 系統 ---------------------------------------------------------------------------------------

def _arc(tmp_path):
    return A.Archive(path=str(tmp_path / "entries.jsonl"))


def _add(arc, genome, verdict, parent=None):
    """Returns the id -- Archive.add returns a string, not the row."""
    g = dict(genome)
    g["parent_id"] = parent
    return arc.add(g, slice_ids=["e1"], pass_at_1=0.5, ci=(0.4, 0.6),
                   gate_verdict=verdict)


def test_lineage_follows_parents_and_ignores_the_other_branch(tmp_path):
    arc = _arc(tmp_path)
    a1 = _add(arc, {"parameters": {"max_retries": 1}}, "REJECT")
    b1 = _add(arc, {"parameters": {"max_retries": 2}}, "KEEP")
    a2 = _add(arc, {"parameters": {"max_retries": 3}}, "REJECT", parent=a1)
    _add(arc, {"parameters": {"max_retries": 4}}, "KEEP", parent=b1)

    chain = [e["id"] for e in arc.lineage(a2)]
    assert chain == [a1, a2], chain


def test_lineage_of_an_unknown_tip_is_empty(tmp_path):
    assert _arc(tmp_path).lineage("nope") == []


def test_a_cycle_in_recorded_lineage_does_not_spin(tmp_path):
    arc = _arc(tmp_path)
    eid = _add(arc, {"parameters": {"max_retries": 1}}, "KEEP")
    arc.get(eid)["genome"]["parent_id"] = eid   # a row that claims to be its own parent
    assert len(arc.lineage(eid)) == 1


def test_tip_is_the_row_the_last_night_wrote(tmp_path):
    arc = _arc(tmp_path)
    _add(arc, {"parameters": {"max_retries": 1}}, "KEEP")
    last = _add(arc, {"parameters": {"max_retries": 2}}, "REJECT")
    assert arc.tip()["id"] == last
    assert _arc(tmp_path / "empty").tip() is None


# ---- 母集団の分離 -------------------------------------------------------------------------------

def _fail(n):
    return [{"state": "REJECT"} for _ in range(n)]


def test_a_keep_on_another_branch_no_longer_resets_this_branchs_plateau():
    """枝を生やした瞬間に出る欠陥。B の KEEP が、毎回落ちている A の
    停滞判定を延命させ、ループは死んだ方向に夜を使い続ける。"""
    flat = _fail(4) + [{"state": "KEEP"}]           # 他の枝の KEEP が混ざった見え方
    scoped = _fail(5)                                # この系統は5連続で落ちている
    reasons = S.preconditions(recent_decisions=flat, lineage_decisions=scoped,
                              budget_candidates=1, baseline_path=None)
    assert any("plateau" in r or "passed its gate" in r for r in reasons), reasons


def test_the_plateau_still_reads_the_flat_list_when_there_is_one_branch():
    """既存の呼び出し側の挙動を変えない。枝が無い世界では母集団は1つ。"""
    reasons = S.preconditions(recent_decisions=_fail(5), budget_candidates=1)
    assert any("passed its gate" in r for r in reasons), reasons


def test_health_is_not_scoped_to_a_branch():
    """INFRA_ABORT は計器の属性であって枝の属性ではない。
    どの枝で起きようが『今夜は走らせるな』の根拠になる。"""
    import inspect
    src = inspect.getsource(S.preconditions)
    # シグネチャを拾わないよう、呼び出しの実引数を見る。
    i = src.index("HF.observe(")
    call = src[i:src.index(")", i)]
    assert "recent_decisions" in call and "lineage_decisions" not in call, call
    j = src.index("POL.plateaued")
    assert "lineage_decisions" in src[i:j], "停滞判定が系統スコープになっていない"


def test_nightly_builds_both_populations(tmp_path, monkeypatch):
    """片方しか作っていなければ、分けた意味がない。"""
    import inspect
    src = inspect.getsource(S.nightly)
    assert "archive.lineage(" in src
    assert "lineage_decisions=lineage_decisions" in src
    assert "archive.all()" in src, "グローバル母集団が消えている"


def test_an_empty_lineage_falls_back_rather_than_disabling_the_plateau(tmp_path):
    """空リストを渡すと『停滞していない』と読まれ、判定が黙って無効になる。
    None にして平坦リストへ落とすのが正しい。"""
    import inspect
    src = inspect.getsource(S.nightly)
    assert "or None" in src, "空の系統が偽の『停滞なし』になる"


# ---- 「見えない」を「同じ」と報告しないこと -------------------------------------------------------

def test_a_difference_the_manifest_cannot_see_is_not_reported_as_sameness():
    """same_program を no-op フィルタに入れた最初の版は、既存テスト2件で
    候補を全滅させた。archive の genome は knobs/cards 語彙で書かれており、
    apply_genome は components と parameters しか読まないので、
    本物の変異が全部同じ基底に潰れて『同一プログラム』になった。

    呼び出し側はこの答えで候補を**落とす**。だから偽陽性は提案を黙って空にする。"""
    assert not M.same_program({"cards": {"c": "a"}}, {"cards": {"c": "b"}})
    assert not M.represented_by_manifest({"cards": {"c": "a"}})


def test_lineage_and_prose_do_not_make_a_genome_invisible():
    """parent_id や note は振る舞いを変えない。これで False になると
    実際の重複を取り逃す。"""
    assert M.represented_by_manifest({"parameters": {"max_retries": 3},
                                      "parent_id": "x", "note": "why"})


def test_the_guard_is_written_next_to_the_rule():
    """『マニフェストが見える範囲でしか答えない』が読めなければ、
    次の人が同じ全滅を再現する。"""
    import inspect
    src = inspect.getsource(M.represented_by_manifest)
    assert "cards" in src and "scaffold vocabulary" in src
