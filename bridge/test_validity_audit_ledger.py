# -*- coding: utf-8 -*-
"""The validity audit ledger: a whole conversation, bound to a claim id, tagged by role.

Behaviour, not source text: a throwaway store and a throwaway tool ledger are filled the way the
fleet fills them (record_fleet_turn, record_refuter_turn, tool_events.jsonl lines), then the real
sync / backfill / read functions run and the rows are read back.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bridge import session_store as S  # noqa: E402
from bridge import validity_audit as V  # noqa: E402

KEY = "rtest0001_a0_w0"
T0 = 1_791_201_700.0


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(S.STORE_DIR_ENV, str(tmp_path / "db"))
    monkeypatch.delenv(S.FULLTEXT_MAX_CHARS_ENV, raising=False)
    return tmp_path


def _call(cid, tool, ts, args, worker="w0", task="job1"):
    return {"schema": 1, "event": "call", "id": cid, "ts": ts, "tool": tool, "task": task,
            "worker": worker, "args": {k: {"text": v, "len": len(v), "sha16": "x", "truncated": False}
                                       for k, v in args.items()}}


def _out(cid, ts, text):
    return {"schema": 1, "event": "outcome", "id": cid, "ts": ts, "ok": True, "error": "",
            "result": {"text": text, "len": len(text), "sha16": "y", "truncated": False}}


def _ledger(tmp_path, events):
    p = tmp_path / "tool_events.jsonl"
    p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                 encoding="utf-8")
    return str(p)


def _fill_worker(goal="validity_judge で 分析ID SS1 を判定して", key=KEY, with_refuter=False):
    S.record_fleet_turn(key, {"meta": True, "key": key, "name": "w0", "goal": goal, "ts": T0},
                        name="w0", goal=goal)
    S.record_fleet_turn(key, {"turn": 1, "role": "user", "text": "JOB TEXT", "ts": T0 + 1},
                        name="w0")
    S.record_wire_turn(key, 1, "PREAMBLE + JOB TEXT", name="w0", route="socket")
    S.record_fleet_turn(key, {"turn": 1, "role": "assistant", "text": "verdict GO", "ts": T0 + 30},
                        name="w0")
    if with_refuter:
        S.record_refuter_turn(key, "refuter_user", 0, "REVIEW PROMPT", lens="rootcause", name="w0",
                              ts=T0 + 40)
        S.record_refuter_turn(key, "refuter_assistant", 1, "UPHELD", lens="rootcause", name="w0",
                              ts=T0 + 50)
        S.record_refuter_turn(key, "refuter_verdict", 10000, "UPHELD: ", lens="rootcause",
                              name="w0", ts=T0 + 51)


def _roles(claim):
    return [(r["role_tag"], r["text"]) for r in S.validity_audit_ledger(claim)["rows"]]


def test_a_conversation_is_bound_to_its_claim_with_role_tags(store):
    _fill_worker(with_refuter=True)
    led = _ledger(store, [_call("c1", "validity_judge", T0 + 10, {"config_id": "SS1"}),
                          _out("c1", T0 + 12, '{"verdict": "GO"}'),
                          _call("c2", "validity_explain_reason", T0 + 13, {"code": "K_OK"}),
                          _out("c2", T0 + 14, "explained")])
    st = V.sync_worker(KEY, ledger_path=led, jid="job1",
                       outcome={"outcome": "DONE", "status": "done"}, reviewed=True)
    assert st["claims"] == ["SS1"] and st["inserted"] == len(S.validity_audit_ledger("SS1")["rows"])
    roles = [r for r, _t in _roles("SS1")]
    # (the wire row is stamped with the real clock by the writer, so it is checked by presence)
    assert [r for r in roles if r != "worker_prompt_wire"] == [
        "goal", "worker_prompt", "tool_call", "tool_result", "tool_call", "tool_result",
        "worker_reply", "refuter_prompt", "refuter_reply", "verdict", "outcome"]
    assert roles.count("worker_prompt_wire") == 1
    texts = dict(_roles("SS1"))
    assert texts["worker_prompt_wire"] == "PREAMBLE + JOB TEXT"
    assert texts["refuter_prompt"] == "REVIEW PROMPT"
    assert texts["tool_result"] in ('{"verdict": "GO"}', "explained")
    assert texts["outcome"].startswith("DONE")


def test_a_second_sync_adds_nothing(store):
    _fill_worker()
    led = _ledger(store, [_call("c1", "validity_judge", T0 + 10, {"config_id": "SS1"})])
    first = V.sync_worker(KEY, ledger_path=led, outcome={"outcome": "DONE"})
    again = V.sync_worker(KEY, ledger_path=led, outcome={"outcome": "DONE"})
    assert first["inserted"] > 0 and again["inserted"] == 0
    assert again["already_there"] == first["inserted"]


def test_a_conversation_without_a_claim_is_kept_as_unattributed(store):
    _fill_worker(goal="use validity_list_claims and tell me what exists")
    led = _ledger(store, [_call("c1", "validity_list_claims", T0 + 10, {})])
    st = V.sync_worker(KEY, ledger_path=led)
    assert st["claims"] == [V.UNATTRIBUTED]
    assert any(r == "worker_reply" for r, _t in _roles(V.UNATTRIBUTED))


def test_a_worker_that_never_touched_a_validity_tool_is_not_audited(store):
    _fill_worker(goal="summarise this spreadsheet")
    st = V.sync_worker(KEY, ledger_path=_ledger(store, []))
    assert st["audited"] is False and S.validity_audit_claims() == []


def test_a_review_before_the_recorder_existed_is_stated_not_silent(store):
    _fill_worker()
    led = _ledger(store, [_call("c1", "validity_judge", T0 + 10, {"config_id": "SS1"})])
    V.sync_worker(KEY, ledger_path=led, reviewed=True)
    rows = {r: t for r, t in _roles("SS1")}
    assert rows["refuter_prompt"] == V.NOT_RECORDED == rows["refuter_reply"]


def test_another_workers_calls_are_not_mixed_in(store):
    _fill_worker()
    led = _ledger(store, [_call("c1", "validity_judge", T0 + 10, {"config_id": "SS1"}),
                          _call("c9", "validity_judge", T0 + 11, {"config_id": "ZZ9"},
                                worker="w1", task="other")])
    V.sync_worker(KEY, ledger_path=led, jid="job1")
    assert [c["claim_id"] for c in S.validity_audit_claims()] == ["SS1"]


def test_calls_no_worker_owns_are_kept_under_their_claim_or_unattributed(store):
    led = _ledger(store, [_call("a1", "validity_judge", T0 + 5, {"config_id": "BA2"}, worker="",
                                task=""),
                          _out("a1", T0 + 6, "res"),
                          _call("a2", "validity_verify_lock", T0 + 7, {}, worker="", task="")])
    st = V.sync_unattributed_calls(led, T0, T0 + 100)
    assert st["inserted"] == 3
    assert [r for r, _t in _roles("BA2")] == ["tool_call", "tool_result"]
    assert [r for r, _t in _roles(V.UNATTRIBUTED)] == ["tool_call"]
    assert V.sync_unattributed_calls(led, T0, T0 + 100)["inserted"] == 0


def test_a_secret_in_the_conversation_is_not_stored(store, monkeypatch):
    secret = "canary-AUDIT-1a2b3c4d5e"
    monkeypatch.setenv("AUDIT_TEST_API_TOKEN", secret)
    _fill_worker(goal="validity_judge 分析ID SS1 token=%s" % secret)
    led = _ledger(store, [_call("c1", "validity_judge", T0 + 10, {"config_id": "SS1"})])
    V.sync_worker(KEY, ledger_path=led)
    blob = json.dumps(S.validity_audit_ledger("SS1", limit=1000, max_chars=10**7))
    assert secret not in blob


def test_the_ledger_read_is_bounded_and_says_what_it_left_out(store):
    S.append_validity_audit([{"claim_id": "SS1", "role_tag": "worker_reply", "ts": T0 + i,
                              "text": "x" * 100, "source_table": "t", "source_key": str(i)}
                             for i in range(10)])
    out = S.validity_audit_ledger("SS1", limit=4, max_chars=250)
    assert out["total_rows"] == 10 and out["returned_rows"] == 3 and out["omitted_rows"] == 7
    assert [len(r["text"]) for r in out["rows"]] == [100, 100, 50]
    assert out["rows"][2]["text_cut_to"] == 50


def test_the_published_tool_reads_the_ledger(store):
    from tools.auto.claim_audit_ledger import validity_audit_ledger
    S.append_validity_audit([{"claim_id": "SS1", "role_tag": "goal", "ts": T0, "text": "g",
                              "source_table": "t", "source_key": "1"}])
    assert json.loads(validity_audit_ledger("SS1"))["rows"][0]["role_tag"] == "goal"
    assert json.loads(validity_audit_ledger(""))["claims"][0]["claim_id"] == "SS1"


def test_the_backfill_script_is_idempotent_and_dry_run_writes_nothing(store, tmp_path):
    _fill_worker()
    led = _ledger(store, [_call("c1", "validity_judge", T0 + 10, {"config_id": "SS1"}),
                          _out("c1", T0 + 11, "r")])
    hist = tmp_path / "history.json"
    hist.write_text(json.dumps([{"run_id": "rtest0001_a0", "name": "w0", "outcome": "DONE",
                                "status": "done", "jid": "job1",
                                "phase_events": [{"event": "refuting"}]}]), encoding="utf-8")
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import validity_audit_backfill as B
    args = ["--since", str(T0 - 10), "--until", str(T0 + 100), "--ledger", led,
            "--history", str(hist)]
    assert B.main(args + ["--dry-run"]) == 0
    assert S.validity_audit_claims() == []
    assert B.main(args) == 0
    n = S.validity_audit_ledger("SS1", limit=1000)["total_rows"]
    assert n >= 7
    assert B.main(args) == 0
    assert S.validity_audit_ledger("SS1", limit=1000)["total_rows"] == n
    tags = [r for r, _t in _roles("SS1")]
    assert "outcome" in tags and "refuter_prompt" in tags     # the not_recorded rows
