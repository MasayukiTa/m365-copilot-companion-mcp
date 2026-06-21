"""Unit tests for the self-improvement dashboard aggregator.

Run: python -m relay.selfimprove.test_dashboard

Hermetic: every test builds synthetic ledgers in a TemporaryDirectory and sets file mtimes
explicitly (os.utime) so ordering is deterministic and no real ledger is touched or written.
"""
import json
import os
import tempfile

from relay.selfimprove import dashboard as D
from relay.selfimprove.archive import Archive


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False))


def _report(toggle, n, on_r, off_r, net_pp, p, verdict, keep):
    return {
        "toggle": toggle, "n": n, "dataset": "Verified",
        "on_resolved": on_r, "off_resolved": off_r,
        "gate": {"keep": keep, "verdict": verdict, "reason": "synthetic",
                 "p": p, "net_pp": net_pp, "n": n, "b": 0, "c": 0,
                 "both": 0, "neither": n, "n_on": on_r, "n_off": off_r},
    }


def test_aggregates_correctly():
    with tempfile.TemporaryDirectory() as d:
        # --- burned.jsonl: a few reasons ---
        burned = os.path.join(d, "burned.jsonl")
        _write_jsonl(burned, [
            {"instance_id": "a__a-1", "reason": "diagnosis"},
            {"instance_id": "a__a-2", "reason": "diagnosis"},
            {"instance_id": "b__b-3", "reason": "ab"},
            {"instance_id": "c__c-4", "reason": "ab"},
            {"instance_id": "c__c-5", "reason": "score"},
        ])

        # --- two report files with distinct mtimes (older = first, newer = second) ---
        rep_old = os.path.join(d, "selfimprove_report_old.json")
        rep_new = os.path.join(d, "selfimprove_report_new.json")
        _write_json(rep_old, _report("OLD_TOG", 80, 69, 64, 6.25, 0.18, "suggestive", False))
        _write_json(rep_new, _report("NEW_TOG", 400, 345, 310, 8.75, 0.001, "keep", True))
        # set mtimes explicitly: old strictly before new
        os.utime(rep_old, (1000, 1000))
        os.utime(rep_new, (2000, 2000))
        reports_glob = os.path.join(d, "selfimprove_report_*.json")

        # --- temp Archive with 2-3 genomes (distinct descriptors -> distinct qd cells) ---
        arc_path = os.path.join(d, "archive_entries.jsonl")
        arc = Archive(arc_path)
        arc.add({"knobs": {"X": "0"}, "cards": {}, "parent_id": None},
                slice_ids=["s1"], pass_at_1=0.40, ci=[0.3, 0.5], gate_verdict="keep",
                descriptors={"diff_bin": "surgical", "turns_bin": "short", "dominant_miss": "precision"},
                ts=10)
        gid2 = arc.add({"knobs": {"X": "1"}, "cards": {}, "parent_id": None},
                       slice_ids=["s2"], pass_at_1=0.55, ci=[0.45, 0.65], gate_verdict="suggestive",
                       descriptors={"diff_bin": "medium", "turns_bin": "mid", "dominant_miss": "underfit"},
                       ts=20)
        arc.add({"knobs": {"X": "2"}, "cards": {}, "parent_id": gid2},
                slice_ids=["s3"], pass_at_1=0.60, ci=[0.5, 0.7], gate_verdict="keep",
                descriptors={"diff_bin": "broad", "turns_bin": "long", "dominant_miss": "regression"},
                ts=30)

        # --- grade_results (read-only, just to exercise the read path) ---
        grade = os.path.join(d, "grade_results.jsonl")
        _write_jsonl(grade, [
            {"instance_id": "g1", "verdict": "RESOLVED", "runid": "r", "ts": 1},
            {"instance_id": "g2", "verdict": "not", "runid": "r", "ts": 1},
        ])

        st = D.dashboard_state(archive_path=arc_path, burned_path=burned,
                               grade_results_path=grade, reports_glob=reports_glob)

        # top-level shape
        assert set(st.keys()) == {"summary", "ab_history", "pass1_trend", "burned_ledger", "archive"}

        # summary
        s = st["summary"]
        assert s["burned_total"] == 5
        assert s["archive_count"] == 3
        assert abs(s["latest_pass_at_1"] - 0.60) < 1e-9     # newest archive entry by insertion order
        assert s["latest_ab"] == {"net_pp": 8.75, "p": 0.001, "verdict": "keep", "keep": True}
        assert s["grade_results_count"] == 2

        # ab_history: oldest -> newest, by mtime
        ab = st["ab_history"]
        assert len(ab) == 2
        assert [r["toggle"] for r in ab] == ["OLD_TOG", "NEW_TOG"]
        assert ab[0]["net_pp"] == 6.25 and ab[0]["p"] == 0.18 and ab[0]["keep"] is False
        assert ab[1]["net_pp"] == 8.75 and ab[1]["verdict"] == "keep" and ab[1]["keep"] is True
        assert ab[0]["n"] == 80 and ab[1]["n"] == 400

        # pass1_trend: oldest -> newest (insertion order)
        pt = st["pass1_trend"]
        assert [round(x["pass_at_1"], 2) for x in pt] == [0.40, 0.55, 0.60]
        assert pt[0]["ci"] == [0.3, 0.5]

        # burned ledger
        bl = st["burned_ledger"]
        assert bl["total"] == 5
        assert bl["by_reason"] == {"diagnosis": 2, "ab": 2, "score": 1}
        assert len(bl["recent"]) == 5
        assert bl["recent"][-1] == {"instance_id": "c__c-5", "reason": "score"}

        # archive section
        a = st["archive"]
        assert a["count"] == 3
        assert len(a["genomes"]) == 3
        assert a["qd_cells"] == 3                            # three distinct MAP-Elites cells
        assert {"id", "parent_id", "pass_at_1", "gate_verdict", "descriptors"} <= set(a["genomes"][0])

        # render_text is plain ASCII and mentions the headline numbers
        text = D.render_text(st)
        assert isinstance(text, str) and text.isascii()
        assert "burned total  : 5" in text
        assert "archive count : 3" in text

    print("ok test_aggregates_correctly")


def test_recent_capped_at_20():
    with tempfile.TemporaryDirectory() as d:
        burned = os.path.join(d, "burned.jsonl")
        _write_jsonl(burned, [{"instance_id": "i%03d" % i, "reason": "ab"} for i in range(25)])
        st = D.dashboard_state(archive_path=os.path.join(d, "none.jsonl"),
                               burned_path=burned,
                               grade_results_path=os.path.join(d, "none.jsonl"),
                               reports_glob=os.path.join(d, "none_*.json"))
        bl = st["burned_ledger"]
        assert bl["total"] == 25
        assert len(bl["recent"]) == 20
        assert bl["recent"][-1]["instance_id"] == "i024"     # last 20, newest at the tail
    print("ok test_recent_capped_at_20")


def test_archive_genomes_capped_at_50():
    with tempfile.TemporaryDirectory() as d:
        arc_path = os.path.join(d, "entries.jsonl")
        arc = Archive(arc_path)
        for i in range(55):
            arc.add({"knobs": {"K": str(i)}, "cards": {}, "parent_id": None},
                    slice_ids=["s"], pass_at_1=0.1, descriptors={"diff_bin": "surgical",
                    "turns_bin": "short", "dominant_miss": "precision"}, ts=i)
        st = D.dashboard_state(archive_path=arc_path,
                               burned_path=os.path.join(d, "no.jsonl"),
                               grade_results_path=os.path.join(d, "no.jsonl"),
                               reports_glob=os.path.join(d, "no_*.json"))
        a = st["archive"]
        assert a["count"] == 55
        assert len(a["genomes"]) == 50                       # capped
        assert a["qd_cells"] == 1                            # all share one cell
        assert len(st["pass1_trend"]) == 55                  # trend is NOT capped
    print("ok test_archive_genomes_capped_at_50")


def test_all_sections_degrade_to_empty():
    # Point every path at a nonexistent directory -> every section empty/zero, NO exception.
    nodir = os.path.join(tempfile.gettempdir(), "no_such_dir_dashboard_zzz")
    st = D.dashboard_state(
        archive_path=os.path.join(nodir, "entries.jsonl"),
        burned_path=os.path.join(nodir, "burned.jsonl"),
        grade_results_path=os.path.join(nodir, "grade_results.jsonl"),
        reports_glob=os.path.join(nodir, "selfimprove_report_*.json"),
    )
    assert st["summary"]["latest_pass_at_1"] is None
    assert st["summary"]["latest_ab"] is None
    assert st["summary"]["burned_total"] == 0
    assert st["summary"]["archive_count"] == 0
    assert st["summary"]["grade_results_count"] == 0
    assert st["ab_history"] == []
    assert st["pass1_trend"] == []
    assert st["burned_ledger"] == {"total": 0, "by_reason": {}, "recent": []}
    assert st["archive"] == {"count": 0, "genomes": [], "qd_cells": 0}
    # render_text must still produce a clean ASCII scorecard, not crash
    text = D.render_text(st)
    assert text.isascii() and "no data" not in text.lower()  # empty state still renders a scorecard
    assert "latest pass@1 : n/a" in text
    print("ok test_all_sections_degrade_to_empty")


def test_render_text_handles_garbage():
    # render_text must never raise even on a non-dict / empty input.
    assert isinstance(D.render_text(None), str)
    assert isinstance(D.render_text({}), str)
    print("ok test_render_text_handles_garbage")


if __name__ == "__main__":
    test_aggregates_correctly()
    test_recent_capped_at_20()
    test_archive_genomes_capped_at_50()
    test_all_sections_degrade_to_empty()
    test_render_text_handles_garbage()
    print("ALL DASHBOARD TESTS PASSED")
