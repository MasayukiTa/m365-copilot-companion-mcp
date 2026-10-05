# -*- coding: utf-8 -*-
"""Fewer Copilot conversations: lazy creation, the unsent measurement, and the merge setting.

WHY THIS EXISTS. A Copilot Studio conversation is billed by the SESSION it opens, and counting
conversation GUIDs over a day found two kinds of waste that the work itself did not need:

  1. ZERO-MESSAGE CONVERSATIONS. 467 conversation ids appeared in one burst (workers w0-w483,
     2026-09-30) and 48 zero-turn worker transcripts on 2026-10-04 carried an id before the
     first message was ever sent. A conversation nobody spoke in opens no Studio session, but the
     id was minted when the object was built and recorded by the status poll, so every one of
     them read as a conversation in the ledgers, the transcripts and the resume lookups.
     The fix lives where the id is born (relay/chathub.py: the id is minted by the first read
     that puts it on the wire, and `peek_conversation_id` lets a recorder ask without creating
     it). This module adds what was missing next to it: a ROW whenever a conversation was
     opened and no message went out, so the rate is a number rather than an anecdote.

  2. THE AGGREGATOR'S OWN CONVERSATION. Every fan-out family opens a splitter conversation (the
     parent), N child conversations and a FRESH aggregator conversation -- about a quarter of the
     volume on 2026-10-04. The merge can instead continue the PARENT's conversation, which
     already holds the goal and the split plan; the children's answers arrive as the merge
     input exactly as before. That is the `merge_conversation` setting: `fresh` (the default and
     the behaviour before this module) or `parent`.

WHAT `parent` CHANGES, AND WHAT IT DOES NOT. Only the transport: the merge goal item gets the
parent's conversation id in `resume_conv`, which is the field RelayWorker already understands
(a follow-up continues an earlier conversation). The merge is still its own goal item, its own
worker and its own admission slot, with the same campaign id, task id, role, depth, checks and
resume key -- so the one-merge-exactly-once guard (`_camp["merged"]`), `merge_requeued`, the
nested-slot rows (`_note_nested_result`) and the resume rebuild never see a difference. Splitting
still ENDS the parent worker (that is what prevents the slot deadlock fanout.aggregation_goal
describes); what outlives it is the conversation's id, and a socket conversation continues by id
(measured 2026-08-24, relay/socket_route.py driver_for). A family adopted from an earlier run,
a tab parent, or a parent that never spoke has no id to continue and merges in a fresh one, as
before.

THE DEFAULT STAYS `fresh`. Nothing measured yet says a merge answers as well inside a
conversation that was primed to produce a split plan; the structure is safe, the quality is not
established. The opt-in exists so that it can be measured, and `aggregator_conversation` rows
(input size, whether plain concatenation would have sufficed) are how.
"""
from __future__ import annotations

import threading
import time

from relay import mechanism_telemetry as _mt

MERGE_SETTING_KEY = "merge_conversation"
MERGE_MODES = ("fresh", "parent")
MERGE_DEFAULT = "fresh"

#: Seconds a worker may hold an opened conversation with nothing sent before it is a row.
UNSENT_AFTER_S = 60.0

_LOCK = threading.Lock()
_COUNTS = {"aggregators_saved": 0, "unsent_created": 0}


def merge_conversation_setting():
    """The `merge_conversation` setting: "fresh" or "parent" (default "fresh").

    Read from settings.txt on every call (each_gate: at each split and each merge). Only the
    exact value `parent` (case-insensitive) selects it; absent, empty or unrecognised is fresh.
    Never raises.
    """
    try:
        from relay import fleet_runner as fr
        raw = fr._settings_text(MERGE_SETTING_KEY)
    except Exception:
        return MERGE_DEFAULT
    if raw is None:
        return MERGE_DEFAULT
    return "parent" if raw.strip().lower() == "parent" else MERGE_DEFAULT


def parent_conversation_ref(worker):
    """The conversation id a split parent could hand to its merge, or "".

    Empty unless the setting is `parent`, the parent is a socket worker (a tab has no id to
    continue) and its conversation has really spoken (peeked, never minted here). Never raises.
    """
    try:
        if merge_conversation_setting() != "parent":
            return ""
        if not getattr(worker, "socket", False) or getattr(worker, "drv", None) is None:
            return ""
        ids = worker.drv.conversation_ids() or {}
        if int(ids.get("turns") or 0) < 1:
            return ""
        return str(ids.get("client") or ids.get("server") or "").strip()
    except Exception:
        return ""


def parent_merge_kwargs(worker):
    """The extra keyword arguments for the spawn call: {} unless there is a parent id to pass,
    so the call a fake or older spawn function sees is unchanged by default."""
    ref = parent_conversation_ref(worker)
    return {"parent_conv": ref} if ref else {}


def _input_chars(records):
    return sum(len(str((r or {}).get("result") or "")) for r in (records or []))


def apply_merge_conversation(agg, camp, records, *, run_id=""):
    """Decide which conversation the merge goal `agg` runs in; return `agg` (same object).

    ALWAYS writes one `aggregator_conversation` row (the measurement: input size, slices, and
    whether plain concatenation could have sufficed). Under `fresh` that is all it does. Under
    `parent`, with a recorded parent conversation, sets `resume_conv` on the goal item and
    counts the aggregator conversation as saved. Never raises and never changes anything else on
    the item: identity, role, checks and resume key are untouched.
    """
    try:
        recs = list(records or [])
        done = [r for r in recs
                if str((r or {}).get("outcome") or "").upper() == "DONE"]
        all_done = bool(recs) and len(done) == len(recs)
        has_checks = bool((camp or {}).get("checks"))
        has_partial = bool((camp or {}).get("partial"))
        setting = merge_conversation_setting()
        parent_conv = str((camp or {}).get("parent_conv") or "").strip()
        used = False
        why = ""
        if setting == "parent":
            if not parent_conv:
                why = "no parent conversation recorded (tab parent, adopted family or none spoken)"
            elif isinstance(agg, dict) and not agg.get("resume_conv"):
                agg["resume_conv"] = parent_conv
                used = True
                with _LOCK:
                    _COUNTS["aggregators_saved"] += 1
            else:
                why = "goal already carries resume_conv"
        try:
            _mt.record("aggregator_conversation", run_id=run_id,
                       instance=str((agg or {}).get("task_id") or ""),
                       configured=True, config_source="settings", config_value=setting,
                       eligible=(setting == "parent"), triggered=used,
                       not_triggered_reason=(why if setting == "parent" and not used else ""),
                       executed=used,
                       extra={"campaign_id": (agg or {}).get("campaign_id", ""),
                              "slices": len(recs), "done": len(done),
                              "input_chars": _input_chars(recs),
                              # A HEURISTIC, named as one: every slice finished, nothing of the
                              # parent's own to carry and no whole-goal check to satisfy. It says
                              # a deterministic join MIGHT have sufficed, not that it would.
                              "concat_candidate": bool(all_done and not has_checks
                                                       and not has_partial)})
        except Exception:
            pass
    except Exception:
        pass
    return agg


def unsent_overdue(attached_ts, sent_ts, turn, now=None, after_s=UNSENT_AFTER_S):
    """True when a conversation was opened `after_s`+ seconds ago and nothing has been sent."""
    try:
        if not attached_ts or sent_ts or int(turn or 0) > 0:
            return False
        return ((time.time() if now is None else now) - float(attached_ts)) >= float(after_s)
    except Exception:
        return False


def record_unsent(*, run_id="", instance="", route="", age_s=0.0, where="", has_id=False):
    """One `conversation_created_unsent` row, and the counter the cockpit reports."""
    with _LOCK:
        _COUNTS["unsent_created"] += 1
    try:
        _mt.record("conversation_created_unsent", run_id=run_id, instance=instance,
                   configured=True, config_source="run", eligible=True, triggered=True,
                   executed=True,
                   extra={"route": route, "age_s": round(float(age_s), 1), "where": where,
                          "has_conversation_id": bool(has_id),
                          "after_s": UNSENT_AFTER_S})
    except Exception:
        pass


def status_block():
    """The additive status.json block. Never raises."""
    try:
        with _LOCK:
            counts = dict(_COUNTS)
        return {"conversation_saving": {
            "merge_conversation": merge_conversation_setting(),
            "aggregators_saved": int(counts["aggregators_saved"]),
            "unsent_created": int(counts["unsent_created"])}}
    except Exception:
        return {}
