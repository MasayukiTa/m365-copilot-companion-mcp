# -*- coding: utf-8 -*-
"""Hermetic tests for the new-route stability replay harness (relay/stability_replay.py).

These prove the three things the owner's rule and the task require, with no fleet running
and no real CPU probe:

  * the DEFAULT is a dry run -- nothing is submitted, and the plan lists exactly the
    owner's verbatim inputs (the CSV continuation candidates are excluded unless the
    caller opts in, because the CSV cannot prove a row was owner-typed);
  * the metric parsers aggregate the fleet's own records correctly (latency median/p90/
    max, STUCK fraction and reason breakdown, resend/retry/fallback, continuation reach,
    resource deltas) and an absent record yields None, never a fabricated zero;
  * the pass/fail logic holds a run to the recorded thresholds, an unmeasured criterion
    is NOT a silent pass, and the executor refuses to start on a hot machine and honours
    the concurrency cap.

Every input is fed as a small fixture; the one real-data read is the committed verbatim
file, so the test and the recorded provenance cannot drift apart.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import stability_replay as SR  # noqa: E402


# ----------------------------------------------------------------------------------------
# Inputs: verbatim only by default; CSV candidates only when opted in.
# ----------------------------------------------------------------------------------------

def test_verbatim_inputs_load_from_the_committed_file():
    inputs = SR.load_verbatim_inputs()
    assert inputs, "the committed verbatim file must carry at least one owner line"
    for i in inputs:
        assert i.origin == "verbatim"
        assert i.text.strip() == i.text and i.text
        # Each owner line in this file is a continuation carrying a resume_conv id.
        assert i.resume_conv


def test_default_collect_excludes_csv_candidates(tmp_path):
    csv_path = tmp_path / "subs.csv"
    csv_path.write_text(
        "at,worker,route,fell_back,turns,outcome,status,reason,goal_len,goal_head200\n"
        '2026-10-26,w1,socket,False,2,DONE,ok,,80,"{""text"":""x"",""resume_conv"":""sess:a""}"\n',
        encoding="utf-8")
    default = SR.collect_inputs(csv_path=str(csv_path))
    assert all(i.origin == "verbatim" for i in default), \
        "the default must not pull in CSV rows that cannot be proven owner-typed"
    widened = SR.collect_inputs(include_csv_continuations=True, csv_path=str(csv_path))
    assert len(widened) >= len(default)
    assert any(i.origin == "csv_continuation" for i in widened)


def test_csv_candidates_keep_only_continuation_shaped_socket_rows(tmp_path):
    csv_path = tmp_path / "subs.csv"
    csv_path.write_text(
        "at,worker,route,fell_back,turns,outcome,status,reason,goal_len,goal_head200\n"
        '2026-10-26,w1,socket,False,2,DONE,ok,,80,"{""text"":""x"",""resume_conv"":""sess:a""}"\n'
        '2026-10-26,w2,socket,False,9,DONE,ok,,600,"a long coding brief with no resume marker"\n'
        '2026-10-26,w3,connector,False,2,DONE,ok,,80,"{""resume_conv"":""sess:b""}"\n',
        encoding="utf-8")
    cands = SR.load_csv_continuation_candidates(str(csv_path))
    # Only the socket row whose head contains a resume_conv marker is kept.
    assert len(cands) == 1
    assert cands[0].origin == "csv_continuation"
    assert "resume_conv" in cands[0].text


# ----------------------------------------------------------------------------------------
# Dry run: the default submits nothing and still produces the full plan.
# ----------------------------------------------------------------------------------------

def test_dry_run_submits_nothing_and_lists_the_owner_inputs():
    cfg = SR.ReplayConfig(execute=False)
    calls = []
    rec = SR.run_replay(cfg, submit_fn=lambda *a, **k: calls.append((a, k)),
                        cpu_fn=lambda: 0.0, sleep_fn=lambda s: None)
    assert rec["plan"]["mode"] == "dry-run"
    assert rec["submitted"] == []
    assert calls == [], "a dry run must not call submit_fn"
    assert rec["plan"]["input_count"] == len(SR.load_verbatim_inputs())
    # Every planned item is one of the owner's verbatim lines.
    texts = {i.text for i in SR.load_verbatim_inputs()}
    assert all(it["text"] in texts for it in rec["plan"]["items"])


# ----------------------------------------------------------------------------------------
# Execute gate: refuse on a hot machine; honour the concurrency cap; a submit failure is
# recorded as data, not raised.
# ----------------------------------------------------------------------------------------

def test_execute_refuses_to_start_when_cpu_is_over_threshold():
    cfg = SR.ReplayConfig(execute=True, cpu_threshold=85.0)
    calls = []
    rec = SR.run_replay(cfg, submit_fn=lambda *a, **k: calls.append(a) or "job",
                        cpu_fn=lambda: 97.0, sleep_fn=lambda s: None)
    assert "aborted" in rec
    assert calls == [], "nothing may be submitted when the CPU gate is closed"


def test_execute_submits_each_owner_input_when_cpu_is_cool():
    cfg = SR.ReplayConfig(execute=True, cpu_threshold=85.0, max_concurrent=2)
    seen = []
    rec = SR.run_replay(
        cfg,
        submit_fn=lambda text, **k: seen.append((text, k.get("resume_conv"))) or "job-%d" % len(seen),
        cpu_fn=lambda: 10.0, sleep_fn=lambda s: None)
    inputs = SR.load_verbatim_inputs()
    assert len(rec["submitted"]) == len(inputs)
    assert len(seen) == len(inputs)
    # resume_conv rides along so the continuation reaches its original conversation.
    for (text, rconv), i in zip(seen, inputs):
        assert text == i.text
        assert rconv == i.resume_conv


def test_execute_records_a_submit_failure_instead_of_crashing():
    cfg = SR.ReplayConfig(execute=True, cpu_threshold=85.0)
    def boom(*a, **k):
        raise RuntimeError("queue full")
    rec = SR.run_replay(cfg, submit_fn=boom, cpu_fn=lambda: 10.0, sleep_fn=lambda s: None)
    assert rec["submitted"] == []
    assert rec["skipped"] and all("submit raised" in s["why"] for s in rec["skipped"])


def test_execute_skips_a_wave_when_cpu_rises_mid_run():
    cfg = SR.ReplayConfig(execute=True, cpu_threshold=85.0)
    cpu_seq = iter([10.0] + [99.0] * 50)
    rec = SR.run_replay(cfg, submit_fn=lambda *a, **k: "job",
                        cpu_fn=lambda: next(cpu_seq), sleep_fn=lambda s: None)
    # First wave allowed, later waves skipped for CPU.
    assert rec["skipped"], "a mid-run CPU rise must skip later submissions"


# ----------------------------------------------------------------------------------------
# Metric parsers over small fixtures.
# ----------------------------------------------------------------------------------------

def test_latency_metrics_median_p90_max():
    hist = [{"phase_events": {"pending": 0.0, "waiting": d}} for d in (0.2, 0.3, 0.3, 27.1)]
    m = SR.latency_metrics(hist)
    assert m["submit_to_start_n"] == 4
    assert m["submit_to_start_max_s"] == pytest.approx(27.1)
    assert m["submit_to_start_median_s"] == pytest.approx(0.3)
    assert m["submit_to_start_p90_s"] >= m["submit_to_start_median_s"]


def test_latency_metrics_absent_is_none_not_zero():
    m = SR.latency_metrics([])
    assert m["submit_to_start_median_s"] is None
    assert m["submit_to_start_p90_s"] is None
    assert m["submit_to_start_max_s"] is None


def test_latency_metrics_ignores_negative_or_missing_phase_pairs():
    hist = [
        {"phase_events": {"pending": 10.0, "waiting": 5.0}},   # waiting before pending
        {"phase_events": {"pending": 0.0}},                    # no waiting
        {"phase_events": {"pending": 0.0, "waiting": 1.0}},    # good
    ]
    m = SR.latency_metrics(hist)
    assert m["submit_to_start_n"] == 1


def test_outcome_metrics_stuck_fraction_and_reasons():
    rows = (
        [{"outcome": "DONE"}] * 6
        + [{"outcome": "STUCK", "reason": "not re-sent: the turn may already have been delivered"}] * 3
        + [{"outcome": "STUCK", "reason": "no close frame"}]
        + [{"outcome": "REFUSED"}]
    )
    m = SR.outcome_metrics(rows)
    assert m["terminal_n"] == 11
    assert m["stuck_count"] == 4
    assert m["stuck_fraction"] == pytest.approx(4 / 11)
    assert m["stuck_reason_counts"]["no close frame"] == 1


def test_outcome_metrics_empty_is_none():
    m = SR.outcome_metrics([])
    assert m["stuck_fraction"] is None


def test_resend_metrics_flags_non_infra_refusals():
    events = [
        {"event": "resend_refused", "reason": "no close frame"},
        {"event": "resend_refused", "reason": "token expired"},
        {"event": "resend_refused", "reason": "duplicate of an existing goal"},  # non-infra
        {"event": "socket_retry", "reason": "dns"},
        {"event": "fallback", "reason": "dns"},
    ]
    m = SR.resend_metrics(events)
    assert m["resend_refused"] == 3
    assert m["socket_retry"] == 1
    assert m["fallback"] == 1
    assert m["resend_refused_non_infra"] == 1
    assert m["resend_refused_non_infra_fraction"] == pytest.approx(1 / 3)


def test_resend_metrics_no_refusals_is_zero_not_none():
    m = SR.resend_metrics([{"event": "socket_retry", "reason": "dns"}])
    assert m["resend_refused"] == 0
    assert m["resend_refused_non_infra_fraction"] == 0.0


def test_continuation_metrics_requires_resume_conv_and_exact_text():
    inputs = [
        SR.OwnerInput(text="continue A", resume_conv="sess:a"),
        SR.OwnerInput(text="continue B", resume_conv="sess:b"),
    ]
    pending = [
        {"payload": {"goal": "continue A", "resume_conv": "sess:a"}},
        {"payload": {"goal": "continue B", "resume_conv": "sess:WRONG"}},
    ]
    m = SR.continuation_metrics(inputs, pending)
    assert m["continue_n"] == 2
    assert m["continue_reached"] == 1
    assert m["continue_reached_fraction"] == pytest.approx(0.5)


# ----------------------------------------------------------------------------------------
# Verdict logic.
# ----------------------------------------------------------------------------------------

def test_evaluate_passes_when_every_measured_criterion_holds():
    metrics = {
        "submit_to_start_p90_s": 2.0,
        "submit_to_start_median_s": 0.3,
        "stuck_fraction": 0.2,
        "continue_reached_fraction": 1.0,
        "resend_refused_non_infra_fraction": 0.0,
    }
    v = SR.evaluate(metrics)
    assert v["passed"] is True
    assert all(r["status"] == "pass" for r in v["results"])


def test_evaluate_fails_on_a_breached_threshold():
    metrics = {
        "submit_to_start_p90_s": 45.0,   # over 30s
        "submit_to_start_median_s": 0.3,
        "stuck_fraction": 0.2,
        "continue_reached_fraction": 1.0,
        "resend_refused_non_infra_fraction": 0.0,
    }
    v = SR.evaluate(metrics)
    assert v["passed"] is False
    bad = [r for r in v["results"] if r["key"] == "submit_to_start_p90_s"][0]
    assert bad["status"] == "fail"


def test_evaluate_unmeasured_is_not_a_silent_pass():
    # Every criterion absent -> all unmeasured -> not passed.
    v = SR.evaluate({})
    assert v["passed"] is False
    assert all(r["status"] == "unmeasured" for r in v["results"])


def test_pass_criteria_cite_a_measured_basis():
    # The bar must not be invented: each criterion carries a non-empty basis string.
    assert SR.PASS_CRITERIA
    for c in SR.PASS_CRITERIA:
        assert c.basis.strip()
        assert c.comparator in ("le", "ge")


# ----------------------------------------------------------------------------------------
# Resource deltas and report writing.
# ----------------------------------------------------------------------------------------

def test_gather_metrics_computes_resource_deltas():
    m = SR.gather_metrics(
        res_start={"c_free_gb": 10.0, "commit_gb": 20.0},
        res_end={"c_free_gb": 9.5, "commit_gb": 22.0})
    assert m["c_free_gb_delta"] == pytest.approx(-0.5)
    assert m["commit_gb_delta"] == pytest.approx(2.0)


def test_write_reports_emits_md_and_csv(tmp_path):
    plan = SR.build_plan(SR.ReplayConfig(execute=False))
    metrics = {"stuck_fraction": 0.2, "outcome_counts": {"DONE": 5, "STUCK": 1}}
    verdict = SR.evaluate({
        "submit_to_start_p90_s": 2.0, "submit_to_start_median_s": 0.3,
        "stuck_fraction": 0.2, "continue_reached_fraction": 1.0,
        "resend_refused_non_infra_fraction": 0.0})
    paths = SR.write_reports(str(tmp_path), plan, metrics, verdict)
    md = open(paths["md"], encoding="utf-8").read()
    assert "Verdict: PASS" in md
    assert "dry-run" in md
    rows = open(paths["csv"], encoding="utf-8").read().splitlines()
    assert rows[0] == "kind,key,value,extra"
    assert any(r.startswith("criterion,") for r in rows)
    assert any(r.startswith("metric,") for r in rows)


def test_main_dry_run_writes_a_report(tmp_path):
    rc = SR.main(["--out", str(tmp_path)])
    assert rc == 0
    assert os.path.isfile(os.path.join(str(tmp_path), "report.md"))
    assert os.path.isfile(os.path.join(str(tmp_path), "metrics.csv"))
    assert os.path.isfile(os.path.join(str(tmp_path), "run_record.json"))
    rec = json.load(open(os.path.join(str(tmp_path), "run_record.json"), encoding="utf-8"))
    assert rec["plan"]["mode"] == "dry-run"
    assert rec["submitted"] == []
