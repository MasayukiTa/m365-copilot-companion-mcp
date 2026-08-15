"""The replay coreset: is it diverse, is it difficult, and does it still have headroom.

The two ways this goes wrong are opposite and both produce a set that cannot rank anything --
all-hardest, where every candidate scores zero, and greedy-by-difficulty, where the hardest
cases all come from one cluster and the set is one failure repeated.
"""
from __future__ import annotations

from relay.selfimprove import coreset as C


def _fail(category, reason, *, mode="functional", latency=1.0, **kw):
    row = {"category": category, "success": False, "latency_s": latency,
           "security_score": 0.0 if mode == "security" else 1.0,
           "side_effect_score": 0.0 if mode == "side_effect" else 1.0,
           "infra_failure": mode == "infra",
           "details": {"reason": reason}}
    row.update(kw)
    return row


def test_two_failures_differing_only_in_a_filename_are_one_failure():
    """同じ障害を20回集めた coreset は、20枠を使って1つの問いに答える。"""
    a = _fail("filesystem", "target file missing: C:/work/a.txt")
    b = _fail("filesystem", "target file missing: C:/work/b.txt")
    assert C.signature(a) == C.signature(b)


def test_different_failure_modes_are_different_failures():
    same_text = "the check did not hold"
    assert C.signature(_fail("excel", same_text)) != \
        C.signature(_fail("excel", same_text, mode="security"))


def test_successes_are_excluded_from_the_replay_set():
    """全部が通る事例で薄めると、検出したい差そのものが薄まる。"""
    rows = [_fail("excel", "wrong total"), {"category": "excel", "success": True}]
    assert sum(len(v) for v in C.cluster(rows).values()) == 1


def test_the_budget_is_spent_on_different_failures_before_more_of_one():
    """難易度だけで貪欲に取ると、最難のものが同じクラスタから来て多様性が消える。"""
    rows = ([_fail("excel", "formula flattened", latency=50) for _ in range(10)]
            + [_fail("sql", "null semantics")]
            + [_fail("auth_consent", "reported done while parked", mode="security")])
    out = C.select(rows, budget=3)
    sigs = {C.signature(r) for r in out["coreset"]}
    assert len(sigs) == 3, "同じクラスタで予算を使い切っている"


def test_within_a_cluster_the_harder_case_is_taken_first():
    rows = [_fail("excel", "wrong total", latency=1),
            _fail("excel", "wrong total", latency=59)]
    out = C.select(rows, budget=1)
    assert out["coreset"][0]["latency_s"] == 59


def test_a_security_failure_outranks_a_functional_one_of_equal_length():
    assert C.difficulty(_fail("x", "r", mode="security")) > \
        C.difficulty(_fail("x", "r", mode="functional"))


def test_an_infra_failure_is_not_difficulty():
    """測れなかったことを『難しかった』と読むと、環境不調が coreset を占領する。"""
    assert C.difficulty(_fail("x", "r", mode="infra")) < 0


def test_a_more_common_failure_is_visited_before_a_singleton():
    """40回起きた障害は製品の実際の挙動で、1回のものに押し出されてはいけない。"""
    rows = ([_fail("excel", "flattened") for _ in range(5)]
            + [_fail("ocr", "one off thing")])
    out = C.select(rows, budget=1)
    assert C.signature(out["coreset"][0])[0] == "excel"


def test_the_reduction_is_reported_rather_than_implied():
    """『340件から12件を再生した』は、2つめの数字にこそ意味がある。"""
    rows = [_fail("excel", "flattened") for _ in range(40)]
    out = C.select(rows, budget=5)
    assert out["considered"] == 40 and out["failures"] == 40
    assert out["dropped"] == 35


def test_a_history_with_one_failure_says_so_instead_of_pretending_to_be_diverse():
    rows = [_fail("excel", "flattened") for _ in range(9)]
    out = C.select(rows, budget=5)
    assert out["clusters"] == 1
    assert "cannot be more diverse" in out["note"]


def test_an_empty_history_produces_an_empty_set_with_a_reason():
    out = C.select([])
    assert out["coreset"] == [] and "no failures" in out["reason"]


def test_the_landscape_is_reportable_on_its_own():
    """障害が1クラスタに固まっている歴史は『それを直せ』と言っている。
    どんな集計通過率にもそれは映らない。"""
    rows = ([_fail("excel", "flattened") for _ in range(7)]
            + [_fail("sql", "null semantics")])
    top = C.summarise(C.cluster(rows))
    assert top[0]["count"] == 7 and "excel" in top[0]["signature"]
