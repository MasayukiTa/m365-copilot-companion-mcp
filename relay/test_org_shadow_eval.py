# -*- coding: utf-8 -*-
"""Shadow evaluation of role/protocol change proposals (relay/org/shadow_eval.py, setting
`org_shadow_eval`, slice D4).

* the mode reads shadow/off and nothing else (`on` is not a mode);
* off reads one line and writes nothing; shadow writes one hash-only line per run and per proposal
  that still applies, never goal text or the proposed text; a stale proposal is not recorded;
* the writer cannot raise into the caller and is bounded;
* the report counts the runs a proposal would have touched, shows their baseline outcome per
  (role, kind, org version), says shadow cannot show a proposal is better, and sizes a promotion
  decision;
* NOTHING THE FLEET SENDS OR ADMITS CHANGES: the prompts the roles produce, and a whole
  run_relay_fleet run, are byte-identical with the mode off and with shadow on.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import test_org_versions as TV  # noqa: E402  (its prompt set and whole-run driver)
from relay.org import definitions as D  # noqa: E402
from relay.org import proposals as P  # noqa: E402
from relay.org import shadow_eval as SE  # noqa: E402
from relay.org import versions as V  # noqa: E402
from scripts import org_shadow_eval_report as REP  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    SE._reset_for_test()
    V._reset_for_test()
    monkeypatch.setattr(SE, "LOG_PATH", str(tmp_path / "state" / SE.FILENAME))
    monkeypatch.setattr(P, "LOG_PATH", str(tmp_path / "state" / P.FILENAME))
    yield
    SE._reset_for_test()
    V._reset_for_test()


def _settings(tmp_path, monkeypatch, text):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    return str(p)


def _lines(path):
    with io.open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def _proposal(target="role:aggregator", base=None, proposed="b" * 16, **kw):
    rec = {"ts": 1.0, "target": target, "base_hash": base or P.targets()[target],
           "proposed_hash": proposed, "proposed_len": 10, "rationale": "the merge hides misses",
           "author": "w1", "campaign": "c1"}
    rec.update(kw)
    return rec


def _file_proposals(tmp_path, *recs):
    path = tmp_path / "state" / P.FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return str(path)


# ------------------------------------------------------------------ the setting

@pytest.mark.parametrize("text,want", [
    ("org_shadow_eval=shadow\n", "shadow"), ("org_shadow_eval=SHADOW\n", "shadow"),
    ("org_shadow_eval=off\n", "off"), ("org_shadow_eval=on\n", "off"),
    ("org_shadow_eval=maybe\n", "off"), ("fleet=on\n", "off"), ("", "off"),
    ("﻿org_shadow_eval=shadow\n", "shadow"),
])
def test_the_mode_reads_shadow_off_and_nothing_else(tmp_path, text, want):
    p = tmp_path / "s.txt"
    p.write_text(text, encoding="utf-8")
    assert SE.mode(str(p)) == want


def test_a_missing_settings_file_is_off(tmp_path):
    assert SE.mode(str(tmp_path / "nope.txt")) == "off"


def test_on_is_not_a_mode():
    assert SE.MODES == ("off", "shadow")


# ------------------------------------------------------------------ the record

def test_off_writes_nothing_and_creates_nothing(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=off\n")
    pp = _file_proposals(tmp_path, _proposal())
    assert SE.record_run("r1_a0", settings_path=s, proposals_path=pp) == 0
    assert not os.path.exists(SE.LOG_PATH)


def test_shadow_with_no_proposals_writes_nothing(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=shadow\n")
    assert SE.record_run("r1_a0", settings_path=s, proposals_path=str(tmp_path / "none")) == 0
    assert not os.path.exists(SE.LOG_PATH)


def test_shadow_writes_one_hash_only_line_per_run_and_live_proposal(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=shadow\n")
    live = _proposal("role:aggregator")
    live2 = _proposal("workflow:worker_protocol", proposed="c" * 16)
    stale = _proposal("role:planner", base="0" * 16)
    pp = _file_proposals(tmp_path, live, live2, stale)
    assert SE.record_run("r1_a0", settings_path=s, proposals_path=pp, now=10.0) == 2
    assert SE.record_run("r2_a0", settings_path=s, proposals_path=pp, now=11.0) == 2
    rows = _lines(SE.LOG_PATH)
    assert len(rows) == 4 and SE._STATE["stale"] == 2
    snap = D.snapshot()
    for r in rows:
        assert tuple(sorted(r)) == tuple(sorted(SE.FIELDS)) and r["kind"] == SE.KIND
        assert r["roles_hash"] == snap["roles_hash"] and r["workflows_hash"] == snap["workflows_hash"]
    assert {r["target"] for r in rows} == {"role:aggregator", "workflow:worker_protocol"}
    assert [r["run_id"] for r in rows] == ["r1_a0", "r1_a0", "r2_a0", "r2_a0"]
    assert rows[0]["proposal"] == SE.proposal_id("role:aggregator", live["base_hash"], "b" * 16)


def test_nothing_in_the_record_is_text(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=shadow\n")
    pp = _file_proposals(tmp_path, _proposal(rationale="SECRET-RATIONALE-TEXT"))
    SE.record_run("r1_a0", settings_path=s, proposals_path=pp)
    with io.open(SE.LOG_PATH, encoding="utf-8") as fh:
        raw = fh.read()
    assert "SECRET-RATIONALE-TEXT" not in raw and "rationale" not in raw


def test_one_run_records_at_most_max_per_run(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=shadow\n")
    monkeypatch.setattr(SE, "MAX_PER_RUN", 2)
    pp = _file_proposals(tmp_path, *[_proposal(proposed=("%016x" % i)) for i in range(1, 6)])
    assert SE.record_run("r1_a0", settings_path=s, proposals_path=pp) == 2


def test_the_file_is_bounded_and_the_overflow_is_counted(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=shadow\n")
    pp = _file_proposals(tmp_path, _proposal())
    assert SE.record_run("r1_a0", settings_path=s, proposals_path=pp) == 1
    size = os.path.getsize(SE.LOG_PATH)
    monkeypatch.setattr(SE, "MAX_BYTES", size + 10)
    assert SE.record_run("r2_a0", settings_path=s, proposals_path=pp) == 0
    assert os.path.getsize(SE.LOG_PATH) == size and SE._STATE["skipped_full"] == 1


def test_a_write_error_never_reaches_the_caller(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=shadow\n")
    pp = _file_proposals(tmp_path, _proposal())
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    assert SE.record_run("r1_a0", settings_path=s, proposals_path=pp,
                         path=str(blocker / "sub" / SE.FILENAME)) == 0
    assert SE._STATE["write_errors"] >= 1


def test_the_reader_skips_damage_and_unknown_kinds(tmp_path):
    path = tmp_path / "x.jsonl"
    good = {"ts": 5.0, "kind": SE.KIND, "run_id": "r1_a0", "proposal": "p1"}
    path.write_text("\n".join([
        json.dumps(good), "not json", "[1]", json.dumps({"kind": "future", "run_id": "r"}),
        json.dumps({"kind": SE.KIND, "ts": 1.0, "proposal": "p1"}),
        json.dumps(dict(good, run_id="r2_a0"))]) + "\n", encoding="utf-8")
    touches, bad = SE.load(str(path))
    assert touches == {"p1": {"r1_a0": 5.0, "r2_a0": 5.0}} and bad == 3
    assert SE.load(str(tmp_path / "nowhere")) == ({}, 0)


# ------------------------------------------------------------------ the report

def test_runs_needed_matches_the_formula_and_grows_toward_a_coin_flip():
    # p0 .80 -> p1 .95, pbar .875: 2 * (1.96 + 0.84)^2 * .875 * .125 / .15^2 = 76.2 -> 77
    assert REP.runs_needed(0.8, 0.15) == 77
    assert REP.runs_needed(0.5, 0.15) > REP.runs_needed(0.8, 0.15)
    assert REP.runs_needed(1.0, 0.15) == REP.runs_needed(0.95, 0.15)      # clamped
    assert REP.runs_needed(0.5, 0.05) > REP.runs_needed(0.5, 0.15)


def _fleet(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=shadow\n")
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    prop = _proposal("workflow:worker_protocol")
    pp = _file_proposals(tmp_path, prop)
    (fleet / P.FILENAME).write_text(open(pp, encoding="utf-8").read(), encoding="utf-8")
    day = 86400.0
    for i, t in enumerate((0.0, 3 * day, 6 * day)):
        SE.record_run("r%d_a0" % i, settings_path=s, proposals_path=pp, now=t,
                      path=str(fleet / SE.FILENAME))
    SE.record_run("r9_a0", settings_path=s, proposals_path=pp, now=7 * day,
                  path=str(fleet / SE.FILENAME))                       # no outcome recorded
    hist = [{"run_id": "r0_a0", "name": "w0", "outcome": "DONE", "turn": 4, "jid": "j0"},
            {"run_id": "r1_a0", "name": "w0", "outcome": "STUCK", "turn": 8, "jid": "j1"},
            {"run_id": "r2_a0", "name": "w0", "outcome": "DONE", "turn": 6, "jid": "j2"},
            {"run_id": "r5_a0", "name": "w0", "outcome": "DONE", "turn": 2, "jid": "j5"}]
    (fleet / "history.json").write_text(json.dumps(hist), encoding="utf-8")
    return str(fleet), prop


def test_the_report_counts_touched_runs_and_their_baseline(tmp_path, monkeypatch):
    fleet, prop = _fleet(tmp_path, monkeypatch)
    rep = REP.build(fleet, min_n=2)
    assert rep["count"] == 1 and "cannot show a proposal is better" in rep["note"]
    p = rep["proposals"][0]
    assert p["target"] == "workflow:worker_protocol" and p["stale"] is False
    assert (p["runs_would_touch"], p["runs_with_outcome"], p["runs_without_outcome"]) == (4, 3, 1)
    assert abs(p["touched_runs_per_day"] - 4 / 7.0) < 1e-9
    (cell,) = p["cells"]                      # r5 was never touched, so it is not counted
    assert cell["role"] == "producer" and cell["n"] == 3 and cell["enough_to_read"] is True
    assert abs(cell["success_rate"] - 2 / 3) < 1e-9 and cell["mean_turns"] == 6.0
    assert cell["org_version"] == "(not recorded)"
    need = REP.runs_needed(2 / 3, 0.15)
    assert cell["runs_needed_per_arm"] == need and cell["shortfall_baseline_runs"] == need - 3
    assert abs(cell["days_at_observed_rate"] - (need - 3) / (4 / 7.0)) < 1e-9


def test_the_report_marks_a_stale_proposal_and_text_says_what_it_cannot_say(tmp_path, monkeypatch):
    fleet, prop = _fleet(tmp_path, monkeypatch)
    stale = dict(prop, base_hash="0" * 16)
    with io.open(os.path.join(fleet, P.FILENAME), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(stale) + "\n")
    # a line whose base hash is not the definition's is still "valid" for the reader
    rep = REP.build(fleet)
    assert rep["proposals"][0]["stale"] is True and rep["proposals"][0]["runs_would_touch"] == 0
    text = REP.render_text(rep)
    assert "STALE" in text and "cannot show a proposal is better" in text


def test_the_report_runs_as_a_script_on_an_empty_directory(tmp_path, capsys):
    assert REP.main(["--fleet-dir", str(tmp_path), "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 0 and out["proposals"] == []


def test_the_report_attributes_the_aggregator_target_to_aggregator_records_only(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=shadow\n")
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    pp = _file_proposals(tmp_path, _proposal("role:aggregator"))
    (fleet / P.FILENAME).write_text(open(pp, encoding="utf-8").read(), encoding="utf-8")
    SE.record_run("r1_a0", settings_path=s, proposals_path=pp, path=str(fleet / SE.FILENAME))
    status = {"workers": [
        {"name": "w0", "role": "producer", "outcome": "DONE", "turn": 3, "run_id": "r1_a0"},
        {"name": "w1", "role": "aggregator", "outcome": "DONE", "turn": 2, "run_id": "r1_a0"}]}
    (fleet / "status.json").write_text(json.dumps(status), encoding="utf-8")
    cells = REP.build(str(fleet))["proposals"][0]["cells"]
    assert [c["role"] for c in cells] == ["aggregator"]


# ------------------------------------------------------------------ nothing the fleet sends changes

def test_the_prompts_the_roles_produce_are_byte_identical_with_shadow_on(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=off\n")
    off = TV._prompts()
    s = _settings(tmp_path, monkeypatch, "org_shadow_eval=shadow\n")
    pp = _file_proposals(tmp_path, _proposal())
    assert SE.record_run("r1_a0", settings_path=s, proposals_path=pp) == 1
    assert json.dumps(TV._prompts(), sort_keys=True) == json.dumps(off, sort_keys=True)


def test_a_whole_run_admits_and_builds_identically_with_the_mode_off_and_shadow(tmp_path, monkeypatch):
    _file_proposals(tmp_path, _proposal("workflow:worker_protocol"))   # at P.LOG_PATH
    off = TV._drive(tmp_path, monkeypatch, "org_shadow_eval=off\n")
    assert not os.path.exists(SE.LOG_PATH)                              # off wrote nothing
    assert len(off["admitted"]) == 5 and all(r["outcome"] == "DONE" for r in off["results"])

    on = TV._drive(tmp_path, monkeypatch, "org_shadow_eval=shadow\n")
    rows = _lines(SE.LOG_PATH)
    assert len(rows) == 1 and rows[0]["run_id"] == "rtest_a0" and rows[0]["kind"] == SE.KIND

    dump = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)  # noqa: E731
    assert dump(on) == dump(off)


def test_production_code_calls_the_recorder_and_a_script_reads_it():
    with io.open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("org_shadow_eval.record_run(") == 1
    i = src.index("org_shadow_eval.record_run(")
    assert src[src.rfind("\n", 0, i) + 1:i].strip() == ""       # a bare statement: value unused
    with io.open(os.path.join(REPO, "scripts", "org_shadow_eval_report.py"), encoding="utf-8") as fh:
        rsrc = fh.read()
    assert "shadow_eval.load(" in rsrc and "def main(" in rsrc and '__name__ == "__main__"' in rsrc
