# -*- coding: utf-8 -*-
"""The fan-out budget must not turn a big campaigns ledger into "usage unknown".

The ledger used to be refused whole past 2 MB, so on a machine that had accumulated a few dozen
finished campaigns every split failed closed ("budget tree usage unknown") and fan-out was off.
These tests pin the repair: the ledger is streamed (size is no longer a reason to refuse), a
top-level split never needs it, a nested split reads only its own root's rows, and for a small
ledger the answers are exactly what the old whole-file reader produced.
"""
from __future__ import annotations

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fanout_budget as fb  # noqa: E402
from relay import fleet_retention as FRET  # noqa: E402

NOW = 1_800_000_000.0
LIM = fb.default_limits()
STEPS = ["step number %d does a distinct slice of the work" % i for i in range(6)]


def _hdr(cid, root=None, n=3, ts=NOW - 600):
    r = {"kind": "campaign", "campaign_id": cid, "goal": "g" * 200, "n": n, "ts": ts}
    if root:
        r["root_id"] = root
    return r


def _child(cid, i):
    return {"campaign_id": cid, "task_id": "t%d" % i, "subtask_index": i,
            "text": "x" * 4000, "goal": {"text": "y" * 3000, "metadata": {"root_id": cid}}}


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _big_ledger(path, campaigns=80, kids=12):
    rows = []
    for c in range(campaigns):
        cid = "old%03d" % c
        rows.append(_hdr(cid, n=kids, ts=NOW - 86400 * 3 - c))
        rows.extend(_child(cid, i) for i in range(kids))
        rows.append({"kind": "child_result", "campaign_id": cid, "subtask_index": 0})
        rows.append({"kind": "merge_done", "campaign_id": cid})
    _write(path, rows)
    return rows


def _old_reader(path):
    """The reader this fix replaced, minus its size cap: every line parsed into a dict."""
    out = []
    with open(path, encoding="utf-8-sig") as fh:
        for ln in fh.read().splitlines():
            try:
                v = json.loads(ln)
            except ValueError:
                continue
            if isinstance(v, dict):
                out.append(v)
    return out


def test_a_six_megabyte_ledger_does_not_block_a_first_level_split(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    _big_ledger(str(p))
    assert os.path.getsize(p) > 6_000_000
    rows = fb.ledger_rows_for_split(str(p), "")            # top-level: new root, no read at all
    assert rows == []
    use, why = fb.apply_budget(STEPS, "croot-new", [], rows, LIM, min_children=2, now=NOW)
    assert use == STEPS and why == ""


def test_a_first_level_split_does_not_even_open_the_ledger(tmp_path, monkeypatch):
    p = tmp_path / "campaigns.jsonl"
    p.write_bytes(b"\xff\xfe not even readable \x00")
    monkeypatch.setattr(fb, "_header_index", lambda path: (_ for _ in ()).throw(AssertionError))
    assert fb.ledger_rows_for_split(str(p), "") == []


def test_a_nested_split_obeys_the_limits_of_its_own_root_on_a_big_ledger(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    rows = _big_ledger(str(p))
    rows.append(_hdr("rootA", n=4, ts=NOW - 60))
    rows.append(_hdr("nestedA", root="rootA", n=20, ts=NOW - 30))
    _write(str(p), rows)
    got = fb.ledger_rows_for_split(str(p), "rootA")
    assert sorted(r["campaign_id"] for r in got) == ["nestedA", "rootA"]
    # 4 + 20 queued children of this tree against a total of 24 with one slot held for the merge
    use, why = fb.apply_budget(STEPS, "rootA", [], got, LIM, min_children=2, now=NOW)
    assert use == [] and "24" in why
    # a root with room is granted in full, on the same big file
    rows.append(_hdr("rootB", n=3, ts=NOW - 60))
    _write(str(p), rows)
    use, why = fb.apply_budget(STEPS, "rootB", [], fb.ledger_rows_for_split(str(p), "rootB"),
                               LIM, min_children=2, now=NOW)
    assert use == STEPS and why == ""


def test_unreadable_usage_refuses_a_nested_split_only(tmp_path):
    missing_but_unreadable = str(tmp_path)                 # a directory: exists, cannot be read
    nested = fb.ledger_rows_for_split(missing_but_unreadable, "rootA")
    assert nested is None
    use, why = fb.apply_budget(STEPS, "rootA", [], nested, LIM, min_children=2, now=NOW)
    assert use == [] and "usage unknown" in why
    top = fb.ledger_rows_for_split(missing_but_unreadable, "")
    use, why = fb.apply_budget(STEPS, "croot", [], top, LIM, min_children=2, now=NOW)
    assert use == STEPS and why == ""


def test_a_big_ledger_is_never_unknown_because_of_its_size(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    _write(str(p), [_hdr("rootA")] + [_child("rootA", i) for i in range(3)])
    assert not hasattr(fb, "_MAX_CAMPAIGN_BYTES")
    rows = fb.ledger_rows_for_split(str(p), "rootA")
    assert rows is not None and len(rows) == 1
    assert fb.ledger_rows_for_split(str(p), "") == []


def test_small_ledgers_give_the_same_usage_as_the_whole_file_reader(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    rows = [_hdr("a", n=5), _child("a", 0), _hdr("n1", root="a", n=2, ts=NOW - 100),
            {"kind": "merged", "campaign_id": "a"}, _hdr("b", n=7, ts=NOW - 900)]
    _write(str(p), rows)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    workers = [{"root_id": "a", "status": "working", "turn": 3},
               {"root_id": "b", "status": "done", "turn": 1}]
    before = fb.usage_from_status(workers, _old_reader(str(p)), now=NOW)
    after = fb.usage_from_status(workers, fb.read_campaign_rows(str(p)), now=NOW)
    assert after == before
    for root in ("a", "b", "zzz"):
        mine = [w for w in workers if w["root_id"] == root]
        narrowed = fb.usage_from_status(
            mine, fb.read_campaign_rows(str(p), root_ids=[root]), now=NOW)
        for req in (2, 6, 12):
            assert fb.grant(root, req, before, LIM) == fb.grant(root, req, narrowed, LIM), (root, req)


def test_the_filter_does_not_leak_rows_of_similar_ids(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    _write(str(p), [_hdr("abc", n=2), _hdr("abc2", n=9), _hdr("xabc", n=8),
                    _hdr("kid", root="abc", n=4), _hdr("kid2", root="abc2", n=5)])
    got = fb.read_campaign_rows(str(p), root_ids=["abc"])
    assert sorted(r["campaign_id"] for r in got) == ["abc", "kid"]
    got = fb.read_campaign_rows(str(p), root_ids=["abc2"])
    assert sorted(r["campaign_id"] for r in got) == ["abc2", "kid2"]
    assert fb.read_campaign_rows(str(p), root_ids=["ab"]) == []
    use = fb.usage_from_status([], fb.read_campaign_rows(str(p), root_ids=["abc"]), now=NOW)
    assert use["abc"]["total"] == 6 and "abc2" not in use


def test_rows_appended_after_a_read_are_seen_and_a_torn_last_line_waits(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    _write(str(p), [_hdr("r1", n=2)])
    assert len(fb.read_campaign_rows(str(p), root_ids=["r1"])) == 1
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_hdr("kid", root="r1", n=3)))      # no newline yet: write in progress
    assert len(fb.read_campaign_rows(str(p), root_ids=["r1"])) == 1
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("\n")
    assert len(fb.read_campaign_rows(str(p), root_ids=["r1"])) == 2
    _write(str(p), [_hdr("r9", n=1)])                           # replaced by a shorter file
    assert fb.read_campaign_rows(str(p), root_ids=["r1"]) == []
    assert len(fb.read_campaign_rows(str(p), root_ids=["r9"])) == 1


def test_a_byte_order_mark_and_non_ascii_goals_are_read(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    with open(p, "w", encoding="utf-8-sig") as fh:
        fh.write(json.dumps(dict(_hdr("r1"), goal="社員名簿"), ensure_ascii=False) + "\n")
    assert [r["campaign_id"] for r in fb.read_campaign_rows(str(p))] == ["r1"]


def test_a_six_megabyte_ledger_is_read_well_inside_a_second(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    _big_ledger(str(p))
    t0 = time.perf_counter()
    rows = fb.read_campaign_rows(str(p), root_ids=["old079"])
    cold = time.perf_counter() - t0
    assert len(rows) == 1
    t1 = time.perf_counter()
    fb.read_campaign_rows(str(p), root_ids=["old000"])
    warm = time.perf_counter() - t1
    assert cold < 1.0, cold
    assert warm < 0.2, warm


def test_the_status_export_reads_a_big_ledger(tmp_path, monkeypatch):
    from relay import fleet_runner as FR
    p = tmp_path / "campaigns.jsonl"
    rows = _big_ledger(str(p))
    rows.append(_hdr("live", n=3, ts=time.time() - 300))
    _write(str(p), rows)
    assert os.path.getsize(p) > 6_000_000
    monkeypatch.setattr(FR, "_ACTIVE_STATE_DIR", str(tmp_path))
    blk = FR._tree_budget_block([{"root_id": "live", "status": "working", "turn": 2}])
    t = blk["tree_budget"]["live"]
    assert t["known"] is True and t["total"] == 3 and 4.0 < t["wall_min"] < 6.0


def test_the_new_mechanism_rows_are_registered():
    from relay import mechanism_telemetry as mt
    assert "fanout_budget_usage_unknown" in mt.MECHANISMS
    assert "campaigns_ledger_large" in mt.MECHANISMS


def test_the_ledger_size_warning_fires_past_the_threshold_and_never_modifies_it(tmp_path, monkeypatch):
    from relay import mechanism_telemetry as mt
    monkeypatch.setattr(mt, "LOG", str(tmp_path / "mech.jsonl"))
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    p = fleet / "campaigns.jsonl"
    _write(str(p), [_hdr("r1")])
    assert FRET.campaigns_ledger_warning(str(fleet)) == 0
    assert not os.path.exists(tmp_path / "mech.jsonl")
    before = p.read_bytes()
    assert FRET.campaigns_ledger_warning(str(fleet), warn_mb=0.0001) == len(before)
    rows = mt.load(str(tmp_path / "mech.jsonl"))
    assert [r["mechanism"] for r in rows] == ["campaigns_ledger_large"]
    assert p.read_bytes() == before
    assert FRET.campaigns_ledger_warning(str(fleet), dry_run=True, warn_mb=0.0001) == len(before)
    assert len(mt.load(str(tmp_path / "mech.jsonl"))) == 1


def test_the_split_decision_asks_the_budget_for_its_rows():
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8-sig").read()
    assert "fanout_budget_mod.ledger_rows_for_split(" in src
    assert '_mt.record("fanout_budget_usage_unknown"' in src
