# -*- coding: utf-8 -*-
"""The campaign board (relay/agent_board.py, tools/board_tool.py), P0: schema, limits, scope,
digest, retention, failure safety, the no-op when off, and the caller rule.

Everything writes under tmp_path; nothing here touches the operator's .fleet.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import agent_board as AB  # noqa: E402
from relay import fanout  # noqa: E402
from relay import fleet_retention as FRET  # noqa: E402
import relay.relay_fleet as rf  # noqa: E402
from tools import board_tool  # noqa: E402

NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    AB._reset_for_test()
    monkeypatch.setattr(AB, "BOARD_PATH", str(tmp_path / "fleet" / "board.jsonl"))
    yield
    AB._reset_for_test()


def _settings(tmp_path, monkeypatch, text):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    return str(p)


def _rows(path=None):
    p = path or AB.BOARD_PATH
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _post(cid="cA", kind="fact", who="w1", role="worker", now=NOW, **fields):
    if not fields:
        fields = {"fact": {"subject": "s", "text": "t"},
                  "claim": {"subject": "s"},
                  "result": {"subject": "s", "ref": "r"},
                  "question": {"text": "t"}}[kind]
    return AB.post(cid, kind, who, role, fields, now=now)


# ------------------------------------------------------------------ the setting

@pytest.mark.parametrize("text,want", [
    ("agent_board_mode=p0\n", "p0"), ("agent_board_mode=P1\n", "p1"),
    ("agent_board_mode=off\n", "off"), ("agent_board_mode=on\n", "off"),
    ("agent_board_mode=\n", "off"), ("other=1\n", "off"), ("\ufeffagent_board_mode=p0\n", "p0"),
])
def test_the_mode_reads_off_p0_p1_and_nothing_else(tmp_path, text, want):
    p = tmp_path / "s.txt"
    p.write_text(text, encoding="utf-8")
    assert AB.setting_mode(str(p)) == want


def test_a_missing_settings_file_is_off(tmp_path):
    assert AB.setting_mode(str(tmp_path / "nope.txt")) == "off"


def test_the_mode_is_read_once_per_run_not_at_every_use(tmp_path, monkeypatch):
    p = _settings(tmp_path, monkeypatch, "agent_board_mode=p0\n")
    assert AB.begin_run() == "p0" and AB.active()
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("agent_board_mode=off\n")
    assert AB.run_mode() == "p0"              # sweep_start: this run keeps what it started with
    assert AB.begin_run() == "off" and not AB.active()


def test_no_run_means_off_and_no_status_block():
    assert AB.run_mode() == "off" and not AB.active() and AB.status_block() == {}


def test_p1_is_declared_and_behaves_as_p0(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "agent_board_mode=p1\n")
    assert AB.begin_run() == "p1" and AB.active()
    assert AB.post_result("cA", 1, "cA-1") is True
    assert AB.status_block()["agent_board"]["mode"] == "p1"
    assert not hasattr(AB, "board_post")      # no worker write path exists


# ------------------------------------------------------------------ schema

def test_a_well_formed_entry_is_stored_with_the_fixed_keys():
    assert _post("cA", "result", who="coordinator", role="coordinator",
                 subject="subtask 1", ref="task cA-1") == (True, "")
    (row,) = _rows()
    assert set(row) == {"v", "id", "ts", "campaign_id", "kind", "author", "author_role",
                        "expires_ts", "subject", "ref"}
    assert row["v"] == 1 and len(row["id"]) == 8 and row["author_role"] == "coordinator"


@pytest.mark.parametrize("kind,fields", [
    ("fact", {"subject": "s", "text": "t", "confidence": 0.9}),
    ("claim", {"subject": "s", "text": "t"}),
    ("question", {"text": "t", "subject": "s"}),
    ("result", {"subject": "s", "ref": "r", "author": "someone"}),
])
def test_an_unknown_key_is_refused_not_dropped(kind, fields):
    assert AB.post("cA", kind, "w1", "worker", fields, now=NOW) == (False, "unknown_key")
    assert _rows() == [] or all(r.get("event") == "deny" for r in _rows())


@pytest.mark.parametrize("kind", ["note", "", None, "FACT", "request"])
def test_only_the_four_kinds_exist(kind):
    assert AB.post("cA", kind, "w1", "worker", {"text": "t"}, now=NOW) == (False, "bad_kind")


@pytest.mark.parametrize("fields", [
    {"subject": "s"}, {"subject": "s", "text": ""}, {"subject": "s", "text": "   "},
    {"subject": "s", "text": 5}, {"subject": None, "text": "t"},
])
def test_missing_empty_or_non_string_fields_are_refused(fields):
    assert AB.post("cA", "fact", "w1", "worker", fields, now=NOW)[0] is False


def test_the_length_limits_are_exact():
    # each field passes at its limit; the whole row is then held to 400 bytes
    assert AB.post("cA", "fact", "w1", "worker", {"subject": "x" * 60, "text": "y" * 100},
                   now=NOW) == (True, "")
    assert AB.post("cA", "fact", "w2", "worker", {"subject": "x" * 60, "text": "y" * 200},
                   now=NOW) == (False, "too_large")
    assert AB.post("cA", "fact", "w1", "worker",
                   {"subject": "x" * 61, "text": "t"}, now=NOW) == (False, "too_long")
    assert AB.post("cA", "fact", "w1", "worker",
                   {"subject": "s", "text": "y" * 201}, now=NOW) == (False, "too_long")
    assert AB.post("cA", "result", "w1", "worker",
                   {"subject": "s", "ref": "y" * 201}, now=NOW) == (False, "too_long")


def test_control_characters_and_line_breaks_are_removed():
    assert _post(kind="fact", subject="a\nb\r\tc\x00d\x1b", text="line1\n\nline2\u2028end")[0]
    (row,) = [r for r in _rows() if r.get("kind") == "fact"]
    assert row["subject"] == "a b c d" and row["text"] == "line1 line2 end"


def test_a_value_that_is_only_control_characters_is_refused():
    assert AB.post("cA", "fact", "w1", "worker",
                   {"subject": "\n\t", "text": "t"}, now=NOW) == (False, "schema")


def test_no_campaign_or_no_identity_is_refused():
    assert AB.post("", "fact", "w1", "worker", {"subject": "s", "text": "t"})[1] == "no_campaign"
    assert AB.post("cA", "fact", "", "worker", {"subject": "s", "text": "t"})[1] == "no_identity"
    assert AB.post("cA", "fact", "w1", "admin", {"subject": "s", "text": "t"})[1] == "no_identity"
    assert _rows() == []


def test_a_malformed_or_foreign_line_is_skipped_when_reading():
    os.makedirs(os.path.dirname(AB.BOARD_PATH))
    good = {"v": 1, "id": "abcd1234", "ts": NOW, "campaign_id": "cA", "kind": "fact",
            "author": "w9", "author_role": "worker", "expires_ts": NOW + 100,
            "subject": "s", "text": "t"}
    bad = [dict(good, v=2), dict(good, kind="note"), dict(good, extra="x"),
           dict(good, author_role="root"), dict(good, expires_ts="soon"), {"nonsense": 1}]
    with open(AB.BOARD_PATH, "w", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        for b in bad:
            fh.write(json.dumps(b) + "\n")
        fh.write(json.dumps(good) + "\n")
        fh.write('{"v": 1, "id": "cut off\n')
    live, expired = AB.active_entries("cA", now=NOW)
    assert [e["author"] for e in live] == ["w9"] and expired == 0


# ------------------------------------------------------------------ limits

def test_a_campaign_holds_at_most_sixty_entries_and_the_refusal_is_recorded():
    for i in range(60):
        assert _post("cA", "result", who="coordinator", role="coordinator",
                     subject="subtask %d" % i, ref="task cA-%d" % i)[0], i
    assert _post("cA", "result", who="coordinator", role="coordinator",
                 subject="subtask 60", ref="task cA-60") == (False, "campaign_full")
    denies = [r for r in _rows() if r.get("event") == "deny"]
    assert len(denies) == 1 and denies[0]["reason"] == "campaign_full"
    assert denies[0]["campaign_id"] == "cA" and "text" not in denies[0]
    assert len([r for r in _rows() if r.get("event") != "deny"]) == 60
    assert AB.status_block() == {}                      # (no run began; counters still counted)
    assert AB._STATE["refused"] == 1


def test_refusals_are_recorded_only_up_to_a_bound():
    for _ in range(AB.DENY_ROWS_MAX + 15):
        AB.post("cA", "fact", "w1", "worker", {"subject": "s", "text": "t", "x": 1}, now=NOW)
    assert len([r for r in _rows() if r.get("event") == "deny"]) == AB.DENY_ROWS_MAX
    assert AB._STATE["refused"] == AB.DENY_ROWS_MAX + 15


def test_a_stored_row_is_at_most_four_hundred_bytes():
    ok, why = AB.post("c" * 100, "result", "w1", "worker",
                      {"subject": "あ" * 60, "ref": "い" * 200}, now=NOW)
    assert (ok, why) == (False, "too_large")
    ok, why = AB.post("cA", "result", "coordinator", "coordinator",
                      {"subject": "subtask 12", "ref": "task c7e01b58b1956-12"}, now=NOW)
    assert ok
    for r in _rows():
        if r.get("event") != "deny":
            assert len(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode()) + 1 <= 400


@pytest.mark.parametrize("kind,cap", [("claim", 3), ("result", 5), ("fact", 5), ("question", 2)])
def test_a_worker_has_a_ceiling_per_kind(kind, cap):
    for i in range(cap):
        assert _post("cA", kind, who="w1")[0], i
    assert _post("cA", kind, who="w1") == (False, "author_cap")
    assert _post("cA", kind, who="w2")[0] is True        # another worker's allowance is its own
    assert _post("cA", kind, who="w1", now=NOW + 1)[0] is False


def test_the_ceiling_counts_live_entries_only():
    for _ in range(3):
        assert _post("cA", "claim", who="w1")[0]
    assert _post("cA", "claim", who="w1", now=NOW + AB.CLAIM_TTL_S + 1)[0] is True


def test_the_coordinator_has_no_per_kind_ceiling_only_the_campaign_total():
    for i in range(12):
        assert _post("cA", "result", who="coordinator", role="coordinator",
                     subject="subtask %d" % i, ref="task cA-%d" % i)[0], i


def test_entries_expire_and_a_claim_expires_sooner():
    _post("cA", "claim", who="w1", now=NOW)
    _post("cA", "fact", who="w2", now=NOW)
    assert len(AB.active_entries("cA", now=NOW + 10)[0]) == 2
    live, expired = AB.active_entries("cA", now=NOW + AB.CLAIM_TTL_S + 1)
    assert [e["kind"] for e in live] == ["fact"] and expired == 1
    live, expired = AB.active_entries("cA", now=NOW + AB.TTL_S + 1)
    assert live == [] and expired == 2
    assert len([r for r in _rows() if r.get("event") != "deny"]) == 2      # expired stays on disk, for audit


# ------------------------------------------------------------------ scope

def test_another_campaign_never_appears_and_an_unknown_one_is_empty():
    _post("cA", "fact", who="w1", subject="mine", text="a-secret")
    _post("cB", "fact", who="w2", subject="theirs", text="b-secret")
    da = AB.digest("cA", now=NOW + 5)
    db = AB.digest("cB", now=NOW + 5)
    assert "a-secret" in da and "b-secret" not in da
    assert "b-secret" in db and "a-secret" not in db
    assert AB.digest("", now=NOW) == "" and AB.digest(None, now=NOW) == ""
    assert "有効 0件" in AB.digest("cC", now=NOW)


def test_the_tool_gives_nothing_to_a_caller_it_cannot_place(monkeypatch):
    _post("cA", "fact", who="w1")
    monkeypatch.setattr(board_tool, "_caller", lambda ts=None: ("", ""))
    assert board_tool.board_digest() == ""


def test_the_tool_reads_the_campaign_of_the_caller_and_hides_its_own(monkeypatch):
    _post("cA", "fact", who="w1", subject="from-w1", text="x")
    _post("cA", "fact", who="w2", subject="from-w2", text="x")
    _post("cB", "fact", who="w3", subject="from-w3", text="x")
    monkeypatch.setattr(board_tool, "_caller", lambda ts=None: ("cA", "w1"))
    out = board_tool.board_digest()
    assert "from-w2" in out and "from-w1" not in out and "from-w3" not in out


def test_the_caller_is_decided_from_the_ledger_identity_never_from_an_argument(monkeypatch):
    import inspect
    assert list(inspect.signature(board_tool.board_digest).parameters) == []
    from tools import tool_ledger, turn_context
    monkeypatch.setattr(tool_ledger, "session_fingerprint", lambda: "sess")
    monkeypatch.setattr(tool_ledger, "_identity_for",
                        lambda *a, **k: ("job", "w7", "window"))
    monkeypatch.setattr(turn_context, "identity_of", lambda w, ts: {"campaign_id": "cZ"})
    assert board_tool._caller() == ("cZ", "w7")
    monkeypatch.setattr(tool_ledger, "_identity_for", lambda *a, **k: ("job", "", "session"))
    assert board_tool._caller() == ("", "")              # a declared job id is not a campaign
    monkeypatch.setattr(tool_ledger, "_identity_for", lambda *a, **k: ("", "", "ambiguous"))
    monkeypatch.setattr(tool_ledger, "_ambiguous_campaign", lambda ts: "cQ")
    assert board_tool._caller() == ("cQ", "")
    monkeypatch.setattr(tool_ledger, "_identity_for", lambda *a, **k: ("", "", ""))
    assert board_tool._caller() == ("", "")


# ------------------------------------------------------------------ digest

def test_the_digest_always_ends_with_the_data_line_and_never_exceeds_n():
    for i in range(40):
        _post("cA", "fact", who="w%d" % (i % 9), subject="subject number %d" % i,
              text="a rather long sentence of text %d " % i * 5, now=NOW + i)
    for n in (120, 200, 400, 600, 1000):
        d = AB.digest("cA", max_chars=n, now=NOW + 100)
        assert len(d) <= n, (n, len(d))
        assert d.endswith(AB.DATA_LINE)
        assert d.startswith("[board cA]")
    assert "(他" in AB.digest("cA", max_chars=600, now=NOW + 100)


def test_n_below_the_minimum_is_raised_to_it():
    assert len(AB.digest("cA", max_chars=5, now=NOW)) <= AB.DIGEST_MIN_CHARS


def test_an_empty_board_is_still_wrapped_as_data():
    d = AB.digest("cA", now=NOW)
    assert d == "[board cA] 有効 0件\n" + AB.DATA_LINE


def test_newest_first_and_at_most_three_lines_per_kind():
    for i in range(6):
        _post("cA", "fact", who="w%d" % i, subject="s%d" % i, text="t", now=NOW + i * 60)
    _post("cA", "question", who="w1", text="why", now=NOW + 5)
    d = AB.digest("cA", now=NOW + 600)
    facts = [l for l in d.splitlines() if l.startswith(" fact")]
    assert len(facts) == 3
    assert "s5" in facts[0] and "s4" in facts[1] and "s3" in facts[2]
    assert any(l.startswith(" question") for l in d.splitlines())
    assert "(他3件)" in d and "有効 7件" in d


def test_the_viewers_own_entries_are_left_out():
    _post("cA", "fact", who="w1", subject="own", text="x")
    _post("cA", "fact", who="w2", subject="other", text="x")
    d = AB.digest("cA", viewer="w1", now=NOW + 5)
    assert "other" in d and "own" not in d and "有効 1件" in d


def test_a_line_is_cut_at_a_hundred_characters_and_has_no_line_break():
    _post("cA", "fact", who="w1", subject="s" * 60, text="y" * 150, now=NOW)
    for line in AB.digest("cA", now=NOW).splitlines():
        assert len(line) <= AB.DIGEST_LINE_CHARS or line == AB.DATA_LINE


def test_old_entries_are_counted_in_the_header_not_shown():
    _post("cA", "claim", who="w1", subject="stale-claim")
    _post("cA", "fact", who="w2", subject="fresh", text="x", now=NOW + 3000)
    d = AB.digest("cA", now=NOW + 3600)
    assert "stale-claim" not in d and "古い 1件は省略" in d and "fresh" in d


def test_the_digest_is_fast_on_a_large_file():
    os.makedirs(os.path.dirname(AB.BOARD_PATH))
    with open(AB.BOARD_PATH, "w", encoding="utf-8") as fh:
        for i in range(20000):
            fh.write(json.dumps({"v": 1, "id": "%08x" % i, "ts": NOW, "campaign_id": "c%d" % (i % 300),
                                 "kind": "fact", "author": "w", "author_role": "worker",
                                 "expires_ts": NOW + 999999, "subject": "s", "text": "t"}) + "\n")
    t = time.perf_counter()
    AB.digest("c7", now=NOW + 1)
    assert time.perf_counter() - t < 0.25           # the budget is 50 ms; this only catches a scan gone wrong
    assert os.path.getsize(AB.BOARD_PATH) > AB.READ_TAIL_BYTES    # and the tail bound was exercised


# ------------------------------------------------------------------ failure never raises

def test_a_write_failure_is_counted_and_never_raises(tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    bad = str(blocker / "sub" / "board.jsonl")           # a directory under a file
    ok, why = AB.post("cA", "fact", "w1", "worker", {"subject": "s", "text": "t"}, path=bad)
    assert (ok, why) == (False, "write_error") and AB._STATE["write_errors"] == 1
    assert AB.digest("cA", path=bad) == "[board cA] 有効 0件\n" + AB.DATA_LINE


def test_a_read_only_file_is_survived(tmp_path):
    p = tmp_path / "ro" / "board.jsonl"
    p.parent.mkdir()
    p.write_text("", encoding="utf-8")
    os.chmod(str(p), 0o444)
    try:
        r = AB.post("cA", "fact", "w1", "worker", {"subject": "s", "text": "t"}, path=str(p))
        assert r[0] in (True, False)                    # (a Windows read-only bit may or may not bite)
    finally:
        os.chmod(str(p), 0o666)


def test_enospc_is_survived(monkeypatch):
    import builtins
    real = builtins.open

    def full(path, mode="r", *a, **k):
        if str(path) == AB.BOARD_PATH and "a" in mode:
            raise OSError(28, "No space left on device")
        return real(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", full)
    assert AB.post("cA", "fact", "w1", "worker", {"subject": "s", "text": "t"}) \
        == (False, "write_error")
    AB.begin_run()
    AB._STATE["mode"] = "p0"
    assert AB.post_result("cA", 1, "cA-1") is False


@pytest.mark.parametrize("args", [(None, None, None, None, None), (1, 2, 3, 4, 5),
                                  ("cA", "fact", "w", "worker", "notadict"),
                                  ("cA", "fact", "w", "worker", None)])
def test_junk_never_raises(args):
    assert AB.post(*args)[0] is False


def test_junk_to_the_readers_never_raises():
    assert AB.digest(object()) == "" and AB.active_entries(5) == ([], 0)
    AB._STATE["mode"] = "p0"
    assert AB.post_result(None, None) in (True, False)
    AB._STATE["mode"] = "p0"
    assert AB.post_result(object(), object(), object(), object()) is False


# ------------------------------------------------------------------ post_result

def test_post_result_does_nothing_when_off(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "agent_board_mode=off\n")
    AB.begin_run()
    assert AB.post_result("cA", 1, "cA-1") is False
    assert not os.path.exists(os.path.dirname(AB.BOARD_PATH))      # off creates nothing


def test_post_result_points_at_the_answer_and_carries_none_of_it(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "agent_board_mode=p0\n")
    AB.begin_run()
    assert AB.post_result("cA", 3, "cA-3") is True
    (row,) = _rows()
    assert (row["kind"], row["author"], row["author_role"]) == ("result", "coordinator", "coordinator")
    assert row["subject"] == "subtask 3" and row["ref"] == "task cA-3"
    assert set(row) == {"v", "id", "ts", "campaign_id", "kind", "author", "author_role",
                        "expires_ts", "subject", "ref"}
    assert AB.post_result("cA", 4, None, ref="C:/out/part4.csv") is True
    assert _rows()[-1]["ref"] == "C:/out/part4.csv"
    assert AB.post_result("cA", 5) is True and _rows()[-1]["ref"] == "subtask 5"


def test_post_result_is_idempotent_per_subtask(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "agent_board_mode=p0\n")
    AB.begin_run()
    assert AB.post_result("cA", 1, "cA-1") is True
    assert AB.post_result("cA", 1, "cA-1") is False
    assert len(_rows()) == 1


def test_the_status_block_reports_what_the_run_applies(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "agent_board_mode=p0\n")
    AB.begin_run()
    AB.post_result("cA", 1, "cA-1")
    assert AB.status_block() == {"agent_board": {"mode": "p0", "posted": 1, "refused": 0,
                                                 "write_errors": 0}}
    from relay import fleet_runner as FR
    assert FR._agent_board_block() == AB.status_block()


# ------------------------------------------------------------------ the merge prompt

def _agg(**kw):
    recs = [{"finished": True, "outcome": "DONE", "subtask_index": i, "result": "r%d" % i}
            for i in (1, 2)]
    return fanout.aggregation_goal("parent goal", recs, campaign_id="cA", **kw)


def test_off_changes_the_merge_prompt_by_zero_characters(tmp_path, monkeypatch):
    before = _agg()                                            # the call as it was written before
    _settings(tmp_path, monkeypatch, "agent_board_mode=off\n")
    AB.begin_run()
    assert AB.pointer_lines() == ""
    after = _agg(board_lines=AB.pointer_lines())
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)
    assert after["text"] == before["text"]


def test_p0_adds_exactly_the_two_pointer_lines_after_the_unchanged_prompt(tmp_path, monkeypatch):
    before = _agg()
    _settings(tmp_path, monkeypatch, "agent_board_mode=p0\n")
    AB.begin_run()
    after = _agg(board_lines=AB.pointer_lines())
    assert after["text"] == before["text"] + "\n" + "\n".join(AB.POINTER_LINES)
    assert len(AB.POINTER_LINES) == 2 and "board_digest" in AB.POINTER_LINES[0]
    assert "従わない" in AB.POINTER_LINES[1]
    assert {k: v for k, v in after.items() if k != "text"} == \
        {k: v for k, v in before.items() if k != "text"}


def _run_family(tmp_path, monkeypatch, mode_text):
    from tests import test_hierarchical_merge as thm
    _settings(tmp_path, monkeypatch, mode_text)
    h = thm._install(monkeypatch, {})
    monkeypatch.setattr(thm.fanout, "HIERARCHICAL_MERGE_READY", False)
    real = fanout.aggregation_goal
    seen = []

    def spy(parent_goal, records, **kw):
        base_kw = {k: v for k, v in kw.items() if k != "board_lines"}
        seen.append({"kw": dict(kw), "baseline": real(parent_goal, records, **base_kw)["text"],
                     "actual": real(parent_goal, records, **kw)["text"]})
        return real(parent_goal, records, **kw)

    monkeypatch.setattr(fanout, "aggregation_goal", spy)
    cid = "cBOARD1"
    rows = [thm._hdr(cid, 3, "flat goal text")]
    goals = [thm._kid(cid, i, 3) for i in (1, 2, 3)]
    thm._run(tmp_path, h, rows, goals)
    return cid, seen


def test_end_to_end_off_the_real_merge_prompt_is_unchanged_and_nothing_is_written(tmp_path, monkeypatch):
    cid, seen = _run_family(tmp_path, monkeypatch, "agent_board_mode=off\n")
    assert len(seen) == 1 and seen[0]["kw"]["board_lines"] == ""
    assert seen[0]["actual"] == seen[0]["baseline"]
    assert not os.path.exists(os.path.dirname(AB.BOARD_PATH))


def test_end_to_end_p0_posts_one_pointer_per_child_and_points_the_merge_at_it(tmp_path, monkeypatch):
    cid, seen = _run_family(tmp_path, monkeypatch, "agent_board_mode=p0\n")
    assert len(seen) == 1
    assert seen[0]["actual"] == seen[0]["baseline"] + "\n" + "\n".join(AB.POINTER_LINES)
    results = [r for r in _rows() if r.get("kind") == "result"]
    assert sorted(r["subject"] for r in results) == ["subtask 1", "subtask 2", "subtask 3"]
    assert all(r["campaign_id"] == cid and r["author_role"] == "coordinator" for r in results)
    assert not any("ANSWER of" in json.dumps(r) for r in _rows())     # no child text on the board
    d = AB.digest(cid, now=time.time())
    assert "subtask 2 結果は task %s-2" % cid in d and d.endswith(AB.DATA_LINE)


def test_end_to_end_a_failing_board_does_not_change_what_the_coordinator_does(tmp_path, monkeypatch):
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(AB, "BOARD_PATH", str(blocker / "sub" / "board.jsonl"))
    cid, seen = _run_family(tmp_path, monkeypatch, "agent_board_mode=p0\n")
    assert len(seen) == 1                                  # the merge was still queued
    assert AB._STATE["write_errors"] == 3


# ------------------------------------------------------------------ retention

def test_the_retention_sweep_keeps_board_jsonl(tmp_path):
    d = tmp_path / "fleet"
    d.mkdir()
    p = d / "board.jsonl"
    AB.post("cA", "fact", "w1", "worker", {"subject": "s", "text": "t"}, now=NOW, path=str(p))
    body = p.read_bytes()
    old = time.time() - 400 * 86400
    os.utime(str(p), (old, old))
    rep = FRET.apply(str(d), now=time.time() + 400 * 86400, dry_run=False)
    assert p.exists() and p.read_bytes() == body
    assert not (d / "board.jsonl.gz").exists()
    assert "error" not in rep["rules"].get("cap_jsonl", {})


def test_the_retention_cap_still_bounds_it(tmp_path):
    d = tmp_path / "fleet"
    d.mkdir()
    p = d / "board.jsonl"
    p.write_bytes(b'{"x": 1}\n' * 20)
    FRET.cap_jsonl(str(d), dry_run=False, max_mb=0.0001)
    assert p.exists() and p.stat().st_size < 9 * 20


# ------------------------------------------------------------------ the caller rule

def _non_test_sources():
    out = subprocess.check_output(["git", "ls-files", "*.py"], cwd=REPO).decode("utf-8", "replace")
    for rel in out.splitlines():
        base = os.path.basename(rel)
        if base.startswith("test_") or rel.startswith("tests/") or base == "conftest.py":
            continue
        yield rel


def _names_used(rel, skip_defs=()):
    with open(os.path.join(REPO, rel), encoding="utf-8-sig", errors="replace") as fh:
        tree = ast.parse(fh.read())
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.alias):
            used.add(node.name.split(".")[-1])
    return used


def _public_functions(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]


def test_every_public_function_has_a_caller_that_is_not_a_test():
    sources = list(_non_test_sources())
    used_elsewhere = {rel: _names_used(rel) for rel in sources}
    for mod in ("relay/agent_board.py", "tools/board_tool.py"):
        for fn in _public_functions(mod):
            callers = [rel for rel, names in used_elsewhere.items() if fn in names and rel != mod]
            inside = False
            if not callers:
                with open(os.path.join(REPO, mod), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                for top in tree.body:
                    if isinstance(top, ast.FunctionDef) and top.name != fn:
                        for node in ast.walk(top):
                            if (isinstance(node, ast.Name) and node.id == fn) or \
                                    (isinstance(node, ast.Attribute) and node.attr == fn):
                                inside = True
            assert callers or inside, "%s.%s is called only by tests" % (mod, fn)


def test_the_production_callers_are_the_ones_the_board_is_wired_through():
    def src(rel):
        with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
            return fh.read()
    rf_src = src("relay/relay_fleet.py")
    assert rf_src.count("agent_board.post_result(") == 1
    assert rf_src.count("agent_board.begin_run()") == 1
    assert rf_src.count("board_lines=agent_board.pointer_lines()") == 1
    assert "agent_board.status_block" in src("relay/fleet_runner.py")
    assert "agent_board.digest(" in src("tools/board_tool.py")
    assert '"board_digest":' in src("relay/fleet_toolset.py")
    assert "from tools.board_tool import board_digest" in src("main.py")
    # the one write to the board from the coordinator sits right after the child's ledger line
    i = rf_src.index("agent_board.post_result(")
    assert '"kind": "child_result"' in rf_src[i - 700:i]
