# -*- coding: utf-8 -*-
"""The refusal rules for a resident component that runs tool blocks found in a person's own chat.

BACKGROUND (design: 設計_人が直接チャットしてもツールが動く経路_20261020.md, design D). A person who
chats directly in the Copilot app, Teams or the mobile app with the configured agent should get
the tools run on THIS PC. A component that watches conversations and runs the `call_tool` blocks it
finds there is only safe if it can never run a block that belongs to someone else's request. The
event this prevents: two people share one agent, B writes in B's app, and A's PC executes B's
block -- B's request would then run on A's machine, under A's server permissions.

THIS MODULE ONLY DECIDES. It is pure: it reads nothing from the network, runs nothing, and is not
connected to any product behaviour. Discovery of conversations and the loop that would call
`may_execute` are later slices. The setting `resident_tools` (off|on, default off) is read here
only so that status.json can report it.

THE THREE CONDITIONS (all must hold; anything unreadable counts as not holding):
  1. the conversation's owner oid equals the oid configured for this PC's user
  2. the conversation's agent id equals the configured agent id
  3. the block's id is not already in the executed ledger (a block runs at most once)
Plus the two that make 1-3 meaningful: the configured values must be non-empty, and the last
message must be the model's own (a block quoted back by the user is not a request to run).

ONE RESIDENT PER USER. A user with the product on two PCs would have both components run the same
block. `ResidentLock` is a lock file under the state directory, named by a hash of the oid, that
holds the owner pid; a lock whose pid is dead is stale and is taken over.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time

KEY = "resident_tools"
MODES = ("off", "on")
DEFAULT = "off"

# Named refusal reasons. Every refusal returns exactly one of these.
OK = "ok"
NO_CONVERSATION = "no_conversation"
CONFIGURED_USER_MISSING = "configured_user_missing"
OWNER_UNREADABLE = "owner_unreadable"
OWNER_MISMATCH = "owner_mismatch"
CONFIGURED_AGENT_MISSING = "configured_agent_missing"
AGENT_UNREADABLE = "agent_unreadable"
AGENT_MISMATCH = "agent_mismatch"
LAST_MESSAGE_NOT_MODEL = "last_message_not_model"
BLOCK_ID_UNREADABLE = "block_id_unreadable"
LEDGER_UNREADABLE = "ledger_unreadable"
ALREADY_EXECUTED = "already_executed"

REASONS = (OK, NO_CONVERSATION, CONFIGURED_USER_MISSING, OWNER_UNREADABLE, OWNER_MISMATCH,
           CONFIGURED_AGENT_MISSING, AGENT_UNREADABLE, AGENT_MISMATCH, LAST_MESSAGE_NOT_MODEL,
           BLOCK_ID_UNREADABLE, LEDGER_UNREADABLE, ALREADY_EXECUTED)

# Lock reasons.
LOCK_TAKEN = "lock_taken"
LOCK_STALE_TAKEN = "stale_lock_taken"
LOCK_HELD = "lock_held_by_other_pid"
LOCK_OWN = "lock_already_ours"
LOCK_NO_USER = "lock_user_missing"
LOCK_UNWRITABLE = "lock_unwritable"
LOCK_RELEASED = "lock_released"
LOCK_NOT_OURS = "lock_not_ours"


def _norm(value):
    """An id as a comparable string, or "" when it is not a usable id. Ids are GUIDs, so case is
    not significant; anything that is not a non-empty string is unreadable, never coerced."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def may_execute(conversation, configured_user_oid, configured_agent_id, executed_ledger):
    """(ok, reason). ok is True only when every condition holds; reason is OK or a name from
    REASONS. Never raises: any surprise is a refusal.

    `conversation` is a mapping with:
        owner_oid   the account the conversation belongs to
        agent_id    the agent the conversation talks to
        last_message  {"role": "assistant", "block_id": <id of the call_tool block in it>}
    `executed_ledger` is anything supporting `in` (set, list, dict) holding ids already run.
    """
    try:
        if not isinstance(conversation, dict) or not conversation:
            return False, NO_CONVERSATION
        want_user = _norm(configured_user_oid)
        if not want_user:
            return False, CONFIGURED_USER_MISSING
        want_agent = _norm(configured_agent_id)
        if not want_agent:
            return False, CONFIGURED_AGENT_MISSING
        owner = _norm(conversation.get("owner_oid"))
        if not owner:
            return False, OWNER_UNREADABLE
        if owner != want_user:
            return False, OWNER_MISMATCH
        agent = _norm(conversation.get("agent_id"))
        if not agent:
            return False, AGENT_UNREADABLE
        if agent != want_agent:
            return False, AGENT_MISMATCH
        last = conversation.get("last_message")
        if not isinstance(last, dict) or _norm(last.get("role")) != "assistant":
            return False, LAST_MESSAGE_NOT_MODEL
        block_id = last.get("block_id")
        if not isinstance(block_id, str) or not block_id.strip():
            return False, BLOCK_ID_UNREADABLE
        if executed_ledger is None or not hasattr(executed_ledger, "__contains__"):
            return False, LEDGER_UNREADABLE
        if block_id in executed_ledger or block_id.strip() in executed_ledger:
            return False, ALREADY_EXECUTED
        return True, OK
    except Exception:
        # a ledger or mapping that raises while being read is as unreadable as a missing one
        return False, LEDGER_UNREADABLE


# ------------------------------------------------------------------ one resident per user

def _pid_alive(pid):
    # the one pid-liveness probe in the repo that does not use os.kill on Windows (where
    # os.kill(pid, 0) would terminate the process)
    from tools.state_store import _pid_alive as alive
    return alive(int(pid))


def lock_path(state_dir, user_oid):
    """The lock file for one user. Named by a hash so the oid is not written into a file name."""
    digest = hashlib.sha256(_norm(user_oid).encode("utf-8")).hexdigest()[:16]
    return os.path.join(state_dir, "resident_%s.lock" % digest)


def _read_owner(path):
    try:
        with io.open(path, encoding="utf-8-sig") as fh:
            return int(json.load(fh).get("pid"))
    except Exception:
        return None


class ResidentLock:
    """At most one live holder per user oid under `state_dir`. `state_dir` is required: there is
    deliberately no default, so a test or probe can never touch the production state directory by
    omission."""

    def __init__(self, state_dir, user_oid, pid=None, alive=None):
        self.state_dir = state_dir
        self.user_oid = user_oid
        self.pid = os.getpid() if pid is None else int(pid)
        self._alive = alive or _pid_alive

    @property
    def path(self):
        return lock_path(self.state_dir, self.user_oid)

    def acquire(self):
        """(ok, reason): LOCK_TAKEN / LOCK_STALE_TAKEN / LOCK_OWN, or a refusal."""
        if not _norm(self.user_oid):
            return False, LOCK_NO_USER
        try:
            os.makedirs(self.state_dir, exist_ok=True)
        except OSError:
            return False, LOCK_UNWRITABLE
        stale = False
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                held = _read_owner(self.path)
                if held == self.pid:
                    return True, LOCK_OWN
                if held is not None and self._alive(held):
                    return False, LOCK_HELD
                # dead pid or a file that names no pid: stale. Remove and try once more; if two
                # processes race here, O_EXCL lets only one of them win.
                try:
                    os.remove(self.path)
                except OSError:
                    return False, LOCK_HELD
                stale = True
                continue
            except OSError:
                return False, LOCK_UNWRITABLE
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"pid": self.pid, "started": int(time.time())}, fh)
            except OSError:
                return False, LOCK_UNWRITABLE
            return True, (LOCK_STALE_TAKEN if stale else LOCK_TAKEN)
        return False, LOCK_HELD

    def release(self):
        """Removes the lock only if this pid holds it."""
        if _read_owner(self.path) != self.pid:
            return False, LOCK_NOT_OURS
        try:
            os.remove(self.path)
        except OSError:
            return False, LOCK_NOT_OURS
        return True, LOCK_RELEASED


# ------------------------------------------------------------------ the setting (status.json only)

_settings_cache = {}


def mode(settings_path=None):
    """`resident_tools=` from settings.txt: "on" or "off". Anything else reads as off. Nothing in
    the product acts on this yet; status_block reports it. Never raises."""
    try:
        path = settings_path
        if path is None:
            from tools.settings_path import settings_file
            path = settings_file()
        if not path:
            return DEFAULT
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        hit = _settings_cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = DEFAULT
        with io.open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(KEY + "="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = v if v in MODES else DEFAULT
                    break
        _settings_cache[path] = (key, val)
        return val
    except Exception:
        return DEFAULT


def status_block(settings_path=None):
    """{"resident_tools": "off"|"on"} for status.json (additive)."""
    return {KEY: mode(settings_path)}


def _reset_for_test():
    _settings_cache.clear()
