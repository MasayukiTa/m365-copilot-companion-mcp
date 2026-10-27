# -*- coding: utf-8 -*-
"""Agent roster (relay/roster.py): derivation, bounds, never stored, the board digest, the cockpit.

Everything writes under tmp_path; nothing here touches the operator's .fleet.
"""
from __future__ import annotations

import builtins
import json
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import agent_board as AB  # noqa: E402
from relay import roster as R  # noqa: E402
from tools import board_tool, ui_source  # noqa: E402

NOW = 1_800_000_000


def _w(name="w1", status="waiting", turn=1, cid="cmp1", role="child", summary="task"):
    return {"name": name, "status": status, "turn": turn, "campaign_id": cid, "role": role,
            "goal_summary": summary, "goal": "THE FULL GOAL TEXT MUST NEVER APPEAR"}


# ------------------------------------------------------------------ derivation

def test_empty_and_junk_inputs_give_the_empty_roster():
    empty = {"total": 0, "shown": 0, "omitted": 0, "campaigns": []}
    for bad in ([], None, "x", 5, {}, [None, 3, "a"]):
        assert R.build_roster(bad) == empty
    assert R.roster_block([]) == {}
    assert R.roster_block(None) == {}


def test_one_worker():
    r = R.build_roster([_w()])
    assert (r["total"], r["shown"], r["omitted"]) == (1, 1, 0)
    assert r["campaigns"] == [{"campaign_id": "cmp1", "count": 1, "workers": [
        {"name": "w1", "role": "child", "status": "waiting", "turn": 1, "task": "task"}]}]
    assert R.roster_block([_w()]) == {"roster": r}


def test_many_workers_are_grouped_per_campaign_live_first_and_standalone_last():
    ws = [_w("a", cid=""), _w("b", status="done", cid="c1"), _w("c", cid="c1"), _w("d", cid="c2")]
    r = R.build_roster(ws)
    assert [c["campaign_id"] for c in r["campaigns"]] == ["c1", "c2", ""]
    assert [x["name"] for x in r["campaigns"][0]["workers"]] == ["c", "b"]   # live before done
    assert r["total"] == 4 and r["shown"] == 4


def test_missing_fields_do_not_raise_and_get_defaults():
    r = R.build_roster([{}, {"name": 7, "turn": "x", "status": None, "goal_summary": ["a"]},
                        {"name": "n", "turn": True, "campaign_id": None}])
    rows = [x for c in r["campaigns"] for x in c["workers"]]
    assert len(rows) == 3
    for x in rows:
        assert x["status"] == R.UNKNOWN and x["turn"] == 0 and x["task"] == "" and x["role"] == ""
    assert rows[0]["name"] == "?" and rows[2]["name"] == "n"


def test_an_unknown_status_is_kept_as_written_and_sorts_as_live():
    r = R.build_roster([_w("a", status="done"), _w("b", status="brand_new_state")])
    assert [x["status"] for x in r["campaigns"][0]["workers"]] == ["brand_new_state", "done"]


def test_long_and_odd_text_is_bounded_and_one_line():
    big = "x" * 5000 + "\n\x00 second line"
    r = R.build_roster([_w(name=big, role=big, status=big, cid=big, summary=big)])
    c = r["campaigns"][0]
    x = c["workers"][0]
    assert len(c["campaign_id"]) <= R.CAMPAIGN_CHARS and len(x["name"]) <= R.NAME_CHARS
    assert len(x["role"]) <= R.ROLE_CHARS and len(x["status"]) <= R.STATUS_CHARS
    assert len(x["task"]) <= R.TASK_CHARS
    assert not re.search(r"[\x00-\x1f ]", json.dumps(r, ensure_ascii=False))
    for line in R.render_lines(r):
        assert len(line) <= R.LINE_CHARS + 1 and "\n" not in line


def test_counts_are_bounded_and_the_rest_is_reported():
    ws = [_w("w%d" % i, cid="c%d" % (i % 20)) for i in range(400)]
    r = R.build_roster(ws)
    assert len(r["campaigns"]) == R.MAX_CAMPAIGNS
    assert all(len(c["workers"]) <= R.MAX_WORKERS_PER_CAMPAIGN for c in r["campaigns"])
    assert r["total"] == 400 and r["shown"] + r["omitted"] == 400 and r["omitted"] > 0
    assert len(json.dumps(r)) < 20000


def test_the_full_goal_never_appears():
    r = R.build_roster([_w()])
    assert "FULL GOAL" not in json.dumps(r)
    assert "FULL GOAL" not in "\n".join(R.render_lines(r))


def test_lines_for_campaign_only_that_campaign():
    ws = [_w("a", cid="c1"), _w("b", cid="c2")]
    assert len(R.lines_for_campaign(ws, "c1")) == 1 and "a [" in R.lines_for_campaign(ws, "c1")[0]
    assert R.lines_for_campaign(ws, "") == [] and R.lines_for_campaign(ws, "zzz") == []
    assert R.lines_for_campaign(ws, None) == []
    many = [_w("w%d" % i, cid="c1") for i in range(8)]
    assert len(R.lines_for_campaign(many, "c1", max_lines=3)) == 3


# ------------------------------------------------------------------ never stored

def test_a_roster_is_never_written_to_disk(tmp_path, monkeypatch):
    real_open = builtins.open
    opened_for_write = []

    def guard(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            opened_for_write.append(file)
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", guard)
    monkeypatch.chdir(tmp_path)
    ws = [_w("w%d" % i, cid="c%d" % (i % 3)) for i in range(30)]
    r = R.build_roster(ws)
    R.roster_block(ws)
    R.render_lines(r)
    R.lines_for_campaign(ws, "c1")
    assert opened_for_write == [] and os.listdir(str(tmp_path)) == []
    src = open(R.__file__, encoding="utf-8").read()
    assert not re.search(r"\bopen\(|\.write|os\.replace|os\.rename|makedirs|json\.dump\b", src)


# ------------------------------------------------------------------ the snapshot

def test_the_snapshot_carries_the_roster_through_its_production_caller():
    from relay import fleet_runner as FR
    ws = [_w("w1"), _w("w2", cid="")]
    assert FR._roster_block(ws)["roster"]["total"] == 2
    assert FR._roster_block([]) == {}
    assert FR._roster_block(object()) == {}


# ------------------------------------------------------------------ the board digest

@pytest.fixture
def board(tmp_path, monkeypatch):
    AB._reset_for_test()
    monkeypatch.setattr(AB, "BOARD_PATH", str(tmp_path / "fleet" / "board.jsonl"))
    for i in range(4):
        ok, _ = AB.post("cA", "fact", "w%d" % i, "worker", {"subject": "s%d" % i, "text": "t%d" % i},
                        now=NOW + i)
        assert ok
    yield
    AB._reset_for_test()


def test_without_roster_lines_the_digest_is_byte_identical(board):
    base = AB.digest("cA", "wx", now=NOW + 600)
    assert AB.digest("cA", "wx", now=NOW + 600, roster_lines=()) == base
    assert AB.digest("cA", "wx", now=NOW + 600, roster_lines=[]) == base
    assert AB.digest("cA", "wx", now=NOW + 600, roster_lines=None) == base
    assert " 担当(参考):" not in base and base.endswith(AB.DATA_LINE)
    # and the literal text the function has always produced for this board
    lines = [AB._line(e, NOW + 600) for e in AB.active_entries("cA", now=NOW + 600)[0]]
    assert base == "\n".join(["[board cA] 有効 4件"] + lines[:3] + [" (他1件)", AB.DATA_LINE])


def test_roster_lines_are_added_before_the_data_line_and_dropped_first_when_long(board):
    ros = R.lines_for_campaign([_w("w9", cid="cA")], "cA")
    d = AB.digest("cA", "wx", now=NOW + 600, roster_lines=ros)
    assert ros[0] in d and d.endswith(AB.DATA_LINE) and len(d) <= AB.DIGEST_CHARS
    assert d.index(" 担当(参考):") < d.index(AB.DATA_LINE)
    base = AB.digest("cA", "wx", now=NOW + 600)
    long_ros = [" x" * 300] * 5
    for n in (AB.DIGEST_MIN_CHARS, 200, 400):
        out = AB.digest("cA", "wx", now=NOW + 600, max_chars=n, roster_lines=long_ros)
        assert len(out) <= n and out.endswith(AB.DATA_LINE)
        assert out == AB.digest("cA", "wx", now=NOW + 600, max_chars=n)   # roster went first


def test_the_tool_adds_the_roster_only_when_the_mode_is_p0_or_p1(tmp_path, monkeypatch):
    from relay import task_router as TR
    (tmp_path / "status.json").write_text(
        json.dumps({"workers": [_w("w1", cid="cA"), _w("w2", cid="cB")]}), encoding="utf-8")
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path))
    for mode, expect in (("off", False), ("p0", True), ("p1", True)):
        monkeypatch.setattr(AB, "setting_mode", lambda m=mode: m)
        got = board_tool._roster_lines("cA")
        assert bool(got) is expect
        if expect:
            assert len(got) == 1 and got[0].startswith(" w1 [")
    monkeypatch.setattr(AB, "setting_mode", lambda: "p0")
    (tmp_path / "status.json").write_text("{not json", encoding="utf-8")
    assert board_tool._roster_lines("cA") == []
    os.remove(str(tmp_path / "status.json"))
    assert board_tool._roster_lines("cA") == []


def test_board_digest_passes_the_roster_to_the_digest(monkeypatch):
    seen = {}
    monkeypatch.setattr(board_tool, "_caller", lambda ts=None: ("cA", "w1"))
    monkeypatch.setattr(board_tool, "_roster_lines", lambda cid: [" r1"])
    monkeypatch.setattr(AB, "digest", lambda cid, viewer, **k: seen.update(k) or "D")
    assert board_tool.board_digest() == "D" and seen == {"roster_lines": [" r1"], "kind": None, "subject": None}


# ------------------------------------------------------------------ the cockpit

def test_the_cockpit_part_is_listed_and_wired():
    ps1 = open(os.path.join(REPO, "ui", "rebuild_ui.ps1"), encoding="utf-8", errors="replace").read()
    build_line = [l for l in ps1.splitlines() if l.lstrip().startswith('Build "FleetCockpit"')]
    assert len(build_line) == 1 and '"FleetCockpit.Roster.cs"' in build_line[0]
    assert "FleetCockpit.Roster.cs" in [os.path.basename(p) for p in ui_source.part_paths("FleetCockpit")]
    src = ui_source.read_parts("FleetCockpit", encoding="utf-8", errors="replace")
    assert src.count("void ApplyRosterToChip(") == 1 and src.count("ApplyRosterToChip(root)") == 1
    assert 'Obj(root, "roster")' in src and '"goal"' not in open(
        os.path.join(REPO, "ui", "FleetCockpit.Roster.cs"), encoding="utf-8").read()
    # Japanese text in it, so it must start with the UTF-8 byte order mark csc relies on
    assert open(os.path.join(REPO, "ui", "FleetCockpit.Roster.cs"), "rb").read(3) == b"\xef\xbb\xbf"
