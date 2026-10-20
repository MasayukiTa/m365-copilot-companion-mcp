# -*- coding: utf-8 -*-
"""The refusal matrix of the resident guard, its one-resident lock, and the setting reader."""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import resident_guard as G  # noqa: E402

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
AGENT = "agent-aaaa"


def conv(**kw):
    base = {"owner_oid": ME, "agent_id": AGENT, "last_message": {"role": "assistant", "block_id": "blk-1"}}
    base.update(kw)
    return base


def test_the_own_conversation_with_a_new_block_may_run():
    assert G.may_execute(conv(), ME, AGENT, set()) == (True, G.OK)


def test_ids_compare_case_and_space_insensitively():
    assert G.may_execute(conv(owner_oid=ME.upper() + " "), ME, AGENT, []) == (True, G.OK)


@pytest.mark.parametrize("c,user,agent,ledger,reason", [
    # condition 1: the owner
    (conv(owner_oid=OTHER), ME, AGENT, set(), G.OWNER_MISMATCH),
    (conv(owner_oid=None), ME, AGENT, set(), G.OWNER_UNREADABLE),
    (conv(owner_oid=""), ME, AGENT, set(), G.OWNER_UNREADABLE),
    (conv(owner_oid=12345), ME, AGENT, set(), G.OWNER_UNREADABLE),
    (conv(), "", AGENT, set(), G.CONFIGURED_USER_MISSING),
    (conv(), None, AGENT, set(), G.CONFIGURED_USER_MISSING),
    # a conversation with no owner must not match a PC with no configured user
    (conv(owner_oid=None), None, AGENT, set(), G.CONFIGURED_USER_MISSING),
    # condition 2: the agent
    (conv(agent_id="agent-bbbb"), ME, AGENT, set(), G.AGENT_MISMATCH),
    (conv(agent_id=None), ME, AGENT, set(), G.AGENT_UNREADABLE),
    (conv(), ME, "", set(), G.CONFIGURED_AGENT_MISSING),
    (conv(agent_id=None), ME, None, set(), G.CONFIGURED_AGENT_MISSING),
    # condition 3: the block
    (conv(), ME, AGENT, {"blk-1"}, G.ALREADY_EXECUTED),
    (conv(), ME, AGENT, ["blk-1"], G.ALREADY_EXECUTED),
    (conv(), ME, AGENT, {"blk-1": "done"}, G.ALREADY_EXECUTED),
    (conv(last_message={"role": "assistant"}), ME, AGENT, set(), G.BLOCK_ID_UNREADABLE),
    (conv(last_message={"role": "assistant", "block_id": ""}), ME, AGENT, set(), G.BLOCK_ID_UNREADABLE),
    (conv(last_message={"role": "assistant", "block_id": 7}), ME, AGENT, set(), G.BLOCK_ID_UNREADABLE),
    (conv(), ME, AGENT, None, G.LEDGER_UNREADABLE),
    (conv(), ME, AGENT, 5, G.LEDGER_UNREADABLE),
    # the last message must be the model's
    (conv(last_message={"role": "user", "block_id": "blk-1"}), ME, AGENT, set(), G.LAST_MESSAGE_NOT_MODEL),
    (conv(last_message=None), ME, AGENT, set(), G.LAST_MESSAGE_NOT_MODEL),
    # no conversation at all
    (None, ME, AGENT, set(), G.NO_CONVERSATION),
    ({}, ME, AGENT, set(), G.NO_CONVERSATION),
    ("a string", ME, AGENT, set(), G.NO_CONVERSATION),
])
def test_every_refusal_has_its_named_reason(c, user, agent, ledger, reason):
    ok, why = G.may_execute(c, user, agent, ledger)
    assert (ok, why) == (False, reason)
    assert why in G.REASONS


def test_a_ledger_that_raises_is_a_refusal_not_a_crash():
    class Boom:
        def __contains__(self, item):
            raise RuntimeError("unreadable")
    assert G.may_execute(conv(), ME, AGENT, Boom()) == (False, G.LEDGER_UNREADABLE)


def test_the_owner_is_checked_before_the_ledger():
    # another person's already-run block reports the owner, the cheaper and more serious fact
    assert G.may_execute(conv(owner_oid=OTHER), ME, AGENT, {"blk-1"}) == (False, G.OWNER_MISMATCH)


# ------------------------------------------------------------------ the lock

def test_the_lock_is_named_by_a_hash_not_the_oid(tmp_path):
    p = G.lock_path(str(tmp_path), ME)
    assert ME not in p and os.path.dirname(p) == str(tmp_path)
    assert p != G.lock_path(str(tmp_path), OTHER)
    assert p == G.lock_path(str(tmp_path), ME.upper())


def test_a_second_live_resident_for_the_same_user_is_refused(tmp_path):
    a = G.ResidentLock(str(tmp_path), ME, pid=1001, alive=lambda pid: True)
    b = G.ResidentLock(str(tmp_path), ME, pid=1002, alive=lambda pid: True)
    assert a.acquire() == (True, G.LOCK_TAKEN)
    assert b.acquire() == (False, G.LOCK_HELD)
    assert a.acquire() == (True, G.LOCK_OWN)


def test_a_different_user_has_its_own_lock(tmp_path):
    a = G.ResidentLock(str(tmp_path), ME, pid=1001, alive=lambda pid: True)
    b = G.ResidentLock(str(tmp_path), OTHER, pid=1002, alive=lambda pid: True)
    assert a.acquire()[0] and b.acquire()[0]


def test_a_lock_of_a_dead_pid_is_stale_and_taken_over(tmp_path):
    a = G.ResidentLock(str(tmp_path), ME, pid=1001, alive=lambda pid: True)
    assert a.acquire()[0]
    b = G.ResidentLock(str(tmp_path), ME, pid=1002, alive=lambda pid: pid != 1001)
    assert b.acquire() == (True, G.LOCK_STALE_TAKEN)
    assert json.load(open(b.path))["pid"] == 1002


def test_a_lock_file_that_names_no_pid_is_stale(tmp_path):
    b = G.ResidentLock(str(tmp_path), ME, pid=1002, alive=lambda pid: True)
    with open(b.path, "w") as fh:
        fh.write("not json")
    assert b.acquire() == (True, G.LOCK_STALE_TAKEN)


def test_release_removes_only_our_own_lock(tmp_path):
    a = G.ResidentLock(str(tmp_path), ME, pid=1001, alive=lambda pid: True)
    b = G.ResidentLock(str(tmp_path), ME, pid=1002, alive=lambda pid: True)
    a.acquire()
    assert b.release() == (False, G.LOCK_NOT_OURS)
    assert os.path.exists(a.path)
    assert a.release() == (True, G.LOCK_RELEASED)
    assert not os.path.exists(a.path)
    assert a.release() == (False, G.LOCK_NOT_OURS)


def test_no_user_no_lock(tmp_path):
    assert G.ResidentLock(str(tmp_path), "", pid=1).acquire() == (False, G.LOCK_NO_USER)
    assert os.listdir(str(tmp_path)) == []


def test_the_real_pid_probe_sees_this_process_and_not_a_dead_one(tmp_path):
    assert G._pid_alive(os.getpid()) is True
    assert G._pid_alive(2 ** 22 + 12345) is False


# ------------------------------------------------------------------ the setting

def _settings(tmp_path, text):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    G._reset_for_test()
    return str(p)


def test_the_setting_defaults_off_and_reads_only_on_or_off(tmp_path):
    assert G.mode(_settings(tmp_path, "maxtabs=3\n")) == "off"
    assert G.mode(_settings(tmp_path, "resident_tools=on\n")) == "on"
    assert G.mode(_settings(tmp_path, "resident_tools= ON \n")) == "on"
    assert G.mode(_settings(tmp_path, "resident_tools=maybe\n")) == "off"
    assert G.mode(str(tmp_path / "missing.txt")) == "off"


def test_status_block_reports_the_mode(tmp_path):
    assert G.status_block(_settings(tmp_path, "resident_tools=off\n")) == {"resident_tools": "off"}
    assert G.status_block(_settings(tmp_path, "resident_tools=on\n")) == {"resident_tools": "on"}
