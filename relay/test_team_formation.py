# -*- coding: utf-8 -*-
"""Dynamic team formation, shadow only (relay/team_formation.py, setting `team_mode`).

The properties that matter, each one tested where it can fail:

* the proposal rule, as a pure function (fold_tiny, review, unknown profile changes nothing);
* off records nothing and reads nothing; shadow records one row per split;
* a row holds role names, counts and lengths only, never goal or step text;
* the recorder cannot raise into the caller (unwritable log, junk arguments) and does not mutate
  what it is given; the log is bounded;
* THE REAL SPLIT IS UNCHANGED: a worker is driven through a split reply with the mode off, with
  shadow on, and with shadow on and an unwritable log, and the children handed to the spawner (text,
  ids, envelope fields), the spawner's keyword arguments, the worker's next prompt, status, outcome
  and reason are byte-identical in all three, even though the proposed team differs;
* production code calls it (the split path and the status snapshot).
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

from relay import fanout as fo  # noqa: E402
from relay import relay_fleet as rf  # noqa: E402
from relay import team_formation as TF  # noqa: E402

POOR_SUB = {
    "subtask": {"success": {"rate": 0.4, "n": 9, "label": "poor"}, "confidence": "low"},
    "aggregator": {"success": {"rate": 1.0, "n": 2, "label": "unknown"}, "confidence": "unknown"},
}
GOOD = {
    "subtask": {"success": {"rate": 1.0, "n": 9, "label": "good"}, "confidence": "low"},
    "aggregator": {"success": {"rate": 1.0, "n": 9, "label": "good"}, "confidence": "low"},
}

SECRET = "社外秘のゴール本文SECRET-GOAL-TEXT"
SPLIT_REPLY = """期間で分割します。

1. 2026年1月分の受信業務メールを取得する
2. 2026年2月分の受信業務メールを取得する
3. 2026年3月分の受信業務メールを取得する

%s""" % fo.SUBTASKS_READY


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    TF._reset_for_test()
    monkeypatch.setattr(TF, "LOG", str(tmp_path / "fleet" / "team_formation_shadow.jsonl"))
    monkeypatch.setattr(TF, "profiles", lambda now=None: POOR_SUB)
    yield
    TF._reset_for_test()


def _settings(tmp_path, monkeypatch, text):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    TF._reset_for_test()
    return str(p)


def _rows():
    try:
        with io.open(TF.LOG, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        return []


def _kids(n=3, cid="c1", depth=1):
    return [{"campaign_id": cid, "task_id": "%s-%d" % (cid, i), "depth": depth, "text": SECRET}
            for i in range(1, n + 1)]


# ------------------------------------------------------------------ the setting

@pytest.mark.parametrize("text,want", [
    ("team_mode=shadow\n", "shadow"), ("team_mode=off\n", "off"), ("team_mode=SHADOW\n", "shadow"),
    ("team_mode=on\n", "off"), ("team_mode=form\n", "off"), ("team_mode=\n", "off"),
    ("other=1\n", "off"), ("﻿team_mode=shadow\n", "shadow"),
])
def test_the_mode_reads_off_or_shadow_and_nothing_else(tmp_path, text, want):
    p = tmp_path / "s.txt"
    p.write_text(text, encoding="utf-8")
    assert TF.mode(str(p)) == want


def test_a_missing_settings_file_is_off(tmp_path):
    assert TF.mode(str(tmp_path / "nope.txt")) == "off"


def test_the_mode_follows_the_file_without_a_restart(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text("team_mode=off\n", encoding="utf-8")
    assert TF.mode(str(p)) == "off"
    p.write_text("team_mode=shadow\n# longer so the size changes\n", encoding="utf-8")
    assert TF.mode(str(p)) == "shadow"


def test_there_is_no_value_that_forms_a_team():
    assert TF.MODES == ("off", "shadow")


# ------------------------------------------------------------------ the rule (pure)

def test_a_known_good_profile_and_ordinary_steps_propose_the_static_team():
    actual, proposed, rules, known = TF.propose([80, 90, 100], GOOD)
    assert actual == {"subtask": 3, "aggregator": 1}
    assert proposed == actual and rules == [] and known is True


def test_tiny_steps_are_folded_in_pairs_but_never_below_the_minimum():
    _, proposed, rules, _ = TF.propose([10, 12, 14, 90], GOOD)
    assert proposed["subtask"] == 3 and rules == ["fold_tiny"]       # 3 tiny -> one pair folded
    _, proposed, _, _ = TF.propose([5, 6], GOOD)
    assert proposed["subtask"] == TF.MIN_CHILDREN                    # already at the floor
    _, proposed, _, _ = TF.propose([5, 6, 7, 8], GOOD)
    assert proposed["subtask"] == 2                                  # 4 tiny -> two pairs


def test_a_poor_subtask_or_aggregator_profile_adds_one_reviewer():
    _, proposed, rules, known = TF.propose([80, 90, 100], POOR_SUB)
    assert proposed == {"subtask": 3, "aggregator": 1, "reviewer": 1}
    assert rules == ["review"] and known is True
    poor_agg = dict(GOOD, aggregator={"success": {"rate": 0.2, "n": 9, "label": "poor"}})
    assert TF.propose([80, 90, 100], poor_agg)[1]["reviewer"] == 1


def test_an_unknown_profile_changes_nothing_and_says_so():
    actual, proposed, rules, known = TF.propose([80, 90, 100], {})
    assert proposed == actual and rules == [] and known is False
    unknown = {"subtask": {"success": {"rate": 0.0, "n": 1, "label": "unknown"}}}
    assert TF.propose([80, 90, 100], unknown)[1] == {"subtask": 3, "aggregator": 1}


# ------------------------------------------------------------------ the recorder

def test_off_records_nothing_and_creates_nothing(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "team_mode=off\n")
    TF.shadow_propose(_kids(), ["a" * 80] * 3, run_id="r", settings_path=s)
    assert not os.path.exists(os.path.dirname(TF.LOG))
    assert TF.status_block(s)["team_mode"] == {"mode": "off", "recorded": 0, "differed": 0}


def test_shadow_records_the_static_team_next_to_the_proposed_one(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "team_mode=shadow\n")
    TF.shadow_propose(_kids(3, "cX"), ["短い", "短い2", "x" * 90], run_id="run1", settings_path=s,
                      now=1000.0)
    (row,) = _rows()
    assert row["campaign_id"] == "cX" and row["run_id"] == "run1" and row["mode"] == "shadow"
    assert row["actual"] == {"roles": {"subtask": 3, "aggregator": 1}, "members": 4}
    assert row["proposed"]["roles"] == {"subtask": 2, "aggregator": 1, "reviewer": 1}
    assert row["proposed"]["members"] == 4
    assert row["differs"] is True and row["rules"] == ["fold_tiny", "review"]
    assert row["shape"] == {"steps": 3, "min_chars": 2, "max_chars": 90, "tiny": 2}
    assert row["profile_known"] is True and row["depth"] == 1
    assert TF.status_block(s)["team_mode"] == {"mode": "shadow", "recorded": 1, "differed": 1}


def test_a_row_never_holds_goal_or_step_text(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "team_mode=shadow\n")
    TF.shadow_propose(_kids(), [SECRET, SECRET + "2", SECRET + "3"], run_id="r", settings_path=s)
    raw = io.open(TF.LOG, encoding="utf-8").read()
    assert "SECRET" not in raw and "社外秘" not in raw


def test_a_retried_split_of_one_campaign_is_one_row(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "team_mode=shadow\n")
    for _ in range(3):
        TF.shadow_propose(_kids(cid="same"), ["a" * 80] * 3, run_id="r", settings_path=s)
    TF.shadow_propose(_kids(cid="other"), ["a" * 80] * 3, run_id="r", settings_path=s)
    assert [r["campaign_id"] for r in _rows()] == ["same", "other"]


def test_the_recorder_does_not_mutate_what_it_is_given(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "team_mode=shadow\n")
    kids, steps = _kids(), ["a" * 80, "b", "c" * 70]
    before = json.dumps([kids, steps], sort_keys=True)
    TF.shadow_propose(kids, steps, run_id="r", settings_path=s)
    assert json.dumps([kids, steps], sort_keys=True) == before


def test_a_write_error_never_reaches_the_caller(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "team_mode=shadow\n")
    blocked = tmp_path / "afile"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr(TF, "LOG", str(blocked / "sub" / "log.jsonl"))
    assert TF.shadow_propose(_kids(), ["a" * 80] * 3, run_id="r", settings_path=s) is None
    assert TF._STATE["write_errors"] >= 1 and TF._STATE["recorded"] == 0


@pytest.mark.parametrize("args", [
    (None, None), ([], []), ([{}], [""]), ("junk", 5), ([None], [None, None]),
    ([{"campaign_id": 7, "depth": "x"}], ["a", "b"]),
])
def test_junk_arguments_never_raise(tmp_path, monkeypatch, args):
    s = _settings(tmp_path, monkeypatch, "team_mode=shadow\n")
    assert TF.shadow_propose(args[0], args[1], run_id=None, settings_path=s) is None


def test_a_failing_profile_does_not_stop_the_record(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "team_mode=shadow\n")
    monkeypatch.setattr(TF, "profiles", lambda now=None: {})
    TF.shadow_propose(_kids(), ["a" * 80] * 3, run_id="r", settings_path=s)
    (row,) = _rows()
    assert row["profile_known"] is False and row["differs"] is False


def test_the_log_is_bounded_and_rotated(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "team_mode=shadow\n")
    monkeypatch.setattr(TF, "MAX_BYTES", 600)
    for i in range(30):
        TF.shadow_propose(_kids(cid="c%d" % i), ["a" * 80] * 3, run_id="r", settings_path=s)
    assert os.path.exists(TF.LOG + ".1")
    assert os.path.getsize(TF.LOG) < 600 + 1200 and os.path.getsize(TF.LOG + ".1") < 600 + 1200


def test_the_seen_set_is_bounded(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "team_mode=shadow\n")
    monkeypatch.setattr(TF, "SEEN_MAX", 5)
    for i in range(20):
        TF.shadow_propose(_kids(cid="c%d" % i), ["a" * 80] * 3, run_id="r", settings_path=s)
    assert len(TF._seen) <= 5


# ------------------------------------------------------------------ the real split is unchanged

def _fixed_label(real):
    import datetime
    when = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    return lambda worker_name="", now=None: real(worker_name, when)


def _split(tmp_path, monkeypatch, settings_text, log_path=None):
    """One worker driven through a split reply. Returns everything the split produced."""
    _settings(tmp_path, monkeypatch, settings_text)
    if log_path is not None:
        monkeypatch.setattr(TF, "LOG", log_path)
    TF._reset_for_test()
    # THE FIRST PROMPT OF A WORKER OPENS WITH "[YYYY-MM-DD HH:MM:SS +00:00 | w0]" (the
    # conversation start label, read from the wall clock at one-second resolution). Two runs
    # that straddle a second boundary differ in that one field and nothing else (PR #228's CI:
    # 06:41:20 vs 06:41:21). Give the label a fixed instant through its own `now` parameter, so
    # the comparison still covers every other byte of every prompt.
    monkeypatch.setattr(rf, "conversation_start_label", _fixed_label(rf.conversation_start_label))
    got = []

    def spawn(goal, kids, **kw):
        got.append({"goal": goal, "kids": kids, "kw": kw})

    w = rf.RelayWorker({"text": "1〜3月のメールを一覧化する " + SECRET, "depth": 0}, "w0",
                       fanout=True, spawn_fn=spawn)
    w.run_id = "t-run"
    w._decide(SPLIT_REPLY)
    return {"spawned": got, "status": w.status, "outcome": w.outcome, "reason": w.reason,
            "job": w.job, "fanout": w.fanout}


def test_the_real_split_and_its_prompts_are_byte_identical_in_off_and_shadow(tmp_path, monkeypatch):
    off = _split(tmp_path, monkeypatch, "team_mode=off\n")
    assert not os.path.exists(os.path.dirname(TF.LOG))                  # off wrote nothing
    assert len(off["spawned"]) == 1 and len(off["spawned"][0]["kids"]) == 3
    assert off["outcome"] == "FANOUT"

    shadow = _split(tmp_path, monkeypatch, "team_mode=shadow\n")
    (row,) = _rows()
    assert row["differs"] is True and row["actual"]["roles"]["subtask"] == 3
    assert row["proposed"]["roles"]["subtask"] == 2                      # the shadow DID disagree ...

    blocked = tmp_path / "afile"
    blocked.write_text("x", encoding="utf-8")
    broken = _split(tmp_path, monkeypatch, "team_mode=shadow\n",
                    log_path=str(blocked / "sub" / "log.jsonl"))
    assert TF._STATE["write_errors"] >= 1                                # ... a failed write is survived

    dump = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)  # noqa: E731
    assert dump(shadow) == dump(off)                                     # ... and changed nothing
    assert dump(broken) == dump(off)


def test_a_budget_refused_or_unusable_split_records_nothing(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "team_mode=shadow\n")
    w = rf.RelayWorker({"text": "g", "depth": 0}, "w0", fanout=True,
                       spawn_fn=lambda *a, **k: pytest.fail("must not spawn"))
    w._decide("1. only one step\n\n" + fo.SUBTASKS_READY)
    assert _rows() == []                                                 # no children, no team


# ------------------------------------------------------------------ production calls it

def test_the_split_path_calls_the_recorder_before_the_spawner_and_ignores_its_result():
    with io.open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("team_formation.shadow_propose(") == 1
    i = src.index("team_formation.shadow_propose(")
    assert src.index("self._spawn_fn(self.goal, kids,", i) - i < 400     # right before the spawn
    line_start = src.rfind("\n", 0, i) + 1
    assert src[line_start:i].strip() == ""                               # a bare statement
    guard = src.rfind("if kids and self._spawn_fn:", 0, i)
    assert guard != -1 and i - guard < 1500                              # only for a real split


def test_the_status_snapshot_calls_the_report():
    with io.open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "_snap.update(_team_formation_block())" in src
