# -*- coding: utf-8 -*-
"""The fleet sweep must not re-parse the campaign ledger per campaign (2026-10-14 watchdog resets).

`_stall_check` read the whole 10 MB campaigns.jsonl once per unmerged old campaign on every sweep
(~70 x 0.3 s = 30 s + 1 GB of reads). The index reads it once and then only appended bytes.
"""
import json
import os
import sqlite3
import time

from relay import fanout, fleet_resume as fr


def _row(**kw):
    return json.dumps(kw, ensure_ascii=False) + "\n"


def _write_ledger(path, n_camps=70, target_bytes=10 * 1024 * 1024):
    """n_camps unmerged families (header + children) padded with big result rows to ~10 MB."""
    pad = "x" * 4000
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n_camps):
            cid = "c%04d" % i
            fh.write(_row(kind="campaign", campaign_id=cid, goal="goal %d" % i, n=4, cwd="C:/x",
                          run_id="r%d" % (i % 3), ts=1000.0 + i))
            for k in range(4):
                fh.write(_row(campaign_id=cid, subtask_index=k, text="child %d" % k,
                              goal={"text": "child %d of %d" % (k, i)}))
        i = 0
        while fh.tell() < target_bytes:
            fh.write(_row(kind="child_result", campaign_id="c%04d" % (i % n_camps),
                          subtask_index=i % 4, outcome="DONE", result=pad))
            i += 1


def test_synthetic_ledger_all_stall_checks_under_one_second(tmp_path):
    p = tmp_path / fr.CAMPAIGNS_FILE
    _write_ledger(str(p))
    assert p.stat().st_size >= 10 * 1024 * 1024
    sd = str(tmp_path)
    fr.read_campaign(sd, "c0000")              # the one-time parse (not what is timed)
    t = time.time()
    for _lap in range(3):                      # three sweeps, 70 campaigns each
        for i in range(70):
            fam = fr.read_campaign(sd, "c%04d" % i)
            assert fam and fam["n"] == 4
    assert time.time() - t < 1.0


def test_incremental_equals_full_parse(tmp_path):
    p = tmp_path / fr.CAMPAIGNS_FILE
    sd = str(tmp_path)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(_row(kind="campaign", campaign_id="a", goal="g", n=2, run_id="r1"))
        fh.write(_row(campaign_id="a", subtask_index=0, text="t0"))
    assert set(fr.read_campaigns_view(sd)) == {"a"}
    # a marker that arrives BEFORE its header, and a header written later, are folded in order
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(_row(kind="merged", campaign_id="b", agg_key="k"))
        fh.write(_row(campaign_id="a", subtask_index=1, text="t1"))
    assert set(fr.read_campaigns_view(sd)) == {"a"}     # b has no header yet
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(_row(kind="campaign", campaign_id="b", goal="gb", n=1))
        fh.write(_row(kind="child_result", campaign_id="a", subtask_index=0, outcome="DONE"))
    inc = fr.read_campaigns(sd)
    with open(p, encoding="utf-8") as fh:
        full = fanout.campaigns_from_ledger(fh)
    assert inc == full
    assert inc["b"]["merged"] is True and inc["b"]["agg_key"] == "k"


def test_partial_tail_is_not_lost_or_doubled(tmp_path):
    p = tmp_path / fr.CAMPAIGNS_FILE
    sd = str(tmp_path)
    head = _row(kind="campaign", campaign_id="a", goal="g", n=1)
    child = _row(campaign_id="a", subtask_index=0, text="t0")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(head + child[:10])               # the writer died / is mid-write
    assert len(fr.read_campaigns_view(sd)["a"]["children"]) == 0
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(child[10:])
    assert len(fr.read_campaigns_view(sd)["a"]["children"]) == 1
    # a whole record without its newline is taken once, and the newline arriving later adds nothing
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(_row(campaign_id="a", subtask_index=1, text="t1").rstrip("\n"))
    assert len(fr.read_campaigns_view(sd)["a"]["children"]) == 2
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("\n")
    assert len(fr.read_campaigns_view(sd)["a"]["children"]) == 2


def test_truncation_rebuilds_and_missing_file_is_empty(tmp_path):
    p = tmp_path / fr.CAMPAIGNS_FILE
    sd = str(tmp_path)
    assert fr.read_campaigns_view(sd) == {}
    p.write_text(_row(kind="campaign", campaign_id="a", goal="g", n=1)
                 + _row(kind="campaign", campaign_id="b", goal="g2", n=1), encoding="utf-8")
    assert set(fr.read_campaigns_view(sd)) == {"a", "b"}
    p.write_text(_row(kind="campaign", campaign_id="z", goal="g", n=1), encoding="utf-8")
    assert set(fr.read_campaigns_view(sd)) == {"z"}
    assert fr.read_campaign(sd, "nope") is None


def test_read_campaigns_returns_private_copies(tmp_path):
    p = tmp_path / fr.CAMPAIGNS_FILE
    p.write_text(_row(kind="campaign", campaign_id="a", goal="g", n=1), encoding="utf-8")
    sd = str(tmp_path)
    fr.read_campaigns(sd)["a"]["merge_abandoned"] = True        # a caller mutating its result
    assert "merge_abandoned" not in fr.read_campaigns(sd)["a"]
    assert "merge_abandoned" not in fr.read_campaign(sd, "a")


def test_abandoned_merge_is_terminal_and_never_carried():
    fam = {"goal": "g", "n": 2, "merged": False, "merge_abandoned": True}
    assert fr.rehydrate_decision(fam, {}) == "drop"
    assert fr.rehydrate_decision({"goal": "g", "merged": False}, {}) == "carry"


def test_worker_rows_prefix_query_matches_substr_semantics_and_uses_index():
    from bridge import validity_audit as va
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE fleet_turns (id INTEGER PRIMARY KEY, key TEXT, name TEXT, turn INTEGER, "
        "role TEXT, text TEXT, extra TEXT, ts REAL);"
        "CREATE INDEX fleet_turns_key_idx ON fleet_turns(key, id);")
    key = "run1_w3"
    keys = [key, key + "__refuter_a", key + "__refuter_\u65e5", key + "__refuter", key + "_x",
            key + "__refuterZ", "run1_w30", "other"]
    for i, k in enumerate(keys):
        conn.execute("INSERT INTO fleet_turns (key, name, turn, role, text, extra, ts) "
                     "VALUES (?, 'n', 0, 'r', 't', '', ?)", (k, float(i)))
    got = {r["key"] for r in va._worker_rows(conn, key)}
    prefix = key + "__refuter_"
    want = {k for k in keys if k == key or k.startswith(prefix)}
    assert got == want == {key, key + "__refuter_a", key + "__refuter_\u65e5"}
    plan = " ".join(str(tuple(r)) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM fleet_turns WHERE key = ? OR (key >= ? AND key < ?)",
        (key, prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1))))
    assert "fleet_turns_key_idx" in plan and "SCAN" not in plan
