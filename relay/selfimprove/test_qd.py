"""Semantic descriptors: do the cells actually separate behaviours?

A quality-diversity map earns its cost by keeping behaviours a single score would discard. So
the test that matters is not that the function runs -- it is that two harnesses a scalar
cannot tell apart land in different cells, and that two which really are alike land in one.
"""
from __future__ import annotations

from relay.selfimprove import qd as QD


def _ep(category, success, **kw):
    row = {"category": category, "success": success, "security_score": 1.0,
           "side_effect_score": 1.0, "infra_failure": False}
    row.update(kw)
    return row


def test_two_harnesses_with_the_same_score_can_occupy_different_cells():
    """同点でも、片方は文書系が得意で片方は状態管理が得意、というのは別の挙動。
    スカラーはこれを潰す。潰さないのが QD の存在理由。"""
    docs = [_ep("filesystem", True), _ep("excel", True), _ep("long_running", False)]
    state = [_ep("filesystem", False), _ep("excel", False), _ep("long_running", True)]
    assert QD.cell_key(QD.descriptors(docs)) != QD.cell_key(QD.descriptors(state))


def test_where_it_fails_is_part_of_the_address():
    """同じ通過率でも、落ちる場所が機能かセキュリティかで交換可能性が違う。"""
    functional = [_ep("filesystem", True), _ep("csv_json", False)]
    security = [_ep("filesystem", True), _ep("security", False, security_score=0.0)]
    a, b = QD.descriptors(functional), QD.descriptors(security)
    assert a["failure_mode"] == "functional"
    assert b["failure_mode"] == "security"


def test_a_security_failure_outranks_a_pile_of_functional_ones():
    """数えたら5件の機能失敗が1件のセキュリティ失敗を多数決で消す。
    比較可能な量ではないので順序で決める。"""
    rows = [_ep("csv_json", False) for _ in range(5)]
    rows.append(_ep("security", False, security_score=0.0))
    assert QD.descriptors(rows)["failure_mode"] == "security"


def test_over_claiming_is_its_own_axis():
    """『終わっていないものを終わったと報告する』はこの製品の代表的な障害。
    率に混ぜず、独立した軸として持つ。"""
    honest = [_ep("auth_consent", True), _ep("long_running", True)]
    liar = [_ep("auth_consent", False, security_score=0.0), _ep("long_running", True)]
    assert QD.descriptors(honest)["caution"] == "appropriately_cautious"
    assert QD.descriptors(liar)["caution"] == "over_claims"


def test_a_harness_that_passes_nothing_has_no_strength():
    """最も苦手でない分野を『強み』と呼ぶと、居るべきでないセルに入る。"""
    rows = [_ep("filesystem", False), _ep("excel", False)]
    assert QD.descriptors(rows)["strength"] == "none"


def test_missing_results_place_the_genome_rather_than_crashing():
    d = QD.descriptors([])
    assert d["strength"] == "unknown" and QD.cell_key(d)


def test_rows_without_semantic_descriptors_are_skipped_not_pooled():
    """既定セルを作ると、記述の無い古い行が全部そこに集まり、
    誰も記述していない挙動の『エリート』が生まれる。"""
    entries = [
        {"pass_at_1": 0.9, "descriptors": {}},                       # legacy row
        {"pass_at_1": 0.1, "descriptors": {"semantic": QD.descriptors(
            [_ep("filesystem", True)])}},
    ]
    m = QD.map_of(entries)
    assert len(m) == 1
    assert m[list(m)[0]]["pass_at_1"] == 0.1


def test_coverage_reports_the_shape_of_a_campaign():
    """2セルしか埋まらなかった campaign は、最高点が何であれ
    1つの挙動をダイヤル回ししただけ。それが見えることが結果と同じくらい重要。"""
    entries = [
        {"pass_at_1": 0.5, "descriptors": {"semantic": QD.descriptors([_ep("excel", True)])}},
        {"pass_at_1": 0.6, "descriptors": {"semantic": QD.descriptors([_ep("excel", True)])}},
        {"pass_at_1": 0.4, "descriptors": {"semantic": QD.descriptors(
            [_ep("security", False, security_score=0.0)])}},
        {"pass_at_1": 0.7, "descriptors": {}},
    ]
    cov = QD.coverage(entries)
    assert cov["cells_occupied"] == 2
    assert cov["described"] == 3 and cov["total"] == 4
    assert "security" in cov["failure_modes_seen"]


def test_categories_are_grouped_rather_than_one_cell_each():
    """カテゴリごとに1セルだと、1 campaign 数個の genome では
    セル数のほうが多くなり、地図ではなく一覧になる。"""
    assert QD.family_of("filesystem") == QD.family_of("excel") == "documents"
    assert QD.family_of("long_running") == QD.family_of("routing") == "orchestration"
    assert QD.family_of("something_new") == "other"
