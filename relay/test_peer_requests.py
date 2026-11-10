# -*- coding: utf-8 -*-
"""Peer work requests, shadow only (relay/peer_requests.py, setting `peer_requests`).

The properties that matter, each tested where it can fail:

* the proposal rule as a pure function (REQUEST needs >=2 unfinished subtasks and queued work,
  OFFER needs a freed worker while another campaign is unfinished, CLAIM pairs them);
* off records nothing and reads nothing; shadow records one row per tick that has any action;
* a row holds campaign ids, role names and counts only, never goal or step text;
* the recorder cannot raise into the caller (unwritable log, junk arguments) and does not mutate
  what it is given; the log and the seen-set are bounded;
* the three peer kinds live as data here and are deliberately NOT on relay/board_kinds.REGISTRY,
  so the board\'s hot serialized path, its four kinds and its tests are unchanged;
* production code calls it (the coordinator tick and the status snapshot).
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

from relay import board_kinds as BK  # noqa: E402
from relay import peer_requests as PR  # noqa: E402

SECRET = "\u793e\u5916\u79d8\u306e\u30b4\u30fc\u30eb\u672c\u6587SECRET-GOAL-TEXT"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    PR._reset_for_test()
    monkeypatch.setattr(PR, "LOG", str(tmp_path / "fleet" / "peer_requests_shadow.jsonl"))
    yield
    PR._reset_for_test()


def _settings(tmp_path, monkeypatch, text):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    PR._reset_for_test()
    return str(p)


def _rows():
    try:
        with io.open(PR.LOG, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        return []


def _sub(cid, fin, text=SECRET):
    return {"campaign_id": cid, "role": "subtask", "outcome": ("DONE" if fin else ""),
            "text": text}


# ------------------------------------------------------------------ the setting

@pytest.mark.parametrize("text,want", [
    ("peer_requests=shadow\n", "shadow"), ("peer_requests=off\n", "off"),
    ("peer_requests=SHADOW\n", "shadow"), ("peer_requests=on\n", "off"),
    ("peer_requests=grant\n", "off"), ("peer_requests=\n", "off"), ("other=1\n", "off"),
    ("\ufeffpeer_requests=shadow\n", "shadow"),
])
def test_the_mode_reads_off_or_shadow_and_nothing_else(tmp_path, text, want):
    p = tmp_path / "s.txt"
    p.write_text(text, encoding="utf-8")
    assert PR.mode(str(p)) == want


def test_a_missing_settings_file_is_off(tmp_path):
    assert PR.mode(str(tmp_path / "nope.txt")) == "off"


def test_the_mode_follows_the_file_without_a_restart(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text("peer_requests=off\n", encoding="utf-8")
    assert PR.mode(str(p)) == "off"
    p.write_text("peer_requests=shadow\n# longer so the size changes\n", encoding="utf-8")
    assert PR.mode(str(p)) == "shadow"


def test_there_is_no_value_that_posts_or_grants():
    assert PR.MODES == ("off", "shadow")


# ------------------------------------------------------------- the kinds stay off the board

def test_the_three_peer_kinds_are_data_here_and_not_on_the_board():
    assert PR.PEER_KINDS == ("request", "offer", "claim_work")
    for k in PR.PEER_KINDS:
        assert not BK.is_kind(k)                         # the board still refuses them
    assert BK.KINDS == ("claim", "result", "fact", "question")   # exactly the four


# ------------------------------------------------------------------ the rule (pure)

def test_an_overloaded_campaign_with_queue_requests_help():
    rows = [_sub("cA", False), _sub("cA", False)]
    req, off, claim = PR.propose(rows, queued=2)
    assert [r["campaign_id"] for r in req] == ["cA"] and req[0]["unfinished"] == 2
    assert off == [] and claim == []


def test_no_request_without_queued_work():
    rows = [_sub("cA", False), _sub("cA", False)]
    assert PR.propose(rows, queued=0)[0] == []


def test_no_request_with_only_one_unfinished_subtask():
    rows = [_sub("cA", False), _sub("cA", True)]
    assert PR.propose(rows, queued=5)[0] == []


def test_a_freed_worker_offers_while_another_campaign_is_unfinished():
    rows = [_sub("cDone", True), _sub("cBusy", False)]
    req, off, claim = PR.propose(rows, queued=0)
    assert [o["campaign_id"] for o in off] == ["cDone"] and off[0]["free"] == 1
    assert claim == []                                   # no request to claim


def test_no_offer_when_no_other_campaign_is_unfinished():
    rows = [_sub("cDone", True), _sub("cDone", True)]
    assert PR.propose(rows, queued=0)[1] == []


def test_a_request_and_an_offer_in_one_tick_make_a_claim():
    rows = [_sub("cBusy", False), _sub("cBusy", False), _sub("cFree", True)]
    req, off, claim = PR.propose(rows, queued=3)
    assert req and off and len(claim) == 1
    assert claim[0]["request_campaign"] == "cBusy" and claim[0]["offer_campaign"] == "cFree"


# ------------------------------------------------------------------ the recorder

def test_off_records_nothing_and_creates_nothing(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "peer_requests=off\n")
    PR.shadow_tick([_sub("cA", False), _sub("cA", False)], queued=2, run_id="r", settings_path=s)
    assert not os.path.exists(os.path.dirname(PR.LOG))
    assert PR.status_block(s)["peer_requests"] == {"mode": "off", "recorded": 0,
        "requests": 0, "offers": 0, "claims": 0}


def test_shadow_records_one_row_with_the_actions(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "peer_requests=shadow\n")
    rows = [_sub("cBusy", False), _sub("cBusy", False), _sub("cFree", True)]
    PR.shadow_tick(rows, queued=3, run_id="run1", settings_path=s, now=1000.0)
    (row,) = _rows()
    assert row["run_id"] == "run1" and row["mode"] == "shadow" and row["queued"] == 3
    assert row["counts"] == {"request": 1, "offer": 1, "claim_work": 1}
    assert row["requests"][0]["campaign_id"] == "cBusy"
    assert row["offers"][0]["campaign_id"] == "cFree"
    assert PR.status_block(s)["peer_requests"] == {"mode": "shadow", "recorded": 1,
        "requests": 1, "offers": 1, "claims": 1}


def test_a_tick_with_no_action_records_nothing(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "peer_requests=shadow\n")
    PR.shadow_tick([_sub("cA", True)], queued=0, run_id="r", settings_path=s)
    assert _rows() == []


def test_a_row_never_holds_goal_or_step_text(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "peer_requests=shadow\n")
    rows = [_sub("cBusy", False, SECRET), _sub("cBusy", False, SECRET), _sub("cFree", True, SECRET)]
    PR.shadow_tick(rows, queued=3, run_id="r", settings_path=s)
    raw = io.open(PR.LOG, encoding="utf-8").read()
    assert "SECRET" not in raw and "\u793e\u5916\u79d8" not in raw


def test_an_identical_tick_is_one_row(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "peer_requests=shadow\n")
    rows = [_sub("cBusy", False), _sub("cBusy", False), _sub("cFree", True)]
    for _ in range(3):
        PR.shadow_tick(rows, queued=3, run_id="r", settings_path=s)
    assert len(_rows()) == 1


def test_the_recorder_does_not_mutate_what_it_is_given(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "peer_requests=shadow\n")
    rows = [_sub("cBusy", False), _sub("cBusy", False), _sub("cFree", True)]
    before = json.dumps(rows, sort_keys=True)
    PR.shadow_tick(rows, queued=3, run_id="r", settings_path=s)
    assert json.dumps(rows, sort_keys=True) == before


def test_a_write_error_never_reaches_the_caller(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "peer_requests=shadow\n")
    blocked = tmp_path / "afile"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr(PR, "LOG", str(blocked / "sub" / "log.jsonl"))
    rows = [_sub("cBusy", False), _sub("cBusy", False), _sub("cFree", True)]
    assert PR.shadow_tick(rows, queued=3, run_id="r", settings_path=s) is None
    assert PR._STATE["write_errors"] >= 1 and PR._STATE["recorded"] == 0


@pytest.mark.parametrize("rows,queued", [
    (None, 0), ([], 0), ([{}], 1), ("junk", 5), ([None], 0),
    ([{"campaign_id": 7, "role": 5}], 2),
])
def test_junk_arguments_never_raise(tmp_path, monkeypatch, rows, queued):
    s = _settings(tmp_path, monkeypatch, "peer_requests=shadow\n")
    assert PR.shadow_tick(rows, queued=queued, run_id=None, settings_path=s) is None


def test_the_log_is_bounded_and_rotated(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "peer_requests=shadow\n")
    monkeypatch.setattr(PR, "MAX_BYTES", 400)
    for i in range(40):
        rows = [_sub("cB%d" % i, False), _sub("cB%d" % i, False), _sub("cF%d" % i, True)]
        PR.shadow_tick(rows, queued=3, run_id="r", settings_path=s)
    assert os.path.exists(PR.LOG + ".1")


def test_the_seen_set_is_bounded(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, "peer_requests=shadow\n")
    monkeypatch.setattr(PR, "SEEN_MAX", 5)
    for i in range(20):
        rows = [_sub("cB%d" % i, False), _sub("cB%d" % i, False), _sub("cF%d" % i, True)]
        PR.shadow_tick(rows, queued=3, run_id="r", settings_path=s)
    assert len(PR._seen) <= 5


# ------------------------------------------------------------------ production calls it

def test_the_coordinator_tick_calls_the_recorder():
    with io.open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "_peer_requests_tick(" in src
    assert '_timed("peer_requests_tick"' in src


def test_the_status_snapshot_calls_the_report():
    with io.open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "_snap.update(_peer_requests_block())" in src
