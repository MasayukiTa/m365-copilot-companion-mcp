"""Unit tests for the genome archive. Run: python -m relay.selfimprove.test_archive"""
import os
import tempfile

from relay.selfimprove import archive as A


def _genome(knobs=None, cards=None, parent_id=None, note=""):
    return {
        "knobs": knobs or {},
        "cards": cards or {},
        "parent_id": parent_id,
        "note": note,
    }


def test_genome_id_determinism():
    g1 = _genome({"SS_SELFTEST": "1"}, {"trace": "follow the symptom"}, parent_id="root", note="a")
    g2 = _genome({"SS_SELFTEST": "1"}, {"trace": "follow the symptom"}, parent_id="other", note="b")
    # same knobs+cards -> same id, regardless of parent_id / note
    assert A.genome_id(g1) == A.genome_id(g2)
    assert len(A.genome_id(g1)) == 12 and all(c in "0123456789abcdef" for c in A.genome_id(g1))
    # key order in knobs/cards must not matter (canonicalisation sorts)
    g3 = _genome({"B": "1", "A": "0"})
    g4 = _genome({"A": "0", "B": "1"})
    assert A.genome_id(g3) == A.genome_id(g4)
    # a real change of content changes the id
    assert A.genome_id(_genome({"X": "1"})) != A.genome_id(_genome({"X": "2"}))
    print("ok test_genome_id_determinism")


def test_add_get_reload():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "archive", "entries.jsonl")
        ar = A.Archive(path)
        assert len(ar) == 0 and ar.get("nope") is None and ar.best() is None

        g = _genome({"K": "1"}, {"c": "txt"}, parent_id=None, note="seed")
        eid = ar.add(g, slice_ids=["i1", "i2"], pass_at_1=0.5, ci=[0.4, 0.6],
                     gate_verdict="keep", descriptors={"diff_bin": "surgical"}, ts=10)
        assert eid == A.genome_id(g)
        got = ar.get(eid)
        assert got["pass_at_1"] == 0.5 and got["parent_id"] is None
        assert got["slice_ids"] == ["i1", "i2"] and got["gate_verdict"] == "keep"
        assert got["ci"] == [0.4, 0.6] and got["ts"] == 10
        assert len(ar) == 1

        # parent_id flows from the genome
        child = _genome({"K": "2"}, parent_id=eid)
        cid = ar.add(child, slice_ids=["i3"], pass_at_1=0.6)
        assert ar.get(cid)["parent_id"] == eid

        # reload from disk reproduces both entries
        ar2 = A.Archive(path)
        assert len(ar2) == 2
        assert ar2.get(eid)["pass_at_1"] == 0.5
        assert ar2.get(cid)["parent_id"] == eid
        assert [e["id"] for e in ar2.all()] == [eid, cid]
    print("ok test_add_get_reload")


def test_best():
    with tempfile.TemporaryDirectory() as d:
        ar = A.Archive(os.path.join(d, "a.jsonl"))
        a = ar.add(_genome({"K": "1"}), slice_ids=[], pass_at_1=0.40)
        b = ar.add(_genome({"K": "2"}), slice_ids=[], pass_at_1=0.70)
        ar.add(_genome({"K": "3"}), slice_ids=[], pass_at_1=0.55)
        assert ar.best()["id"] == b and ar.best()["pass_at_1"] == 0.70
        # tie -> most recently added wins
        c1 = ar.add(_genome({"K": "4"}), slice_ids=[], pass_at_1=0.70)
        assert ar.best()["id"] == c1
        # alternate metric
        assert a == A.genome_id(_genome({"K": "1"}))
    print("ok test_best")


def test_descriptors_and_cell_key():
    # surgical / short / precision
    recs = [
        {"diff_size": 10, "turns": 5, "miss_class": "precision"},
        {"diff_size": 20, "turns": 6, "miss_class": "precision"},
        {"diff_size": 12, "turns": 4, "miss_class": "underfit"},
    ]
    desc = A.descriptors(recs)
    assert desc == {"diff_bin": "surgical", "turns_bin": "short", "dominant_miss": "precision"}
    assert A.cell_key(desc) == "surgical|short|precision"

    # broad / long, unknown miss_class folded to "other"
    big = [{"diff_size": 500, "turns": 40, "miss_class": "mystery"} for _ in range(3)]
    d2 = A.descriptors(big)
    assert d2["diff_bin"] == "broad" and d2["turns_bin"] == "long" and d2["dominant_miss"] == "other"

    # medium / mid boundary
    d3 = A.descriptors([{"diff_size": 100, "turns": 10, "miss_class": "regression"}])
    assert d3["diff_bin"] == "medium" and d3["turns_bin"] == "mid"

    # tie in dominant_miss broken alphabetically (precision < regression)
    tie = [
        {"diff_size": 5, "turns": 1, "miss_class": "regression"},
        {"diff_size": 5, "turns": 1, "miss_class": "precision"},
    ]
    assert A.descriptors(tie)["dominant_miss"] == "precision"

    # empty input -> stable addressable cell
    empty = A.descriptors([])
    assert empty == {"diff_bin": "empty", "turns_bin": "empty", "dominant_miss": "none"}
    assert A.cell_key(empty) == "empty|empty|none"
    print("ok test_descriptors_and_cell_key")


def test_qd_map():
    with tempfile.TemporaryDirectory() as d:
        ar = A.Archive(os.path.join(d, "a.jsonl"))
        surgical = {"diff_bin": "surgical", "turns_bin": "short", "dominant_miss": "precision"}
        broad = {"diff_bin": "broad", "turns_bin": "long", "dominant_miss": "wrong_layer"}
        # two genomes in the surgical cell; the higher pass@1 should be the elite
        ar.add(_genome({"K": "1"}), slice_ids=[], pass_at_1=0.40, descriptors=surgical)
        win = ar.add(_genome({"K": "2"}), slice_ids=[], pass_at_1=0.62, descriptors=surgical)
        # one genome in the broad cell
        b = ar.add(_genome({"K": "3"}), slice_ids=[], pass_at_1=0.55, descriptors=broad)
        # one genome with no descriptors -> excluded from the map
        ar.add(_genome({"K": "4"}), slice_ids=[], pass_at_1=0.99, descriptors=None)

        m = ar.qd_map()
        assert set(m.keys()) == {"surgical|short|precision", "broad|long|wrong_layer"}
        assert m["surgical|short|precision"]["id"] == win
        assert m["broad|long|wrong_layer"]["id"] == b
    print("ok test_qd_map")


def test_select_parent():
    with tempfile.TemporaryDirectory() as d:
        ar = A.Archive(os.path.join(d, "a.jsonl"))
        assert ar.select_parent() is None and ar.select_parent("qd") is None

        surgical = {"diff_bin": "surgical", "turns_bin": "short", "dominant_miss": "precision"}
        broad = {"diff_bin": "broad", "turns_bin": "long", "dominant_miss": "wrong_layer"}

        # surgical elite has the higher score but will get a descendant (well-explored)
        surg = ar.add(_genome({"K": "1"}), slice_ids=[], pass_at_1=0.70, descriptors=surgical)
        # broad elite: lower score, but no descendants -> least-explored
        broad_id = ar.add(_genome({"K": "2"}), slice_ids=[], pass_at_1=0.50, descriptors=broad)
        # give the surgical elite a descendant so its cell looks explored
        ar.add(_genome({"K": "3"}, parent_id=surg), slice_ids=[], pass_at_1=0.40,
               descriptors=surgical)

        # "best" -> the global top score (surgical)
        assert ar.select_parent("best")["id"] == surg
        # "qd" -> the least-explored cell's elite (broad, 0 descendants) despite lower score
        chosen = ar.select_parent("qd")
        assert chosen["id"] == broad_id
        # unknown strategy falls back to best
        assert ar.select_parent("whatever")["id"] == surg
    print("ok test_select_parent")


if __name__ == "__main__":
    test_genome_id_determinism()
    test_add_get_reload()
    test_best()
    test_descriptors_and_cell_key()
    test_qd_map()
    test_select_parent()
    print("ALL ARCHIVE TESTS PASSED")


def test_a_row_records_when_it_was_written(tmp_path):
    """全既存行が `ts: null` だった -- 呼び出し側が一度も渡していなかったため。

    時刻の無い実験記録は、他の実験と順序づけられないし、その日に起きた他の事象と
    突き合わせることもできない。いま書いているのだから「いま」は推測ではない。"""
    import time as _t
    a = A.Archive(path=str(tmp_path / "entries.jsonl"))
    before = _t.time()
    a.add({"components": {}, "parameters": {}}, slice_ids=["e1"], pass_at_1=0.5)
    after = _t.time()
    import json as _json
    row = _json.loads((tmp_path / "entries.jsonl").read_text(encoding="utf-8").strip())
    assert row["ts"] is not None, "時刻の無い行が書かれた"
    assert before <= row["ts"] <= after


def test_an_explicit_timestamp_still_wins(tmp_path):
    """再生やバックフィルでは、書いた時刻ではなく起きた時刻が正しい。"""
    a = A.Archive(path=str(tmp_path / "entries.jsonl"))
    a.add({"components": {}, "parameters": {}}, slice_ids=["e1"], pass_at_1=0.5, ts=1234.5)
    import json as _json
    row = _json.loads((tmp_path / "entries.jsonl").read_text(encoding="utf-8").strip())
    assert row["ts"] == 1234.5


# ---------------------------------------------------------------------------
# Every named state must be REACHABLE, not merely defined. A state in the enum that no input
# can produce is a promise the loop does not keep, and the way to find out is to construct
# the input for each rather than to read the branches.
# ---------------------------------------------------------------------------

def _decide(**kw):
    from relay.selfimprove import decision as D
    base = {"gate": {"keep": False, "verdict": "non-positive", "n": 4},
            "security": {"regressed": False}, "regression": {"regressed": False},
            "sentinel": {"regressed": False, "comparable": 2},
            "infra": {"aborted": False}}
    base.update(kw)
    return (D.decide(**base) or {}).get("state")


def test_every_named_state_is_reachable():
    import pytest as _pytest

    from relay.selfimprove import decision as D

    wanted = {
        D.KEEP: dict(gate={"keep": True, "verdict": "positive", "n": 40}),
        D.REJECT: dict(gate={"keep": False, "verdict": "regression", "n": 40}),
        D.INCONCLUSIVE: dict(gate={"keep": False, "verdict": "underpowered", "n": 1}),
        D.INFRA_ABORT: dict(infra={"aborted": True, "reason": "x"}),
        D.SECURITY_REJECT: dict(security={"regressed": True, "lost": ["s1"]}),
        D.SENTINEL_REJECT: dict(sentinel={"regressed": True, "comparable": 2}),
        D.REGRESSION_REJECT: dict(regression={"regressed": True, "lost": ["r1"]}),
        # ONLY WHEN ACTIVATING. Incomplete evidence is worth reporting on any run and worth
        # STOPPING only when something is about to be switched on -- so this state is
        # unreachable in a report-only run by design, and that distinction is the point.
        D.NEEDS_HUMAN_REVIEW: dict(gate={"keep": True, "verdict": "positive", "n": 40},
                                   sentinel={"unevaluable": True, "reason": "no sealed pair"},
                                   will_activate=True),
    }
    assert set(wanted) == set(D.STATES), "a state was added or removed without a way to reach it"
    for state, kw in wanted.items():
        assert _decide(**kw) == state, "%s is defined but not reachable" % state


def test_uncertainty_stops_an_activation_but_not_a_report():
    """報告のみの走行では『不完全な証拠』は報告して通す。有効化しようとした瞬間に止める。

    危険な行為は有効化のほうであって、測ること自体ではない。"""
    from relay.selfimprove import decision as D
    incomplete = dict(gate={"keep": True, "verdict": "positive", "n": 40},
                      security={"regressed": False, "incomplete_coverage": ["s1"]})
    assert _decide(**incomplete) == D.KEEP
    assert _decide(**dict(incomplete, will_activate=True)) == D.NEEDS_HUMAN_REVIEW
