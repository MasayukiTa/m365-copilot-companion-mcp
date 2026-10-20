# -*- coding: utf-8 -*-
"""Role and protocol version record (relay/org, setting `org_version_record`, slices D1 and D2).

* the definitions are read from where they live, hashed by content, and a wording change moves
  only the hash of what it touched;
* off reads one line and writes nothing; on writes two small lines, once per run, no goal text;
* the writer cannot raise into the caller, is bounded, and an unreadable part is never hashed
  as if it were absent;
* the capability report groups by version only when the record exists, and is unchanged without it;
* NOTHING THE FLEET SENDS OR ADMITS CHANGES: the prompts the roles produce, and a whole
  run_relay_fleet run, are byte-identical with the record off and on.
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import test_admission as TA  # noqa: E402  (its fake worker / route harness)
import relay.relay_fleet as rf  # noqa: E402
from relay.org import definitions as D  # noqa: E402
from relay.org import versions as V  # noqa: E402
from relay.relay_fleet import RelayWorker, run_relay_fleet  # noqa: E402
from scripts import capability_profile as CP  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated():
    V._reset_for_test()
    yield
    V._reset_for_test()


def _settings(tmp_path, monkeypatch, text):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    return str(p)


def _lines(path):
    with io.open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


# ------------------------------------------------------------------ the definitions

def test_the_roles_and_protocols_in_use_are_all_readable():
    s = D.snapshot()
    assert sorted(s["roles"]) == ["aggregator", "planner", "refuter", "splitter"]
    assert sorted(s["workflows"]) == ["follow_up", "worker_protocol"]
    for section in (s["roles"], s["workflows"]):
        for name, e in section.items():
            assert re.fullmatch(r"[0-9a-f]{16}", e["hash"]), name
            assert e["version"] == "v1" and "missing" not in e
    assert s["roles_hash"] and s["workflows_hash"]
    assert D.snapshot() == s                                   # pure: same source, same answer


def test_a_wording_change_moves_only_the_hash_of_what_it_touched(monkeypatch):
    import relay.planner as planner
    before = D.snapshot()
    monkeypatch.setattr(planner, "PLAN_PROMPT", planner.PLAN_PROMPT + " (edited)")
    after = D.snapshot()
    assert after["roles"]["planner"]["hash"] != before["roles"]["planner"]["hash"]
    assert after["roles"]["planner"]["version"] == before["roles"]["planner"]["version"]
    for name in ("splitter", "refuter", "aggregator"):
        assert after["roles"][name] == before["roles"][name]
    assert after["roles_hash"] != before["roles_hash"]
    assert after["workflows"] == before["workflows"]
    assert after["workflows_hash"] == before["workflows_hash"]


def test_the_worker_protocol_hash_follows_the_protocol_text(monkeypatch):
    import relay.copilot_autopilot_relay as car
    before = D.snapshot()
    monkeypatch.setattr(car, "PROTOCOL", car.PROTOCOL + "x")
    after = D.snapshot()
    assert after["workflows"]["worker_protocol"]["hash"] != before["workflows"]["worker_protocol"]["hash"]
    assert after["roles_hash"] == before["roles_hash"]


def test_line_endings_do_not_change_a_hash():
    lf = D.hash_definition([lambda: "a\nb"])
    crlf = D.hash_definition([lambda: "a\r\nb"])
    assert lf == crlf and lf["hash"]


def test_an_unreadable_part_is_named_and_never_hashed_as_absent(monkeypatch):
    def boom():
        raise RuntimeError("renamed")
    boom.label = "relay.nowhere.GONE"
    monkeypatch.setitem(D.ROLES, "planner", ("v1", (boom,)))
    s = D.snapshot()
    assert s["roles"]["planner"]["hash"] == ""
    assert s["roles"]["planner"]["missing"] == ["relay.nowhere.GONE"]
    assert s["roles_hash"] == ""                              # the set is unknown, not "equal"
    assert s["workflows_hash"] != ""


# ------------------------------------------------------------------ the setting and the writer

@pytest.mark.parametrize("text,want", [
    ("", "off"), ("org_version_record=on\n", "on"), ("org_version_record=ON\n", "on"),
    ("org_version_record=off\n", "off"), ("org_version_record=yes\n", "off"),
    ("org_version_record=\n", "off"), ("﻿org_version_record=on\n", "on"),
    ("other=1\norg_version_record=on\n", "on")])
def test_the_mode_reads_on_off_and_nothing_else(tmp_path, text, want):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    assert V.mode(str(p)) == want


def test_a_missing_settings_file_is_off(tmp_path):
    assert V.mode(str(tmp_path / "nope.txt")) == "off"


def test_off_writes_nothing_and_creates_nothing(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "org_version_record=off\n")
    tdir = tmp_path / "fleet" / "transcripts"
    assert V.record_run("r1_a0", str(tdir)) is False
    assert not (tmp_path / "fleet").exists()


def test_on_writes_definitions_once_and_a_run_line_per_run(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "org_version_record=on\n")
    tdir = str(tmp_path / "fleet" / "transcripts")
    assert V.record_run("r1_a0", tdir, now=10.0) is True
    assert V.record_run("r2_a0", tdir, now=11.0) is True
    rows = _lines(str(tmp_path / "fleet" / V.FILENAME))
    assert [r["kind"] for r in rows] == ["defs", "run", "run"]
    s = D.snapshot()
    assert rows[0]["roles_hash"] == s["roles_hash"] and rows[0]["workflows_hash"] == s["workflows_hash"]
    assert rows[0]["roles"] == s["roles"]
    assert [r["run_id"] for r in rows[1:]] == ["r1_a0", "r2_a0"]
    assert set(rows[1]) == {"kind", "ts", "run_id", "roles_hash", "workflows_hash"}


def test_no_transcript_dir_means_no_record(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "org_version_record=on\n")
    assert V.record_run("r1_a0", None) is False and V.record_run("r1_a0", "") is False


def test_the_file_is_bounded_and_the_overflow_is_counted(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "org_version_record=on\n")
    tdir = str(tmp_path / "fleet" / "transcripts")
    assert V.record_run("r1_a0", tdir) is True
    path = str(tmp_path / "fleet" / V.FILENAME)
    size = os.path.getsize(path)
    monkeypatch.setattr(V, "MAX_BYTES", size + 10)
    assert V.record_run("r2_a0", tdir) is False
    assert os.path.getsize(path) == size and V._STATE["skipped_full"] == 1


def test_a_write_error_never_reaches_the_caller(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "org_version_record=on\n")
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    assert V.record_run("r1_a0", str(blocker / "sub" / "transcripts")) is False
    assert V._STATE["write_errors"] >= 1


def test_the_reader_skips_damage_and_unknown_kinds(tmp_path):
    p = tmp_path / V.FILENAME
    p.write_text("\n".join([
        json.dumps({"kind": "defs", "roles_hash": "a", "workflows_hash": "b",
                    "roles": {"planner": {"version": "v1", "hash": "h"}}, "workflows": {}}),
        "not json", "[1, 2]",
        json.dumps({"kind": "run", "run_id": "r1_a0", "roles_hash": "a", "workflows_hash": "b"}),
        json.dumps({"kind": "future", "x": 1}),
        json.dumps({"kind": "run", "run_id": "r1_a0", "roles_hash": "c", "workflows_hash": "d"}),
    ]) + "\n", encoding="utf-8")
    runs, defs, bad = V.load(str(p))
    assert runs == {"r1_a0": ("c", "d")} and bad == 2           # the last line for a run wins
    assert defs[("a", "b")]["roles"]["planner"]["hash"] == "h"
    assert V.load(str(tmp_path / "missing.jsonl")) == ({}, {}, 0)
    assert V.run_base("r6aa_a2") == "r6aa" and V.run_base("r6aa") == "r6aa"


def test_nothing_in_the_record_is_goal_text(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "org_version_record=on\n")
    V.record_run("r1_a0", str(tmp_path / "fleet" / "transcripts"))
    raw = (tmp_path / "fleet" / V.FILENAME).read_text(encoding="utf-8")
    import relay.planner as planner
    assert planner.PLAN_PROMPT[:30] not in raw                  # hashes, never the text


# ------------------------------------------------------------------ the report

def _w(role, outcome, turn, run_id):
    return {"name": "w0", "role": role, "outcome": outcome, "turn": turn, "run_id": run_id}


def test_the_report_is_unchanged_without_the_record_and_split_with_it():
    ws = [_w("producer", "DONE", 4, "r1_a0"), _w("producer", "STUCK", 8, "r2_a0"),
          _w("producer", "DONE", 6, "r3_a0")]
    plain = CP.derive([], ws, [])
    assert "by_org_version" not in plain["profiles"]["producer"] and "org_definitions" not in plain
    assert CP.derive([], ws, [], org_by_run={}, org_defs={}) == plain

    pair_a, pair_b = ("a" * 16, "b" * 16), ("c" * 16, "b" * 16)
    defs = {pair_a: {"roles": {}, "workflows": {}}, pair_b: {"roles": {}, "workflows": {}}}
    rep = CP.derive([], ws, [], org_by_run={"r1": pair_a, "r2": pair_b, "r3": pair_a}, org_defs=defs)
    by = rep["profiles"]["producer"]["by_org_version"]
    la, lb = CP.org_label(pair_a), CP.org_label(pair_b)
    assert by[la] == {"n": 2, "success_rate": 1.0, "mean_turns": 5.0}
    assert by[lb] == {"n": 1, "success_rate": 0.0, "mean_turns": 8.0}
    assert sorted(rep["org_definitions"]) == sorted([la, lb])
    assert rep["profiles"]["producer"]["n_runs"] == plain["profiles"]["producer"]["n_runs"]
    assert "org roles:" in CP.render_text(rep)


def test_a_run_with_no_record_is_counted_as_not_recorded():
    ws = [_w("producer", "DONE", 4, "r1_a0"), _w("producer", "DONE", 4, "r9_a0")]
    pair = ("a" * 16, "b" * 16)
    rep = CP.derive([], ws, [], org_by_run={"r1": pair}, org_defs={pair: {}})
    by = rep["profiles"]["producer"]["by_org_version"]
    assert by[CP.NO_ORG]["n"] == 1 and by[CP.org_label(pair)]["n"] == 1


def test_load_org_reads_the_file_the_writer_made(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "org_version_record=on\n")
    V.record_run("r5_a1", str(tmp_path / "fleet" / "transcripts"))
    runs, defs, bad = CP.load_org(str(tmp_path / "fleet"))
    s = D.snapshot()
    assert runs == {"r5": (s["roles_hash"], s["workflows_hash"])} and bad == 0
    assert (s["roles_hash"], s["workflows_hash"]) in defs
    assert CP.load_org(str(tmp_path / "nowhere")) == ({}, {}, 0)


# ------------------------------------------------------------------ nothing the fleet sends changes

def _prompts():
    import relay.copilot_autopilot_relay as car
    import relay.fanout as fo
    import relay.planner as planner
    from relay.relay_fleet import SKILL_VERSIONS  # noqa: F401
    res = [{"subtask_index": 1, "outcome": "DONE", "result": "r"},
           {"subtask_index": 2, "outcome": "MISSING", "result": ""}]
    return {"opening": planner.opening_turn("goal G", car.PROTOCOL),
            "plan": planner.PLAN_PROMPT, "approve": planner.APPROVE_JOB,
            "split": fo.SPLIT_JOB, "midrun": fo.midrun_split_job(3),
            "merge": fo.aggregation_prompt("goal G", res), "protocol": car.PROTOCOL}


def test_the_prompts_the_roles_produce_are_byte_identical_with_the_record_on(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "org_version_record=off\n")
    off = _prompts()
    _settings(tmp_path, monkeypatch, "org_version_record=on\n")
    assert V.record_run("r1_a0", str(tmp_path / "fleet" / "transcripts")) is True
    assert json.dumps(_prompts(), sort_keys=True) == json.dumps(off, sort_keys=True)


def _built_with(w):
    env = getattr(w, "task_envelope", None)
    return {"name": w.name, "goal": getattr(w, "goal", None),
            "max_turns": getattr(w, "max_turns", None),
            "envelope": (dict(dataclasses.asdict(env), goal_hash=env.goal_hash)
                         if env is not None else None)}


def _drive(tmp_path, monkeypatch, settings_text):
    _settings(tmp_path, monkeypatch, settings_text)
    V._reset_for_test()
    monkeypatch.setattr(rf, "avail_phys_mb", lambda: 64000.0)
    monkeypatch.setattr(rf, "free_disk_gb", lambda path=None: 500.0)
    state = {"control": {}}
    orig = TA._install_fake_worker(state)
    admitted, built = [], {}
    inner = RelayWorker.attach

    def spy(self, context, agent_url):
        admitted.append(self.name)
        built[self.name] = _built_with(self)
        return inner(self, context, agent_url)

    RelayWorker.attach = spy
    goals = [{"text": "g0", "role": "subtask"}] + [{"text": "g%d" % i, "role": "producer"}
                                                    for i in range(1, 5)]

    def on_tick(workers):
        for w in workers:
            if getattr(w, "page", None) is not None and w.status == "waiting":
                state["control"][w.name] = "done"
                break

    try:
        res = run_relay_fleet(TA.FakeContext(), goals, "http://agent", max_concurrent=2, poll_s=0,
                              on_tick=on_tick, notify=lambda *a, **k: None, run_id="rtest_a0",
                              transcript_dir=str(tmp_path / "fleet" / "transcripts"))
    finally:
        TA._restore_worker(orig)
    return {"admitted": admitted, "built": built,
            "results": [{"name": r.get("name"), "outcome": r.get("outcome"),
                         "goal": r.get("goal")} for r in res]}


def test_a_whole_run_admits_and_builds_identically_with_the_record_off_and_on(tmp_path, monkeypatch):
    off = _drive(tmp_path, monkeypatch, "org_version_record=off\n")
    assert not (tmp_path / "fleet" / V.FILENAME).exists()       # off wrote nothing
    assert len(off["admitted"]) == 5 and all(r["outcome"] == "DONE" for r in off["results"])

    on = _drive(tmp_path, monkeypatch, "org_version_record=on\n")
    rows = _lines(str(tmp_path / "fleet" / V.FILENAME))
    assert [r["kind"] for r in rows] == ["defs", "run"] and rows[1]["run_id"] == "rtest_a0"

    dump = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)  # noqa: E731
    assert dump(on) == dump(off)


def test_production_code_calls_the_recorder_and_the_report_reads_it():
    with io.open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("org_versions.record_run(") == 1
    i = src.index("org_versions.record_run(")
    assert src[src.rfind("\n", 0, i) + 1:i].strip() == ""       # a bare statement: its value is unused
    with io.open(os.path.join(REPO, "scripts", "capability_profile.py"), encoding="utf-8") as fh:
        assert "load_org(fleet_dir)" in fh.read()


def test_the_module_fixes_no_path_to_the_operators_record():
    with io.open(os.path.join(REPO, "relay", "org", "versions.py"), encoding="utf-8") as fh:
        code = fh.read()
    assert '".fleet"' not in code and "'.fleet'" not in code
