# -*- coding: utf-8 -*-
"""A call that lands in the overlap of several workers' turn windows cannot name its worker, but
when every overlapping worker belongs to one fan-out campaign the campaign is not unknown. The
ledger keeps it as `campaign_candidate` (additive); task and worker stay empty; attr stays
"ambiguous" so existing readers see exactly what they saw before."""
import json

import pytest

from tools import tool_ledger as L
from tools import turn_context as T


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "LEDGER_PATH", str(tmp_path / "tool_events.jsonl"), raising=False)
    return str(tmp_path / "tool_events.jsonl")


def _rows(path):
    return [json.loads(l) for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]


def _patch(monkeypatch, cands, idents):
    monkeypatch.setattr(T, "candidates", lambda ts: cands)
    monkeypatch.setattr(T, "identity_of", lambda worker, ts: idents.get(worker, {}))


def test_siblings_of_one_campaign_leave_the_campaign_but_not_the_worker(ledger, monkeypatch):
    _patch(monkeypatch, [("w1", "t1"), ("w2", "t2")],
           {"w1": {"campaign_id": "cA", "role": "subtask"}, "w2": {"campaign_id": "cA"}})
    L.record_call("read_file", {"path": "x"}, ts=100.0)
    row = _rows(ledger)[0]
    assert row["attr"] == "ambiguous"
    assert row["task"] == "" and row["worker"] == ""
    assert row["campaign_candidate"] == "cA"


def test_two_campaigns_in_the_overlap_give_no_campaign(ledger, monkeypatch):
    _patch(monkeypatch, [("w1", "t1"), ("w2", "t2")],
           {"w1": {"campaign_id": "cA"}, "w2": {"campaign_id": "cB"}})
    L.record_call("read_file", {"path": "x"}, ts=100.0)
    row = _rows(ledger)[0]
    assert row["attr"] == "ambiguous" and "campaign_candidate" not in row


def test_a_candidate_without_an_identity_gives_no_campaign(ledger, monkeypatch):
    _patch(monkeypatch, [("w1", "t1"), ("w2", "t2")], {"w1": {"campaign_id": "cA"}})
    L.record_call("read_file", {"path": "x"}, ts=100.0)
    assert "campaign_candidate" not in _rows(ledger)[0]


def test_an_unambiguous_call_is_unchanged(ledger, monkeypatch):
    _patch(monkeypatch, [("w1", "t1")], {"w1": {"campaign_id": "cA", "task_id": "cA-1"}})
    L.record_call("read_file", {"path": "x"}, ts=100.0)
    row = _rows(ledger)[0]
    assert row["attr"] == "window" and row["worker"] == "w1" and "campaign_candidate" not in row


def test_the_report_has_a_production_reader_for_the_field():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "scripts", "sibling_dup_report.py"), encoding="utf-8").read()
    assert "campaign_candidate" in src
