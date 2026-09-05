# -*- coding: utf-8 -*-
"""The door an agent walks through to hand this machine a goal.

WHY THIS EXISTS, AND WHY THE OTHER FOUR ANSWERS WERE WRONG.

The owner wants to type an instruction into the M365 Copilot app on a phone and have this
machine pick it up and run a fleet. Four routes were investigated and measured before this
one, and each is closed:

  * hold the ChatHub socket open and read pushes -- the connection is per turn by observed
    protocol, and reusing one was MEASURED to make the second turn fail InvalidRequest
    (relay/chathub.py:547).
  * poll substrate's GetChatsActivity for each chat's last message -- the endpoint exists and
    the web client polls it, but it answers 403 ForbiddenRequest for this account, and the
    PAGE'S OWN call is refused identically. Its body says totalCountOfSavedChats 0,
    retentionPolicyStatus 0, and the desktop sidebar renders no conversation rows at all.
  * the /chat action API's RefreshNavPane, which a third-party client uses to list
    conversations -- it answers 200 to our own token but hands back only notebooks and
    tasksFlyout, never the chats. The client that does get chats mints a token by presenting
    a client_id to Entra, which is impersonation and is out of bounds.
  * Microsoft Graph's aiInteraction change notifications, which are documented and would be
    the right answer -- the browser's own Graph token carries 20 scopes and none of them is
    AiEnterpriseInteraction.Read, so reaching it needs a separate app registration.

The answer was in this house the whole time. The server log records 82 POST /mcp, 8 GET /mcp
and 8 DELETE /mcp from two remote addresses: the agent already connects here, fetches the
catalogue, and can call whatever is registered. So an instruction does not have to be
scraped out of a conversation -- it arrives as an ARGUMENT, through the front door, over the
owner's own tunnel, with no undocumented endpoint, no borrowed identity and no browser tab.

WHAT THIS DOES NOT DO. It does not run anything. It writes a job and returns its id. Whether
that job becomes a fleet run is the consumer's decision and the approval gate's, which is the
whole reason the two halves are separate: a tool that could start work on this machine from a
sentence typed on a phone should not also be the thing that decides to.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from relay import task_router as TR

#: A goal longer than this is a pasted document, not an instruction. The cap is generous --
#: real SWE-bench goals run to a few hundred characters -- and it exists so a runaway agent
#: cannot fill the queue directory with one call.
MAX_GOAL_CHARS = 4000

#: How many jobs may sit unclaimed before intake refuses. The queue is a handoff, not a
#: mailbox: if nothing is draining it, the honest answer is to say so rather than to keep
#: accepting work that will never run.
MAX_PENDING = 50


def _pending_count() -> int:
    try:
        return len([f for f in os.listdir(os.path.join(TR.TASKS, "pending"))
                    if f.endswith(".json")])
    except Exception:
        return 0


def _waiting_count() -> int:
    """Goals that have arrived and not yet been picked up by a runner.

    Two directories, because the router empties `pending` within a drain cycle and moves the
    goal to `for_fleet` when no run is in flight. Counting only `pending` therefore tells a
    sender "1 waiting" while five goals sit ahead of theirs, which is the opposite of what
    the sentence claims.
    """
    n = 0
    for sub in ("pending", "for_fleet"):
        try:
            n += len([f for f in os.listdir(os.path.join(TR.TASKS, sub))
                      if f.endswith(".json") or f.endswith(".txt")])
        except Exception:
            pass
    return n


def fleet_submit(goal: str, note: str = "", source: str = "") -> str:
    """Queue a goal for this machine's worker fleet. Returns the job id.

    goal: the whole instruction, standalone -- whatever runs it will not see this
    conversation. note: context for the human reviewing the queue; never executed.
    source: where the instruction came from.

    QUEUED, NOT STARTED. Nothing runs because this was called.
    """
    text = " ".join(str(goal or "").split())
    if not text:
        return "[fleet_submit: refused -- an empty goal is not an instruction]"
    if len(text) > MAX_GOAL_CHARS:
        return ("[fleet_submit: refused -- the goal is %d characters, over the %d limit. "
                "Send the instruction, not the document.]" % (len(text), MAX_GOAL_CHARS))
    n = _pending_count()
    if n >= MAX_PENDING:
        return ("[fleet_submit: refused -- %d jobs are already waiting and nothing is "
                "draining the queue. Ask the owner to look before sending more.]" % n)

    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid,
        "type": "fleet_goal",
        "payload": {"goal": text, "note": " ".join(str(note or "").split())[:500]},
        "created": time.time(),
        # PROVENANCE TRAVELS WITH THE JOB. This arrived over a tunnel from an agent, which is
        # not the same authority as a person typing into the cockpit, and the consumer is
        # entitled to treat it differently. Recording it here means the difference survives
        # into the queue instead of being lost at the door.
        "origin": {"via": "mcp", "source": " ".join(str(source or "agent").split())[:60]},
    }
    try:
        TR.ensure_dirs()
        path = os.path.join(TR.TASKS, "pending", "%s.json" % jid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(job, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as exc:
        return "[fleet_submit: could not queue the goal: %s: %s]" % (type(exc).__name__, exc)

    # COUNTED AFTER THE WRITE, NOT BEFORE. Four devices submitting at the same instant each
    # read the queue before any of them had written, so all four were told "1 job waiting"
    # when there were four -- and submitting from more than one device at a time is the
    # premise this door was built for. Reading after os.replace means the number includes
    # this goal and everything that landed ahead of it.
    #
    # The admission check above keeps its earlier reading on purpose: it is a cheap refusal
    # for a queue nobody is draining, and it can still let a simultaneous burst past
    # MAX_PENDING. Nothing breaks at 51 -- the number is advisory -- so this is stated
    # rather than locked against.
    return ("queued %s -- the goal is in this machine's queue and will be picked up by its "
            "runner. It has NOT started yet, and nothing has run because of this call. "
            "%d job(s) waiting." % (jid, _waiting_count()))


def fleet_queue() -> str:
    """What is waiting, running and finished in this machine's job queue."""
    try:
        TR.ensure_dirs()
    except Exception as exc:
        return "[fleet_queue: %s: %s]" % (type(exc).__name__, exc)
    lines = []
    for sub in ("pending", "running", "awaiting", "for_fleet", "done"):
        try:
            names = sorted(os.listdir(os.path.join(TR.TASKS, sub)))
        except Exception:
            names = []
        lines.append("%-9s %d" % (sub, len(names)))
    return "\n".join(lines)
