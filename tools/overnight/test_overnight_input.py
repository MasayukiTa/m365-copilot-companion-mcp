# -*- coding: utf-8 -*-
"""Input file parsing and goal text for the overnight batch (synthetic data only)."""
from __future__ import annotations

import json
import re

import pytest

from tools.overnight.items import (MAX_GOAL_CHARS, Item, build_goal, parse_items)


def _w(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_txt_comments_blank_lines_and_preamble(tmp_path):
    p = _w(tmp_path, "items.txt",
           "# 前置き: 数値は小数1桁で\n\n# memo\n先月の件数を集計して\nB2: 月別の推移を出して\n")
    r = parse_items(p)
    assert [i.id for i in r.items] == ["I001", "B2"]
    assert r.preamble == "数値は小数1桁で"
    assert r.items[1].ask == "月別の推移を出して"


def test_json_items_with_wrapper(tmp_path):
    data = {"preamble": "共通", "items": [
        {"id": "A1", "ask": "件数を数えて", "tags": ["x"], "max_minutes": 5},
        {"ask": "平均を出して"}]}
    r = parse_items(_w(tmp_path, "items.json", json.dumps(data, ensure_ascii=False)))
    assert r.items[0].max_minutes == 5.0 and r.items[0].tags == ["x"]
    assert r.items[1].id == "I001" and r.preamble == "共通"


def test_json_plain_list(tmp_path):
    r = parse_items(_w(tmp_path, "items.json", '[{"id":"A1","ask":"x"},"y"]'))
    assert len(r.items) == 2


def test_same_wording_is_kept_but_a_duplicate_id_is_skipped(tmp_path):
    # Two requests with nearly identical wording are BOTH kept (content resemblance never drops
    # a job; 2026-10-26/27 owner instruction). Only a duplicate ID is skipped, because that is an
    # identity collision, not a resemblance.
    p = _w(tmp_path, "items.txt", "件数を集計して\n 件数を 集計して。\nA1: 別の依頼\nA1: 同じID\n")
    r = parse_items(p)
    assert [i.id for i in r.items] == ["I001", "I002", "A1"]
    reasons = [s[1] for s in r.skipped]
    assert "duplicate id" in reasons
    assert not any(str(x).startswith("duplicate of") for x in reasons)


def test_item_limit_warns_and_keeps_the_rest_as_skipped(tmp_path):
    p = _w(tmp_path, "items.txt", "\n".join("依頼その%d" % i for i in range(7)))
    r = parse_items(p, max_items=5)
    assert len(r.items) == 5 and len(r.skipped) == 2
    assert any("limit" in w for w in r.warnings)


def test_empty_file_warns(tmp_path):
    r = parse_items(_w(tmp_path, "items.txt", "# only a comment\n"))
    assert r.items == [] and r.warnings


def test_cp932_file_is_read(tmp_path):
    p = tmp_path / "items.txt"
    p.write_bytes("先月の件数\n".encode("cp932"))
    assert parse_items(p).items[0].ask == "先月の件数"


def test_goal_is_one_short_japanese_line_with_the_id():
    it = Item("A1", "過去1か月の不良データを集計して。\n月別にして")
    g = build_goal(it, "C:\\Users\\x\\Documents\\朝レポート\\20260101_2300", "数値は小数1桁で。")
    assert "\n" not in g and "\r" not in g
    assert g.startswith("[A1] ")
    assert "C:/Users/x/Documents/朝レポート/20260101_2300/A1/" in g
    assert len(re.findall("。", g)) <= 2
    assert len(g) <= MAX_GOAL_CHARS
    assert re.search(r"[ぁ-んァ-ン一-龥]", g)


def test_variant_changes_the_text_but_keeps_the_id():
    it = Item("A1", "件数を数えて")
    a, b = build_goal(it, "/o", "", 0), build_goal(it, "/o", "", 1)
    assert a != b and b.startswith("[A1] ")


def test_overlong_goal_is_refused():
    with pytest.raises(ValueError):
        build_goal(Item("A1", "あ" * 150), "/" + "x" * 300)
