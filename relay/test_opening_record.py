# -*- coding: utf-8 -*-
"""Tests for relay/opening_record.py -- the append-only JSONL record of data openings.

Synthetic item ids only ('a1', 'item-001'); no real data appears here.
"""
from __future__ import annotations

import hashlib
import io
import json
import os

import pytest

from relay import opening_record as orc


def _record(tmp_path):
    return os.path.join(str(tmp_path), "openings.jsonl")


def test_items_digest_is_sha256_of_sorted_unique_ids():
    ids = ["item-001", "a1", "a1"]
    expected = hashlib.sha256("\n".join(["a1", "item-001"]).encode("utf-8")).hexdigest()
    assert orc.items_digest(ids) == expected


def test_items_digest_is_order_and_duplicate_insensitive():
    assert orc.items_digest(["a1", "item-001"]) == orc.items_digest(["item-001", "a1"])
    assert orc.items_digest(["a1", "item-001"]) == orc.items_digest(["a1", "a1", "item-001"])


def test_items_digest_distinguishes_different_sets():
    assert orc.items_digest(["a1"]) != orc.items_digest(["a1", "item-001"])


def test_init_record_creates_empty_file_and_parents(tmp_path):
    path = os.path.join(str(tmp_path), "sub", "deep", "openings.jsonl")
    returned = orc.init_record(path)
    assert returned == path
    assert os.path.exists(path)
    assert orc.read_records(path) == []


def test_init_record_is_idempotent_and_preserves_rows(tmp_path):
    path = _record(tmp_path)
    orc.record_opening(path, "an-1", ["a1"], "t0", ["c1"])
    orc.init_record(path)
    rows = orc.read_records(path)
    assert len(rows) == 1
    assert rows[0]["analysis_id"] == "an-1"


def test_record_opening_returns_the_written_row(tmp_path):
    path = _record(tmp_path)
    row = orc.record_opening(path, "an-1", ["item-001", "a1"], "2020-01-01", ["c1", "c2"])
    assert row["analysis_id"] == "an-1"
    assert row["items"] == ["a1", "item-001"]
    assert row["items_digest"] == orc.items_digest(["a1", "item-001"])
    assert row["fetched_at"] == "2020-01-01"
    assert row["columns"] == ["c1", "c2"]


def test_record_opening_appends_only(tmp_path):
    path = _record(tmp_path)
    orc.record_opening(path, "an-1", ["a1"], "t0", ["c1"])
    orc.record_opening(path, "an-2", ["item-001"], "t1", ["c1"])
    with io.open(path, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["analysis_id"] == "an-1"
    assert json.loads(lines[1])["analysis_id"] == "an-2"


def test_record_opening_creates_file_without_init(tmp_path):
    path = _record(tmp_path)
    assert not os.path.exists(path)
    orc.record_opening(path, "an-1", ["a1"], "t0", ["c1"])
    assert os.path.exists(path)
    assert len(orc.read_records(path)) == 1


def test_read_records_missing_file_is_empty(tmp_path):
    assert orc.read_records(_record(tmp_path)) == []


def test_read_records_in_file_order(tmp_path):
    path = _record(tmp_path)
    for i in range(3):
        orc.record_opening(path, "an-%d" % i, ["a1"], "t%d" % i, ["c1"])
    rows = orc.read_records(path)
    assert [r["analysis_id"] for r in rows] == ["an-0", "an-1", "an-2"]


def test_build_row_does_not_write(tmp_path):
    path = _record(tmp_path)
    row = orc.build_row("an-1", ["item-001", "a1"], "t0", ["c1"])
    assert not os.path.exists(path)
    assert row["items"] == ["a1", "item-001"]
    assert row["items_digest"] == orc.items_digest(["a1", "item-001"])


def test_iter_openings_all(tmp_path):
    path = _record(tmp_path)
    orc.record_opening(path, "an-1", ["a1"], "t0", ["c1"])
    orc.record_opening(path, "an-2", ["item-001"], "t1", ["c1"])
    got = [r["analysis_id"] for r in orc.iter_openings(path)]
    assert got == ["an-1", "an-2"]


def test_iter_openings_filtered_by_analysis(tmp_path):
    path = _record(tmp_path)
    orc.record_opening(path, "an-1", ["a1"], "t0", ["c1"])
    orc.record_opening(path, "an-2", ["item-001"], "t1", ["c1"])
    orc.record_opening(path, "an-1", ["item-002"], "t2", ["c1"])
    got = [r["items"] for r in orc.iter_openings(path, analysis_id="an-1")]
    assert got == [["a1"], ["item-002"]]


def test_iter_openings_missing_file_yields_nothing(tmp_path):
    assert list(orc.iter_openings(_record(tmp_path))) == []


def test_reuse_is_visible_by_digest(tmp_path):
    # A confirmation that reopened an earlier analysis's items shares its digest.
    path = _record(tmp_path)
    orc.record_opening(path, "earlier", ["a1", "item-001"], "t0", ["c1"])
    orc.record_opening(path, "confirm", ["item-001", "a1"], "t1", ["c1"])
    rows = orc.read_records(path)
    assert rows[0]["items_digest"] == rows[1]["items_digest"]


def test_independent_confirmation_has_a_different_digest(tmp_path):
    path = _record(tmp_path)
    orc.record_opening(path, "earlier", ["a1"], "t0", ["c1"])
    orc.record_opening(path, "confirm", ["item-001"], "t1", ["c1"])
    rows = orc.read_records(path)
    assert rows[0]["items_digest"] != rows[1]["items_digest"]
