"""The job authority: is the boundary the one the docstring describes?

The authority exists so the judge does not read evidence the solver could have written. It
is a CAPABILITY boundary, not a sandbox -- an agent running as the same OS user is not
contained by it, and that limit is stated rather than hidden. These tests are about the
claims it does make: that the two tokens separate what an agent may do from what a judge may
do, that a receipt cannot be forged, and that one cannot quietly go missing.

It had no tests of its own until an independent review pointed out that its secrets were on
the child's command line -- readable by exactly the process it was meant to be a boundary
against -- and that its verification would accept a truncated receipt list. Both are checked
here now, which is the point of writing them down.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from bench.companionbench.job_authority import (AGENT_OPERATIONS, JUDGE_OPERATIONS,
                                                JobAuthority)


@pytest.fixture()
def authority():
    with JobAuthority() as auth:
        yield auth


#: The shape LocalJobStore accepts. Mirrors the runtime episodes' fixture; kept local so a
#: change to theirs cannot silently make these tests exercise something else.
def _spec(job_id="cb_auth"):
    return {"job_id": job_id, "execution_profile": "LOCAL_LOOP", "data_location": "LOCAL",
            "requires_local_tool": True,
            "task": {"type": "file_write", "instruction": "authority test"},
            "constraints": {"max_turns": 4, "allowed_base": ".", "allow_shell": False}}


def _job(auth, job_id="cb_auth"):
    auth.as_judge("create_job", job=_spec(job_id))
    return job_id


# ---- the token boundary -----------------------------------------------------------------

def test_an_agent_cannot_create_the_job_it_is_supposed_to_find(authority):
    """自分で仕事を作れるなら、『仕事を見つけた』は測定ではない。"""
    out = authority.as_agent("create_job", job=_spec("cb_mine"))
    assert "error" in out and "judge-only" in out["error"]


def test_an_agent_cannot_read_the_receipts_it_is_measured_by(authority):
    assert "judge-only" in authority.as_agent("receipts").get("error", "")


def test_an_agent_cannot_read_the_raw_state(authority):
    assert "judge-only" in authority.as_agent("state").get("error", "")


def test_an_agent_may_do_the_five_things_the_protocol_needs(authority):
    """境界が仕事を妨げるなら、それは境界ではなく不具合。"""
    job = _job(authority)
    claimed = authority.as_agent("claim_turn", job_id=job, expected_seq=1, worker_id="w1")
    assert "error" not in claimed, claimed
    assert "error" not in authority.as_agent("get_job_status", job_id=job)


def test_an_unknown_operation_is_refused_rather_than_dispatched(authority):
    """getattr(STORE, op) は、許可した5つ以外にも当たってしまう。"""
    out = authority.as_agent("__class__")
    assert "no such operation" in out.get("error", "")


def test_a_bad_token_reaches_nothing(authority):
    job = _job(authority)
    body = json.dumps({"op": "claim_turn",
                       "args": {"job_id": job, "expected_seq": 1, "worker_id": "w1"},
                       "token": "not-a-token"})
    import urllib.error
    import urllib.request
    req = urllib.request.Request(authority.url, data=body.encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 403


def test_the_two_operation_lists_do_not_overlap():
    """片方に足したつもりが両方に効く、を防ぐ。"""
    assert not set(AGENT_OPERATIONS) & set(JUDGE_OPERATIONS)


# ---- the secrets ------------------------------------------------------------------------

def test_no_secret_is_visible_in_the_child_command_line(authority):
    """DBパス・両トークン・HMAC鍵が argv にあった。同一OSユーザのプロセスは
    プロセス一覧を読むだけでそれを全部得られる -- 境界と呼べるものではない。"""
    if sys.platform != "win32":
        cmdlines = subprocess.run(["ps", "-ww", "-o", "args="], capture_output=True,
                                  text=True).stdout
    else:
        cmdlines = subprocess.run(
            ["wmic", "process", "get", "commandline"],
            capture_output=True, text=True).stdout or ""
        if not cmdlines.strip():
            cmdlines = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True).stdout or ""
    if not cmdlines.strip():
        pytest.skip("cannot read the process table on this host")
    for secret in (authority.agent_token, authority.judge_token,
                   authority._secret, authority.db):
        assert secret not in cmdlines, "秘匿値がプロセス一覧に出ている"


def test_the_database_is_outside_the_workdir_and_removed_afterwards(tmp_path):
    """証拠が作業ディレクトリの中にあるなら、それは解答者が書けるファイル。"""
    with JobAuthority() as auth:
        _job(auth)
        db, root = auth.db, auth.root
        assert os.path.isfile(db)
        assert not os.path.abspath(db).startswith(os.path.abspath(str(tmp_path)))
    assert not os.path.exists(root), "エピソード終了後も証拠が残っている"


# ---- the receipts -----------------------------------------------------------------------

def test_every_operation_leaves_a_receipt(authority):
    job = _job(authority)
    authority.as_agent("claim_turn", job_id=job, expected_seq=1, worker_id="w1")
    ops = [r["op"] for r in authority.receipts()]
    assert ops == ["create_job", "claim_turn"]


def test_a_failed_operation_also_leaves_one(authority):
    """失敗したことは、起きなかったことではない。"""
    authority.as_agent("claim_turn", job_id="no-such-job", expected_seq=1,
                       worker_id="w1")
    assert [r["ok"] for r in authority.receipts()] == [False]


def test_an_edited_receipt_breaks_the_chain(authority, monkeypatch):
    job = _job(authority)
    real = authority.as_judge

    def tampered(op, **args):
        out = real(op, **args)
        if op == "receipts":
            out["receipts"][0]["args"] = {"goal": "something else"}
        return out

    monkeypatch.setattr(authority, "as_judge", tampered)
    assert authority.receipts_intact() is False
    assert job


def test_a_removed_receipt_is_detected(authority, monkeypatch):
    """検証は返ってきた一覧から始まっていた。末尾を1件削った一覧は完璧に検証を通る。"""
    _job(authority)
    real = authority.as_judge

    def truncated(op, **args):
        out = real(op, **args)
        if op == "receipts":
            out["receipts"] = out["receipts"][:-1]
        return out

    monkeypatch.setattr(authority, "as_judge", truncated)
    assert authority.receipts_intact() is False


def test_an_empty_receipt_list_is_detected(authority, monkeypatch):
    _job(authority)
    real = authority.as_judge

    def emptied(op, **args):
        out = real(op, **args)
        if op == "receipts":
            out["receipts"] = []
        return out

    monkeypatch.setattr(authority, "as_judge", emptied)
    assert authority.receipts_intact() is False


def test_an_untouched_chain_verifies(authority):
    job = _job(authority)
    authority.as_agent("claim_turn", job_id=job, expected_seq=1, worker_id="w1")
    assert authority.receipts_intact() is True


def test_the_prompt_fragment_names_the_url_and_not_the_secrets(authority):
    text = authority.prompt_fragment(job_id="j1")
    assert authority.url in text
    assert authority.judge_token not in text
    assert authority._secret not in text
