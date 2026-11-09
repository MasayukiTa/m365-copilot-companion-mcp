# -*- coding: utf-8 -*-
"""Load-aware routing, shadow only (relay/load_routing.py, setting `fanout_load_routing`).

The properties that matter, each one tested where it can fail:

* the decision rule, as a pure function (tight / loose margins, unknown is not cheap);
* off records nothing and reads nothing; shadow records one row per admission;
* the recorder cannot raise into the caller (unwritable log, junk arguments) and does not
  mutate what it is given;
* the log is bounded;
* THE REAL ADMISSION IS UNCHANGED: relay_fleet.run_relay_fleet is driven end to end with the mode
  off, with shadow on, and with shadow on and an unwritable log, and the order goals are
  admitted in, what each worker was built with (goal, envelope) and the results are
  byte-identical in all three, even though the shadow choice differs from the real one.
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import load_routing as LR  # noqa: E402
from relay import test_admission as TA  # noqa: E402  (its fake worker / route harness)
import relay.relay_fleet as rf  # noqa: E402
from relay.relay_fleet import RelayWorker, run_relay_fleet  # noqa: E402
from scripts import capability_profile as CP  # noqa: E402

PROFILES = {
    "producer": {"turns": {"mean": 20.0, "n": 9, "label": "poor"},
                 "success": {"rate": 0.9, "n": 9, "label": "good"}, "confidence": "low"},
    "subtask": {"turns": {"mean": 3.0, "n": 9, "label": "good"},
                "success": {"rate": 1.0, "n": 9, "label": "good"}, "confidence": "low"},
    "aggregator": {"turns": {"mean": None, "n": 0, "label": "unknown"},
                   "success": {"rate": 1.0, "n": 2, "label": "unknown"}, "confidence": "unknown"},
}


class _W:
    def __init__(self, name, role="producer"):
        self.name = name
        self.goal = "secret goal text of " + name
        self.task_envelope = type("E", (), {"role": role})()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    LR._reset_for_test()
    monkeypatch.setattr(LR, "LOG", str(tmp_path / "fleet" / "load_routing_shadow.jsonl"))
    monkeypatch.setattr(LR, "profiles", lambda now=None: PROFILES)
    yield
    LR._reset_for_test()


def _settings(tmp_path, monkeypatch, text):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    LR._reset_for_test()
    return str(p)


def _rows():
    try:
        with io.open(LR.LOG, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        return []


# ------------------------------------------------------------------ the setting

@pytest.mark.parametrize("text,want", [
    ("", "off"), ("fanout_load_routing=shadow\n", "shadow"), ("fanout_load_routing=SHADOW\n", "shadow"),
    ("fanout_load_routing=off\n", "off"), ("fanout_load_routing=on\n", "on"),
    ("fanout_load_routing=ON\n", "on"), ("fanout_load_routing=route\n", "off"),
    ("fanout_load_routing=\n", "off"), ("﻿fanout_load_routing=shadow\n", "shadow")])
def test_the_mode_reads_off_shadow_or_on_and_nothing_else(tmp_path, text, want):
    p = tmp_path / "s.txt"
    p.write_text(text, encoding="utf-8")
    LR._reset_for_test()
    assert LR.mode(str(p)) == want


def test_a_missing_settings_file_is_off(tmp_path):
    assert LR.mode(str(tmp_path / "nope.txt")) == "off"


def test_the_mode_follows_the_file_without_a_restart(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text("fanout_load_routing=shadow\n", encoding="utf-8")
    assert LR.mode(str(p)) == "shadow"
    p.write_text("fanout_load_routing=off\nfiller=1\n", encoding="utf-8")
    assert LR.mode(str(p)) == "off"


# ------------------------------------------------------------------ the rule

def _c(*roles):
    return [{"name": "w%d" % i, "role": r} for i, r in enumerate(roles)]


def test_a_single_candidate_has_no_choice():
    v = LR.choose(_c("producer"), 3, True, PROFILES)
    assert v["index"] == 0 and not v["comparable"] and v["reason"] == "single candidate"


def test_tight_margins_prefer_the_cheapest_known_goal():
    v = LR.choose(_c("producer", "subtask"), 1, True, PROFILES)
    assert v["index"] == 1 and v["tight"] and v["comparable"]


def test_loose_margins_prefer_the_most_expensive_known_goal():
    v = LR.choose(_c("subtask", "producer"), 3, True, PROFILES)
    assert v["index"] == 1 and not v["tight"]


def test_a_refused_rate_gate_counts_as_tight_whatever_the_slots():
    assert LR.choose(_c("producer", "subtask"), 5, False, PROFILES)["tight"] is True


def test_a_tie_keeps_queue_order():
    assert LR.choose(_c("producer", "producer", "producer"), 1, True, PROFILES)["index"] == 0
    # the admitted goal already is the most expensive one, loose margins
    assert LR.choose(_c("producer", "subtask"), 4, True, PROFILES)["index"] == 0


def test_an_unknown_cost_is_never_treated_as_cheap_or_dear():
    # admitted goal unknown: nothing to compare against, same goal
    v = LR.choose(_c("aggregator", "subtask", "producer"), 1, True, PROFILES)
    assert v["index"] == 0 and not v["comparable"] and "unknown" in v["reason"]
    # an unknown goal in the queue is not picked even under tight margins
    v = LR.choose(_c("producer", "aggregator", "subtask"), 1, True, PROFILES)
    assert v["index"] == 2
    # a role that is not in the profile at all is unknown too
    assert LR.choose(_c("mystery", "subtask"), 1, True, PROFILES)["index"] == 0


def test_a_profile_labelled_unknown_gives_no_cost():
    prof = {"producer": {"turns": {"mean": 4.0, "n": 2, "label": "unknown"}, "confidence": "unknown"}}
    assert LR.expected("producer", prof)["turns"] is None
    assert LR.expected("producer", {})["turns"] is None


# ------------------------------------------------------------------ the recorder

def test_off_records_nothing_and_creates_nothing(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "fanout_load_routing=off\n")
    calls = []
    LR.shadow_assign(_W("w0"), [_W("w1", "subtask")], 0, 2, 2, run_id="r1",
                     probes={"rate_ok": lambda: calls.append(1) or (True, "")})
    assert not os.path.exists(os.path.dirname(LR.LOG)) and calls == []
    assert LR.status_block()["fanout_load_routing"] == {"mode": "off", "recorded": 0, "differed": 0}


def test_shadow_records_the_actual_next_to_the_shadow_choice(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")
    admitted, rest = _W("w0", "producer"), [_W("w1", "subtask"), _W("w2", "producer")]
    out = LR.shadow_assign(admitted, rest, 1, 2, 3, run_id="r9", ram_floor_mb=512,
                           probes={"rate_ok": lambda: (True, ""), "avail_mb": lambda: 4096.0})
    assert out is None
    (row,) = _rows()
    assert row["actual"] == {"name": "w0", "role": "producer"}
    assert row["shadow"] == {"name": "w1", "role": "subtask"}      # slots 1 -> tight -> cheapest
    assert row["differs"] is True and row["comparable"] is True and row["tight"] is True
    assert row["run_id"] == "r9" and row["queue_len"] == 3 and row["mode"] == "shadow"
    assert row["headroom"] == {"active": 1, "cap": 2, "lane_ceiling": 3, "slots": 1,
                               "rate_ok": True, "rate_why": "", "avail_mb": 4096.0,
                               "ram_floor_mb": 512}
    assert [c["name"] for c in row["candidates"]] == ["w0", "w1", "w2"]
    assert row["candidates"][1]["turns"] == 3.0
    assert "secret goal text" not in json.dumps(row)               # names and numbers only
    assert LR.status_block()["fanout_load_routing"] == {"mode": "shadow", "recorded": 1, "differed": 1}


def test_the_rate_gate_probe_tuple_is_read(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")
    LR.shadow_assign(_W("w0"), [_W("w1", "subtask")], 0, 5, 5,
                     probes={"rate_ok": lambda: (False, "rate 90/90 per minute")})
    (row,) = _rows()
    assert row["tight"] is True and row["headroom"]["rate_why"].startswith("rate 90/90")


def test_a_goal_admitted_again_after_a_failed_attach_is_one_row(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")
    w = _W("w0")
    for _ in range(3):
        LR.shadow_assign(w, [], 0, 2, 2, run_id="r1")
    assert len(_rows()) == 1
    LR.shadow_assign(_W("w1"), [], 0, 2, 2, run_id="r1")
    assert len(_rows()) == 2


def test_the_recorder_does_not_mutate_what_it_is_given(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")
    admitted, rest = _W("w0"), [_W("w1", "subtask"), _W("w2")]
    before = (list(rest), dict(vars(admitted)), [dict(vars(w)) for w in rest])
    LR.shadow_assign(admitted, rest, 1, 2, 2)
    assert rest == before[0] and vars(admitted) == before[1]
    assert [vars(w) for w in rest] == before[2]


def test_a_write_error_never_reaches_the_caller(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(LR, "LOG", str(blocker / "sub" / "log.jsonl"))   # a directory under a file
    assert LR.shadow_assign(_W("w0"), [_W("w1", "subtask")], 0, 2, 2) is None
    assert LR._STATE["write_errors"] == 1 and LR._STATE["recorded"] == 0


@pytest.mark.parametrize("args", [
    (None, None, None, None, None), (object(), 5, "x", [], {}), (_W("w0"), None, 1, 2, 3)])
def test_junk_arguments_never_raise(tmp_path, monkeypatch, args):
    _settings(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")
    assert LR.shadow_assign(*args) is None


def test_a_failing_probe_does_not_stop_the_record(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")

    def boom():
        raise RuntimeError("probe")

    LR.shadow_assign(_W("w0"), [_W("w1")], 0, 2, 2, probes={"rate_ok": boom, "avail_mb": boom})
    (row,) = _rows()
    assert row["headroom"]["rate_ok"] is None and row["headroom"]["avail_mb"] is None


def test_the_log_is_bounded_and_rotated(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")
    monkeypatch.setattr(LR, "MAX_BYTES", 4096)
    for i in range(400):
        LR.shadow_assign(_W("w%d" % i), [_W("x%d" % i, "subtask")], 0, 2, 2, run_id="r")
    live, old = os.path.getsize(LR.LOG), os.path.getsize(LR.LOG + ".1")
    assert os.path.exists(LR.LOG + ".1")
    assert live < 4096 + 4096 and old < 4096 + 4096     # one row past the limit at most
    assert LR._STATE["recorded"] == 400


def test_a_row_lists_at_most_the_first_candidates(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")
    LR.shadow_assign(_W("w0"), [_W("q%d" % i) for i in range(60)], 0, 2, 2)
    (row,) = _rows()
    assert row["queue_len"] == 61 and len(row["candidates"]) == LR.MAX_CANDIDATES_LOGGED


# ------------------------------------------------------------------ the profile source

def test_the_profile_is_derived_from_history_without_reading_tool_events(tmp_path):
    fleet = tmp_path / "fleet2"
    fleet.mkdir()
    hist = [{"outcome": "DONE", "turn": 10, "run_id": "r%d" % i, "name": "w0", "jid": "j%d" % i}
            for i in range(6)]
    (fleet / "history.json").write_text(json.dumps(hist), encoding="utf-8")
    (fleet / "tool_events.jsonl").write_text("this is not read\n", encoding="utf-8")
    prof = CP.load_profiles(str(fleet))
    assert prof["producer"]["turns"]["mean"] == 10.0 and prof["producer"]["turns"]["label"] == "good"
    assert CP.load_profiles(str(tmp_path / "nothing-here")) == {}


def test_the_profile_is_cached_and_follows_the_log_directory(tmp_path, monkeypatch):
    monkeypatch.undo()                      # use the real profiles() against a tmp fleet dir
    LR._reset_for_test()
    fleet = tmp_path / "f3"
    fleet.mkdir()
    monkeypatch.setattr(LR, "LOG", str(fleet / "load_routing_shadow.jsonl"))
    seen = []
    real = CP.load_profiles
    monkeypatch.setattr(CP, "load_profiles", lambda d, *a, **k: seen.append(d) or real(d, *a, **k))
    LR.profiles(now=1000.0)
    LR.profiles(now=1000.0 + LR.PROFILE_TTL_S - 1)
    assert seen == [str(fleet)]
    LR.profiles(now=1000.0 + LR.PROFILE_TTL_S + 1)
    assert len(seen) == 2


# ------------------------------------------------------------------ the real admission is unchanged

def _built_with(w):
    env = getattr(w, "task_envelope", None)
    return {"name": w.name, "goal": getattr(w, "goal", None),
            "original_goal_hash": getattr(w, "original_goal_hash", None),
            "max_turns": getattr(w, "max_turns", None),
            "envelope": (dict(dataclasses.asdict(env), goal_hash=env.goal_hash)
                         if env is not None else None)}


def _drive(tmp_path, monkeypatch, settings_text, log_path=None):
    """One end-to-end fleet run on the fake-worker harness. Returns what the coordinator did."""
    _settings(tmp_path, monkeypatch, settings_text)
    if log_path is not None:
        monkeypatch.setattr(LR, "LOG", log_path)
    LR._reset_for_test()
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
    # w0 is the cheap role and the rest are expensive ones: under loose margins the router would
    # have admitted w1 first, while the coordinator admits w0.
    goals = [{"text": "g0", "role": "subtask"}] + [{"text": "g%d" % i, "role": "producer"}
                                                    for i in range(1, 5)]

    def on_tick(workers):
        for w in workers:
            if getattr(w, "page", None) is not None and w.status == "waiting":
                state["control"][w.name] = "done"
                break

    try:
        res = run_relay_fleet(TA.FakeContext(), goals, "http://agent", max_concurrent=2, poll_s=0,
                              on_tick=on_tick, notify=lambda *a, **k: None, run_id="rtest")
    finally:
        TA._restore_worker(orig)
    return {"admitted": admitted, "built": built,
            "results": [{"name": r.get("name"), "outcome": r.get("outcome"),
                         "goal": r.get("goal")} for r in res]}


def test_the_real_assignment_and_prompts_are_byte_identical_in_off_and_shadow(tmp_path, monkeypatch):
    off = _drive(tmp_path, monkeypatch, "fanout_load_routing=off\n")
    assert not os.path.exists(os.path.dirname(LR.LOG))              # off wrote nothing
    assert len(off["admitted"]) == 5 and all(r["outcome"] == "DONE" for r in off["results"])

    shadow = _drive(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")
    rows = _rows()
    assert len(rows) == 5                                           # one row per admission
    assert rows[0]["actual"]["name"] == "w0" and rows[0]["shadow"]["name"] == "w1"
    assert rows[0]["differs"] is True                               # the shadow DID disagree ...

    blocked = tmp_path / "afile"
    blocked.write_text("x", encoding="utf-8")
    broken = _drive(tmp_path, monkeypatch, "fanout_load_routing=shadow\n",
                    log_path=str(blocked / "sub" / "log.jsonl"))
    assert LR._STATE["write_errors"] >= 1                           # ... and a failed write is survived

    dump = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)  # noqa: E731
    assert dump(shadow) == dump(off)                                # ... and changed nothing
    assert dump(broken) == dump(off)
    assert off["admitted"] == ["w0", "w1", "w2", "w3", "w4"]        # queue order, as before


def test_on_returns_the_index_in_on_and_none_in_shadow(tmp_path, monkeypatch):
    # same inputs, only the mode differs: shadow records and returns None; on records AND returns
    # the 1-based position in `remaining` of the goal it would admit instead (loose margins -> the
    # expensive producer w1, index 1).
    admitted, rest = _W("w0", "subtask"), [_W("w1", "producer"), _W("w2", "subtask")]
    _settings(tmp_path, monkeypatch, "fanout_load_routing=shadow\n")
    assert LR.shadow_assign(admitted, rest, 0, 2, 5, run_id="r") is None
    _settings(tmp_path, monkeypatch, "fanout_load_routing=on\n")
    out = LR.shadow_assign(admitted, rest, 0, 2, 5, run_id="r")
    assert out == 1                                                 # admit remaining[0] (w1) instead
    row = _rows()[-1]                                              # on writes the same shape of row
    assert row["mode"] == "on" and row["actual"]["name"] == "w0" and row["shadow"]["name"] == "w1"
    assert row["differs"] is True
    # shadow wrote one row before the mode switch; on appended a second. on's own counters stand alone.
    assert LR.status_block()["fanout_load_routing"] == {"mode": "on", "recorded": 1, "differed": 1}


def test_on_when_the_router_agrees_returns_none(tmp_path, monkeypatch):
    # tight margins: the cheapest goal is already at the head, so the router agrees -> no swap.
    _settings(tmp_path, monkeypatch, "fanout_load_routing=on\n")
    out = LR.shadow_assign(_W("w0", "subtask"), [_W("w1", "producer")], 1, 2, 2,
                           probes={"rate_ok": lambda: (True, "")})
    assert out is None                                             # agrees -> coordinator's goal kept
    (row,) = _rows()
    assert row["mode"] == "on" and row["differs"] is False


def test_on_actually_reorders_admission_while_shadow_and_off_do_not(tmp_path, monkeypatch):
    # off and shadow admit in queue order w0..w4 (proven above). on admits the router's choice
    # first: under loose margins it prefers the expensive producer w1 over the cheap w0, so w1 is
    # admitted before w0. All five still run to DONE, the recorder still writes one row per
    # admission, and no goal is dropped or duplicated.
    on = _drive(tmp_path, monkeypatch, "fanout_load_routing=on\n")
    assert len(on["admitted"]) == 5
    assert sorted(on["admitted"]) == ["w0", "w1", "w2", "w3", "w4"]      # every goal, once
    assert on["admitted"][0] == "w1"                                     # the router's choice went first
    assert on["admitted"] != ["w0", "w1", "w2", "w3", "w4"]              # order genuinely changed
    assert all(r["outcome"] == "DONE" for r in on["results"])
    rows = _rows()
    # one row per admission that had something to compare against; reordering means the final
    # admission(s) can face an empty queue, so it is 4-5 rows, every one in on mode.
    assert 4 <= len(rows) <= 5 and all(r["mode"] == "on" for r in rows)
    # the goal each worker was built with is unchanged (on reorders WHICH goal is admitted, it does
    # not edit any prompt): the built goal for w1 is still g1, for w0 still g0.
    assert on["built"]["w1"]["goal"] == "g1" and on["built"]["w0"]["goal"] == "g0"


def test_the_admission_loop_calls_the_recorder_after_the_choice_and_before_attach():
    with io.open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("load_routing.shadow_assign(") == 1
    i = src.index("load_routing.shadow_assign(")
    before, after = src[:i], src[i:]
    gate = "if w.status in TERMINAL:   # (shouldn't happen, but be safe)"
    assert gate in before and "w.attach(" not in before[before.rindex(gate):]   # after the choice ...
    assert after.index("ok = w.attach(context, agent_url)") < 1400              # ... right before attach
    # The result is bound and used ONLY behind the `on` guard that re-validates the index; off and
    # shadow return None, so the swap block is skipped and admission is unchanged for them.
    line_start = before.rfind("\n") + 1
    assert src[line_start:i].strip() == "_lr_pick ="           # bound to a name, used only under the guard
    assert "if isinstance(_lr_pick, int) and 1 <= _lr_pick <= len(pending):" in after
    assert "pending.insert(0, w)" in after and "w = _chosen" in after
