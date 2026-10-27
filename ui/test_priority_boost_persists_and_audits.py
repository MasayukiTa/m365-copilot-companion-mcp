# -*- coding: utf-8 -*-
"""すでに並んだ仕事に、あとから最優先を付けて、それを記録に残す。

fleet_submit(priority=True) は投入時だけ有効で、すでに waiting/ready にいる担当にあとから
優先を付ける手段が無かった。relay/priority_boost.py がその欠けた半分：

  * boost/unboost が .fleet/tasks/boosts/<jid>.json を作る/消す（投入後の担当に優先印）
  * 「誰が・いつ・どの担当を」を boost_audit.jsonl に追記（JSON Lines, utf-8, 改行 \n, BOM 無し）
  * 設定フラグ FLEET_PRIORITY_BOOST_ENABLED（既定オン、オフで全無効）

このテストは tasks_dir を tmp_path に明示して渡し、本番の .fleet に触れない。
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import priority_boost as PB  # noqa: E402


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Default-on is the shipped default, but make it explicit so a stray env var in CI cannot
    turn the feature off under the feature's own tests."""
    monkeypatch.setenv("FLEET_PRIORITY_BOOST_ENABLED", "1")


def test_boost_writes_a_marker_and_is_boosted_reads_it(tmp_path):
    td = str(tmp_path)
    assert PB.is_boosted("job-1", tasks_dir=td) is False
    assert PB.boost("job-1", actor="owner", tasks_dir=td) is True
    assert PB.is_boosted("job-1", tasks_dir=td) is True
    assert PB.list_boosted(tasks_dir=td) == ["job-1"]
    assert PB.boosted_count(tasks_dir=td) == 1


def test_unboost_removes_the_marker(tmp_path):
    td = str(tmp_path)
    PB.boost("job-2", actor="owner", tasks_dir=td)
    assert PB.is_boosted("job-2", tasks_dir=td) is True
    assert PB.unboost("job-2", actor="owner", tasks_dir=td) is True
    assert PB.is_boosted("job-2", tasks_dir=td) is False
    # Removing an absent marker is a no-op that reports False.
    assert PB.unboost("job-2", actor="owner", tasks_dir=td) is False


def test_audit_records_who_and_what_as_jsonl_without_bom(tmp_path):
    td = str(tmp_path)
    PB.boost("job-3", actor="owner", reason="急ぎ", tasks_dir=td)
    PB.unboost("job-3", actor="owner", tasks_dir=td)

    path = os.path.join(td, PB.AUDIT_NAME)
    assert os.path.exists(path)

    # First byte must NOT be a BOM: a line reader opening utf-8 must parse line 1.
    with io.open(path, "rb") as fh:
        head = fh.read(3)
    assert head != b"\xef\xbb\xbf", "audit ledger must be written WITHOUT a byte-order mark"

    with io.open(path, encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    assert [r["action"] for r in lines] == ["boost", "unboost"]
    assert all(r["jid"] == "job-3" for r in lines)
    assert all(r["actor"] == "owner" for r in lines)
    assert lines[0].get("reason") == "急ぎ"
    # Every record is timestamped and individually addressable.
    assert all(isinstance(r["ts"], (int, float)) for r in lines)
    assert len({r["audit_id"] for r in lines}) == 2


def test_read_audit_returns_records_oldest_first(tmp_path):
    td = str(tmp_path)
    PB.boost("a", actor="owner", tasks_dir=td)
    PB.boost("b", actor="owner", tasks_dir=td)
    PB.unboost("a", actor="owner", tasks_dir=td)
    recs = PB.read_audit(tasks_dir=td)
    assert [(r["action"], r["jid"]) for r in recs] == [
        ("boost", "a"), ("boost", "b"), ("unboost", "a")]
    # limit keeps the most recent slice, still oldest-first within it.
    assert [(r["action"], r["jid"]) for r in PB.read_audit(tasks_dir=td, limit=2)] == [
        ("boost", "b"), ("unboost", "a")]


def test_an_address_in_actor_is_redacted(tmp_path):
    td = str(tmp_path)
    PB.boost("job-4", actor="worker alice@example.com on 10.0.0.1", tasks_dir=td)
    marker = os.path.join(td, PB.BOOSTS_DIR, "job-4.json")
    data = json.loads(io.open(marker, encoding="utf-8").read())
    assert "alice@example.com" not in data["actor"]
    assert "<address redacted>" in data["actor"]
    recs = PB.read_audit(tasks_dir=td)
    assert "alice@example.com" not in recs[0]["actor"]


def test_marker_file_has_no_byte_order_mark(tmp_path):
    td = str(tmp_path)
    PB.boost("job-5", actor="owner", tasks_dir=td)
    marker = os.path.join(td, PB.BOOSTS_DIR, "job-5.json")
    with io.open(marker, "rb") as fh:
        assert fh.read(3) != b"\xef\xbb\xbf"


def test_disabled_turns_every_entry_point_into_a_no_op(tmp_path, monkeypatch):
    td = str(tmp_path)
    # First boost while enabled, so there is a real marker on disk.
    PB.boost("job-6", actor="owner", tasks_dir=td)
    monkeypatch.setenv("FLEET_PRIORITY_BOOST_ENABLED", "0")
    assert PB.is_enabled() is False
    assert PB.boost("job-7", actor="owner", tasks_dir=td) is False
    assert PB.unboost("job-6", actor="owner", tasks_dir=td) is False
    assert PB.is_boosted("job-6", tasks_dir=td) is False
    assert PB.list_boosted(tasks_dir=td) == []
    assert PB.boosted_count(tasks_dir=td) == 0
    # The marker that was written while enabled is left untouched on disk.
    assert os.path.exists(os.path.join(td, PB.BOOSTS_DIR, "job-6.json"))


def test_disabled_values_are_recognised(monkeypatch):
    for off in ("0", "false", "FALSE", "no", "off", "Off"):
        monkeypatch.setenv("FLEET_PRIORITY_BOOST_ENABLED", off)
        assert PB.is_enabled() is False, off
    for on in ("1", "true", "yes", "on", "", "anything"):
        monkeypatch.setenv("FLEET_PRIORITY_BOOST_ENABLED", on)
        assert PB.is_enabled() is True, on


def test_boost_is_idempotent_but_audit_records_each_press(tmp_path):
    td = str(tmp_path)
    assert PB.boost("job-8", actor="owner", tasks_dir=td) is True
    assert PB.boost("job-8", actor="owner", tasks_dir=td) is True
    assert PB.list_boosted(tasks_dir=td) == ["job-8"]  # still one marker
    actions = [r["action"] for r in PB.read_audit(tasks_dir=td)]
    assert actions == ["boost", "boost"]  # but two audit lines
