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

        # top-level shape (now includes the general-user `usage` lens)
        assert set(st.keys()) == {"summary", "pending_decisions", "usage", "reliability", "ab_history", "pass1_trend",
                                  "burned_ledger", "archive", "branches"}

        # summary
        s = st["summary"]
        assert s["burned_total"] == 5
        assert s["archive_count"] == 3
        assert abs(s["latest_pass_at_1"] - 0.60) < 1e-9     # newest archive entry by insertion order
        assert s["latest_ab"] == {"net_pp": 8.75, "p": 0.001, "verdict": "keep", "keep": True}
        assert s["grade_results_count"] == 2
        # summary mirrors the general-user persona-leak rate (here no history -> None)
        assert "persona_leak_rate" in s
        assert s["persona_leak_rate"] == st["usage"].get("persona_leak_rate")

        # usage section is a dict carrying the persona-leak quality key
        u = st["usage"]
        assert isinstance(u, dict)
        assert "persona_leak_rate" in u
        assert "quality_scored" in u and "persona_flagged" in u

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
        assert "persona leak  : " in text                    # quality headline line is present (ASCII)

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
    assert st["archive"] == {"count": 0, "records": 0, "genomes": [], "qd_cells": 0}
    # usage section degrades to a valid dict (never raises) and still carries the persona keys.
    assert isinstance(st["usage"], dict)
    assert "persona_leak_rate" in st["usage"]
    # with these synthetic empty ledgers the usage lens reads the REAL repo history; whether that is
    # populated or not, the summary must mirror usage exactly and never raise.
    assert st["summary"]["persona_leak_rate"] == st["usage"].get("persona_leak_rate")
    # render_text must still produce a clean ASCII scorecard, not crash
    text = D.render_text(st)
    assert text.isascii() and "no data" not in text.lower()  # empty state still renders a scorecard
    assert "latest pass@1 : n/a" in text
    assert "persona leak  : " in text                        # quality headline line present and ASCII
    print("ok test_all_sections_degrade_to_empty")


def test_render_text_handles_garbage():
    # render_text must never raise even on a non-dict / empty input.
    assert isinstance(D.render_text(None), str)
    assert isinstance(D.render_text({}), str)
    print("ok test_render_text_handles_garbage")


def test_write_json_writes_valid_feed():
    # write_json must drop a valid, pretty JSON file carrying the 6 top-level keys, return its
    # path, create the parent dir if missing, and never raise.
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "sub", "selfimprove_dashboard.json")   # parent does not exist yet
        ret = D.write_json(out)
        assert ret == out
        assert os.path.isfile(out)
        with open(out, encoding="utf-8") as f:
            obj = json.load(f)                                       # must be valid JSON
        assert set(obj.keys()) == {"summary", "pending_decisions", "usage", "reliability", "ab_history", "pass1_trend",
                                   "burned_ledger", "archive", "branches"}
        # pretty-printed (indent=2) -> multi-line with leading spaces, not a single dense line
        raw = open(out, encoding="utf-8").read()
        assert "\n  " in raw
        # idempotent re-write over an existing file also succeeds
        ret2 = D.write_json(out)
        assert ret2 == out and os.path.isfile(out)
    print("ok test_write_json_writes_valid_feed")


def test_cli_json_mode_importable_and_callable():
    # The --json CLI path must be importable and callable, return 0, and never traceback.
    rc = D.main(["--json"])
    assert rc == 0
    rc_text = D.main([])          # bare mode (text scorecard) still works
    assert rc_text == 0
    print("ok test_cli_json_mode_importable_and_callable")


if __name__ == "__main__":
    test_aggregates_correctly()
    test_recent_capped_at_20()
    test_archive_genomes_capped_at_50()
    test_all_sections_degrade_to_empty()
    test_render_text_handles_garbage()
    test_write_json_writes_valid_feed()
    test_cli_json_mode_importable_and_callable()
    print("ALL DASHBOARD TESTS PASSED")


# ---- §21 は、人が数を読む場所で効く ---------------------------------------------------------

def test_a_report_that_does_not_say_reads_as_not_recorded_rather_than_as_a_verdict():
    """3状態であること。『報告が言っていない』は報告についての事実で、行動につながる。
    『NO』は主張についての判定。畳むと、この行は全ダッシュボードで常に赤になり、
    永久に赤い信号は読み飛ばされる訓練にしかならない。"""
    from relay.selfimprove.dashboard import render_text
    got = render_text({"summary": {"latest_ab": {"net_pp": 6.2, "p": 0.18,
                                                 "verdict": "suggestive", "keep": False}}})
    assert "claimable     : not recorded" in got
    assert "claimable     : NO" not in got


def test_a_gain_from_the_optimisation_pool_says_so():
    """最適化に使った当のデータから出た数は、適合の推定。"""
    from relay.selfimprove.dashboard import render_text
    got = render_text({"summary": {"pools": ["evolution"], "pool_reads": {"evolution": 1},
                                   "latest_ab": {"net_pp": 6.2, "keep": True}}})
    assert "claimable     : NO" in got and "optimiser's own feedback" in got


def test_a_gain_from_a_fresh_held_out_pool_is_claimable():
    from relay.selfimprove.dashboard import render_text
    got = render_text({"summary": {"pools": ["sealed"], "pool_reads": {"sealed": 1},
                                   "latest_ab": {"net_pp": 6.2, "keep": True}}})
    assert "claimable     : yes" in got


def test_a_burned_held_out_pool_stops_being_claimable():
    """3回覗いた sealed は、2回目の時点で最適化フィードバックに変わっている。"""
    from relay.selfimprove.dashboard import render_text
    got = render_text({"summary": {"pools": ["sealed"], "pool_reads": {"sealed": 3},
                                   "latest_ab": {"net_pp": 6.2, "keep": True}}})
    assert "claimable     : NO" in got and "second look" in got


def test_the_claimable_line_can_actually_go_green():
    """死んだ配線でないことの証明。`dashboard_state` が pools を載せない限り
    この行は永久に赤で、赤にしかならない検査は検査ではない。"""
    from relay.selfimprove.dashboard import render_text
    got = render_text({"summary": {"pools": ["sealed"], "pool_reads": {"sealed": 1},
                                   "latest_ab": {"net_pp": 6.2, "keep": True}}})
    assert "claimable     : yes" in got


def test_the_report_row_carries_the_pools_so_the_summary_can_read_them():
    """報告行に pools が無ければ、要約にも出ず、行は永久に『not recorded』のまま。"""
    import inspect

    from relay.selfimprove import dashboard as D
    src = inspect.getsource(D._ab_history)
    assert '"pools": rep.get("pools")' in src
    assert '"pool_reads": rep.get("pool_reads")' in src


def test_an_unknown_read_history_is_not_treated_as_never_read():
    """pools はあるが読み回数が無い状態。履歴不明を0回と読むのが fail-open。"""
    from relay.selfimprove.dashboard import render_text
    got = render_text({"summary": {"pools": ["sealed"],
                                   "latest_ab": {"net_pp": 6.2, "keep": True}}})
    assert "claimable     : NO" in got and "no reading history" in got


# ---- pass^k beside pass@1 -----------------------------------------------------------------

def test_the_scorecard_shows_both_metrics_and_names_what_each_asks():
    """pass@1 alone let a scaffold that solves a different 40% each run read the same as one
    that solves the same 40% every run."""
    from relay.selfimprove.dashboard import dashboard_state, render_text
    text = render_text(dashboard_state())
    assert "pass@1" in text and "pass^" in text
    assert "capability" in text and ("reliability" in text or "not measured" in text)


def test_an_unmeasured_reliability_says_so_rather_than_printing_a_number():
    """1.000 from a single run would invent the finding the metric was added to check."""
    from relay.selfimprove.dashboard import render_text
    state = {"summary": {"latest_pass_at_1": 0.5},
             "reliability": {"measured": False, "slices": [], "spread": [],
                             "why_not": "no repeated runs"}}
    text = render_text(state)
    assert "not measured" in text
    assert "pass^k : 1.000" not in text


def test_a_measured_reliability_reports_k_so_the_number_can_be_read():
    """pass^k without k is not interpretable: it falls as k rises by construction."""
    from relay.selfimprove.dashboard import render_text
    state = {"summary": {"latest_pass_at_1": 0.5},
             "reliability": {"measured": True, "spread": [],
                             "slices": [{"n": 50, "k": 3, "enough": True,
                                         "pass_hat_k": 0.24, "pass_any": 0.60,
                                         "flaky": 0.36, "per_run_pass_at_1": [.4, .5, .4]}]}}
    text = render_text(state)
    assert "pass^3" in text and "0.240" in text
    assert "0.360" in text          # the flakiness is shown, not only the floor


def test_a_replicate_does_not_supersede_the_row_it_repeats():
    """Two honest runs of one scaffold share an id. Read as a correction, k could never
    reach 2 and no reliability figure was computable from any number of runs."""
    from relay.selfimprove.dashboard import dashboard_state
    import json
    import tempfile
    import os
    d = tempfile.mkdtemp()
    p = os.path.join(d, "archive.jsonl")
    rows = [{"id": "g", "genome": {}, "slice_ids": ["a"], "pass_at_1": 0.3,
             "replicate": None, "ts": 1},
            {"id": "g", "genome": {}, "slice_ids": ["a"], "pass_at_1": 0.5,
             "replicate": 2, "ts": 2}]
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    st = dashboard_state(archive_path=p)
    assert [t["superseded"] for t in st["pass1_trend"]] == [False, False]
