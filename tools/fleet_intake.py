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
import re
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

#: Appended by _clean when it cuts, so a short field is never mistaken for a short answer.
_TRUNCATED = "\u2026[cut]"

#: What `source` is allowed to say. It was 60, which is under the length of a single sentence
#: naming a worker and an IP -- and that is exactly what a real one said. `note` has had 500
#: all along for text a person reads; provenance is not less important than a note.
MAX_SOURCE_CHARS = 300


#: AN ADDRESS IN THE PROVENANCE LINE IS THE AGENT TALKING ABOUT THE PERSON, and it is the one
#: field here the agent composes rather than relays. Measured 2026-09-10: asked over a plain
#: conversation to write a file, the agent could not unlock, handed the work to this door as
#: designed -- and filled `source` in with the owner's own work address. That string lands in
#: .fleet/tasks/*/<id>.json and travels with the job into done/, so one ordinary handoff wrote
#: both an employee identifier and the employer's domain into the repository tree. .fleet is
#: gitignored, so this was not a publication; it is still the exact two-word class this project
#: rewrote its history to remove, arriving by a route nobody had looked at.
#:
#: Redacted at the door rather than at the reader, because there are several readers (the
#: router's done/ record, the cockpit, fleet_queue) and only one writer. The goal and note are
#: the PERSON's own words and are left alone -- an instruction that names someone is the
#: instruction, and rewriting it would change the work.
_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _clean(text, limit: int) -> str:
    """Collapse whitespace, drop any address, cap -- and SAY SO when the cap bites.

    A silent cut is how a record stops being a record. Measured 2026-09-18 on a real archived
    job: `origin.source` ended mid-sentence at "...はローカル書込・実行ツールが", because the
    caller had named the requesting worker AND the client IP and 60 characters does not hold
    both. Nothing in the file said it had been cut, so it read as a caller that had trailed
    off rather than as a field too small for what it is asked to carry.

    This repository already states the rule elsewhere -- evidence_trace records a truncated
    argument list AS truncated, "silently keeping the first 4,000 characters means a
    destination named at character 4,001 is not merely missed, it is missed by a check that
    then reports nothing wrong". Same rule, same reason.
    """
    out = " ".join(_ADDRESS_RE.sub("<address redacted>", str(text or "")).split())
    if limit and len(out) > limit:
        # The marker is inside the budget, so the result still honours `limit`.
        cut = max(0, limit - len(_TRUNCATED))
        return out[:cut] + _TRUNCATED
    return out


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


#: MEASURED 2026-09-15. A worker that could not obtain an unlock token called this door to hand
#: its work to another worker instead of stopping (origin.source recorded "unlock unavailable on
#: this client"). Two more submissions for the same PowerPoint rebuild followed -- one a worker's
#: own paraphrase, one the operator's deliberate resubmission of a stopped run. None were
#: byte-identical, so refusing only an exact repeat (as the empty/length/capacity checks above
#: already effectively do by construction) catches none of them: a failure of ANY kind that makes
#: a worker delegate onward, not only a lock, lands here as three occupied Copilot conversations
#: instead of one.
#:
#: THE COMPARISON SHAPE IS tools/skill_lessons.py's, NOT tools/skill_candidates.normalise's. That
#: module's docstring explains why at length: normalise-then-compare-exact groups two attempts
#: only when their WORDING matches, which made it blind to the exact case it was built for (an
#: added sentence, a rewritten prefix). skill_lessons instead asks whether two freely-worded goals
#: are recognisably the same WORK -- Jaccard overlap of content words -- and that is the same
#: question this door has to answer: is the goal arriving now the one already sitting in the
#: queue or on a worker's plate, reworded.
#:
#: DIGITS COUNT AS CONTENT HERE, WHICH IS relay_fleet.py's _stuck_converged CHOICE, NOT
#: skill_lessons.words()'s (skill_lessons deliberately drops digits). That module compares two
#: FAILURE EXPLANATIONS, where a changed digit -- a turn count, an error code -- is usually the
#: one thing that makes a restated conclusion a NEW conclusion, so dropping digits there would
#: erase the distinction that matters. This door compares two INSTRUCTIONS for what is or is not
#: the same WORK, where the opposite risk dominates: a page count, a version suffix or a slice
#: index is exactly the kind of concrete detail that survives a paraphrase of the same job and
#: distinguishes it from a merely similar-sounding different one. It also matters for the one
#: shape this door must never strangle -- relay/fanout.py's `child_goals` builds every sibling by
#: appending "range %d/%d" (its subtask_index/count) to a COPY of the whole parent goal, so if a
#: fanout goal ever did reach this door, the digits in that header are the one thing that tells
#: two siblings apart; dropping them would make every sibling of the same split look like a
#: resubmission of the last one. (It does not reach this door at all -- see the note above
#: DUP_SIMILARITY -- so this is belt, not braces, but the reasoning is the same reasoning either
#: way: digits are the signal that keeps near-identical-by-construction goals apart.)
_DUP_WORD = re.compile(r"[A-Za-z]{3,}|[0-9]+|[一-鿿]{2,}|[゠-ヿ]{2,}")
_DUP_STOPWORDS = frozenset(
    "the and for with from this that you your please into out all any are was were have "
    "has had not but its use using can will should".split())

#: tools/skill_lessons.MIN_SIMILARITY (0.5), NOT relay_fleet.STUCK_CONVERGENCE_SIMILARITY (0.7).
#: The higher bound there is calibrated to never mistake a genuinely changing failure (a fixture
#: whose reasons share 5 of ~6-7 words turn to turn) for a repeated conclusion -- a question about
#: whether prose that is MOSTLY the same is ACTUALLY the same finding. The question here is
#: coarser: is this goal the one already in the queue, at all. skill_lessons measured 0.5 as the
#: point where pairs are "recognisably one job" without pulling in attempts that merely share a
#: topic's vocabulary, and that is the same distinction this door needs -- a worker's paraphrase
#: and an operator's terser resubmission of the SAME job, not two unrelated jobs about the same
#: file. Unlike skill_lessons this is a REFUSAL, not a research grouping a person reviews
#: afterwards, so getting it wrong costs a worker one extra sentence explaining why its goal
#: really is new -- cheap next to the failure mode this exists to close.
DUP_SIMILARITY = 0.5

#: Same floor and same reason as skill_lessons.MIN_WORDS: below this many content words, overlap
#: is not evidence either way, and a short goal ("do a thing", used throughout this file's own
#: tests) must never be compared at all rather than compared unreliably.
DUP_MIN_WORDS = 4

#: relay_fleet.TERMINAL, copied rather than imported. relay_fleet.py is the live fleet loop -- a
#: run is live on this machine while this file is edited -- and importing it here would pull that
#: whole module (and whatever it does at import time) into every call this MCP-exposed door makes,
#: for the sake of one seven-item tuple. Duplication is the safer coupling: if relay_fleet ever
#: adds a terminal status this tuple does not know about, the cost is a worker that has actually
#: finished still being read as in-flight for one refusal, not an import-time failure in a door
#: an agent is calling over a live tunnel.
_WORKER_TERMINAL = frozenset(("done", "stuck", "maxturns", "error", "cancelled",
                               "content_refused"))


def _dup_words(text):
    return {w.lower() for w in _DUP_WORD.findall(text or "")} - _DUP_STOPWORDS


def _dup_similarity(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _job_dicts(sub):
    """Every {..} job record sitting in TASKS/<sub>, skipping anything unreadable.

    Covers `pending/` (arrived, not yet claimed) and `running/` (claimed by the router,
    mid-dispatch) -- both hold the same job shape written by this door or read by
    relay.task_router, so one reader serves either.
    """
    out = []
    d = os.path.join(TR.TASKS, sub)
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def _in_flight_goals():
    """(job_id, goal_text) for every goal this machine already has queued or running, from
    every place one can be sitting RIGHT NOW -- not only `pending/`, which is precisely the
    directory the measured incident's goals were never waiting in by the time a second and
    third copy were submitted.

    Four sources, in the order a goal actually moves through this machine:
      pending/, running/  -- arrived through this door (or another), not yet handed to a fleet.
      for_fleet/*.txt      -- handed off, parked because no fleet run was live to take it.
      awaiting_ack/*.json  -- handed to a LIVE fleet, not yet confirmed read (see
                              relay.task_router.fleet_handoff / _reconcile_landings) -- the
                              window a resubmission arriving seconds after the first would
                              otherwise fall straight through.
      .fleet status.json   -- a worker actually holding the goal right now (`closed` false,
                              status not terminal). This is the one the measured incident
                              needed most: by the time the second and third copies were
                              submitted, the first was not in any queue directory at all --
                              it was a worker's own `goal`.
    A finished job is not in any of these: pending/running/for_fleet/awaiting_ack all lose it
    once the router marks it done, and a finished worker is `closed`. That is what makes a
    deliberate resubmission of STOPPED work keep working with no special case here.
    """
    out = []
    for sub in ("pending", "running"):
        for job in _job_dicts(sub):
            goal = (job.get("payload") or {}).get("goal") or ""
            if goal:
                out.append((job.get("id") or "?", goal))

    d = os.path.join(TR.TASKS, "for_fleet")
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".txt"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                goal = fh.read()
        except OSError:
            continue
        if goal.strip():
            out.append((name[:-4], goal))

    for job in _job_dicts("awaiting_ack"):
        goal = job.get("goal") or ""
        if goal:
            out.append((job.get("id") or "?", goal))

    try:
        with open(os.path.join(TR.FLEET_STATE_DIR, "status.json"), encoding="utf-8-sig") as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        status = {}
    for w in status.get("workers") or []:
        if not isinstance(w, dict) or w.get("closed"):
            continue
        if str(w.get("status") or "").lower() in _WORKER_TERMINAL:
            continue
        goal = w.get("goal") or ""
        if goal:
            out.append((w.get("jid") or w.get("task_id") or "?", goal))

    return out


def _duplicate_of(text: str):
    """The (job_id, similarity) of the closest in-flight goal, or None if nothing clears
    DUP_SIMILARITY. `text` is the ALREADY-NORMALISED goal (whitespace collapsed) fleet_submit
    is about to queue."""
    words = _dup_words(text)
    if len(words) < DUP_MIN_WORDS:
        return None
    best = None
    for jid, existing in _in_flight_goals():
        w = _dup_words(existing)
        if len(w) < DUP_MIN_WORDS:
            continue
        sim = _dup_similarity(words, w)
        if sim >= DUP_SIMILARITY and (best is None or sim > best[1]):
            best = (jid, sim)
    return best


def fleet_submit(goal: str, note: str = "", source: str = "",
                 priority: bool = False) -> str:
    """Queue a goal for this machine's worker fleet. Returns the job id.

    goal: the whole instruction, standalone -- whatever runs it will not see this
    conversation. note: context for the human reviewing the queue; never executed.
    source: where the instruction came from.
    priority: jump the fleet's pending queue when a slot frees up. NOT a way to run sooner
    than the machine can -- the fleet still waits for a free worker slot, and a priority goal
    simply goes to the front of the line rather than the back. Default false, because a door
    where everything is urgent has no priority at all.

    QUEUED, NOT STARTED. Nothing runs because this was called.
    """
    text = " ".join(str(goal or "").split())
    if not text:
        return "[fleet_submit: refused -- an empty goal is not an instruction]"
    if len(text) > MAX_GOAL_CHARS:
        return ("[fleet_submit: refused -- the goal is %d characters, over the %d limit. "
                "Send the instruction, not the document.]" % (len(text), MAX_GOAL_CHARS))
    dup = _duplicate_of(text)
    if dup is not None:
        dup_jid, sim = dup
        return ("[fleet_submit: refused -- this reads as the goal already queued or running "
                "as %s (%d%% word overlap). If it is genuinely different work, say specifically "
                "how; if you meant to check on that job, use fleet_queue() instead of "
                "resubmitting it.]" % (dup_jid, round(sim * 100)))
    n = _pending_count()
    if n >= MAX_PENDING:
        return ("[fleet_submit: refused -- %d jobs are already waiting and nothing is "
                "draining the queue. Ask the owner to look before sending more.]" % n)

    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid,
        "type": "fleet_goal",
        # `priority` IS ON THE PAYLOAD, BESIDE THE GOAL, because that is where the router
        # reads a job's own fields. It travels to the fleet through every one of the three
        # delivery paths -- joining a live run, starting one, and waiting in for_fleet/ --
        # which is the part that had to be built rather than declared: the waiting path stored
        # the goal as a bare .txt, so a field added here without touching it would have been
        # dropped exactly the way resume_conv was.
        "payload": {"goal": text, "note": _clean(note, 500), "priority": bool(priority)},
        "created": time.time(),
        # PROVENANCE TRAVELS WITH THE JOB. This arrived over a tunnel from an agent, which is
        # not the same authority as a person typing into the cockpit, and the consumer is
        # entitled to treat it differently. Recording it here means the difference survives
        # into the queue instead of being lost at the door.
        "origin": {"via": "mcp", "source": _clean(source or "agent", MAX_SOURCE_CHARS)},
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
