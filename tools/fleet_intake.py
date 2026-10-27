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


def _consumer_sentence() -> str:
    """Whether anything is draining the queue RIGHT NOW -- the one fact "queued" hid.

    2026-10-17: jobs sat 4 h 45 min in pending because the supervisor (the only caller of the
    router) was gone while this server kept answering, and the reply said only "queued".
    """
    try:
        from relay import consumer_watch as CW
        alive = CW.consumer_alive()
    except Exception:
        alive = None
    if alive is True:
        return "A queue consumer (the supervisor) is running."
    if alive is False:
        return ("WARNING: no queue consumer (the supervisor) is running right now, so this job "
                "will NOT be picked up until it is restarted. The server restarts it "
                "automatically after about %d s; if the job is still waiting after a few "
                "minutes, the owner must run start_all.bat." % int(CW.STALE_AFTER_S))
    return "Could not tell whether a queue consumer is running."


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

#: A goal's IDENTIFIERS: maximal runs of Latin letters/digits, keeping internal . _ / - so a
#: filename (sales.csv), a version (v2, beta.12) or a path stays one token. Unlike _DUP_WORD this
#: KEEPS one- and two-letter runs, because a single letter is often the ONE thing that tells two
#: short human requests apart -- "ファイルAを解析" vs "ファイルBを解析" differ only in A vs B, and
#: _DUP_WORD's three-letter floor drops both, leaving two goals that look word-for-word identical.
#: Used only to ask "do these two goals name DIFFERENT specific things"; it does not feed the
#: Jaccard measure (that stays on _dup_words, where the floor keeps stray short fragments from
#: being counted as shared vocabulary).
_DUP_IDENT = re.compile(r"[A-Za-z0-9]+(?:[._/-][A-Za-z0-9]+)*")
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

#: OWNER ORDER 2026-10-26: refusing a goal at the door on word overlap is OFF by default.
#: 177 measured refusals (144 of them goals under 200 characters) stopped DIFFERENT work -- two
#: short goals sharing a verb, or two unrelated coding goals sharing one appended block of common
#: rules -- and that was the single biggest source of a person being turned away at the door. The
#: owner's decision is that near-duplicates are NORMALISED IN THE BACKGROUND (a note on the job and
#: one line in .fleet/dup_notes.jsonl), not refused in front of the person. The whole similarity
#: machinery below is kept, because it is still exactly what the background note needs; it simply no
#: longer gates admission. The old refusing behaviour can be restored per-machine with the
#: `fleet_submit_dup_guard=on` setting (see tools/settings_keys.py), for an operator who wants it
#: back; absent or anything but `on` is off.
DUP_GUARD_ENABLED = False

#: Same floor and same reason as skill_lessons.MIN_WORDS: below this many content words, overlap
#: is not evidence either way, and a short goal ("do a thing", used throughout this file's own
#: tests) must never be compared at all rather than compared unreliably.
DUP_MIN_WORDS = 4

#: MEASURED 2026-10-26 on the owner's two real refused goals (goal1.txt, goal2.txt). Two
#: UNRELATED coding requests -- a desktop chat-switch bug fix, and a post-quantum config
#: migration -- were each rejected as near-duplicates of the other. The cause was not the
#: threshold: measured on those two goals, whole-goal Jaccard of content words was 0.539, just
#: over DUP_SIMILARITY, and 0.515-0.55 is the band the incident report quotes. But the two goals
#: END with the SAME long shared "common rules" paragraph (anchored at "共通の守ること"): 212
#: trailing tokens are IDENTICAL on both sides. Of the 152 content words the two goals shared,
#: 107 -- 70.4% -- appear only inside that boilerplate tail and in neither goal's own head. Strip
#: the shared tail and compare the goals' OWN text (the request title and body before it) and the
#: similarity falls to 0.15 -- plainly different work. So the fix is not to move DUP_SIMILARITY (which
#: still has to catch a real paraphrase) but to stop a paragraph that is identical ACROSS
#: submissions from counting as evidence that two goals are the same job. _goal_specific_words
#: below removes, for each PAIR, the longest run of trailing tokens the two goals share verbatim,
#: and _duplicate_of compares what is left. A true resubmission (same text, a few words reworded,
#: words reordered) keeps almost all of its OWN words in common, so it still clears the bar; two
#: different jobs that merely carry the same appended rules no longer do.
#:
#: Tokens, not the word-SET, because the shared tail has to be found as a contiguous block at the
#: end -- a set cannot tell where the boilerplate begins. Case-folded, whitespace already
#: collapsed by fleet_submit. Only the TAIL is stripped (common rules are appended, never
#: prepended), so a goal that happens to open with a stock sentence is untouched.
#: Below this many goal-specific words survive the strip, the stripped comparison is not trusted
#: and the whole-goal similarity is used instead -- the same "too little to judge on" floor as
#: DUP_MIN_WORDS, applied to the residue.
DUP_SPECIFIC_MIN_WORDS = 4

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


def _dup_tokens(text):
    """The content words of `text` IN ORDER, lower-cased, stopwords kept.

    _dup_words returns a SET (what two goals share); this returns the SEQUENCE, which is what
    is needed to find the shared block at the end. Stopwords are NOT dropped here: the shared
    'common rules' tail is matched verbatim token-for-token, and dropping stopwords first would
    let two tails that differ only in a stopword still look identical, stripping more than the
    two goals actually share.
    """
    return [w.lower() for w in _DUP_WORD.findall(text or "")]


def _shared_tail_len(ta, tb):
    """How many tokens at the END of `ta` and `tb` are identical, compared from the back.

    This is the appended shared paragraph ('common rules'): the owner's two refused goals end
    with 115 identical content tokens. Returns 0 when the goals do not share a tail.
    """
    n = 0
    la, lb = len(ta), len(tb)
    while n < la and n < lb and ta[la - 1 - n] == tb[lb - 1 - n]:
        n += 1
    return n


def _goal_specific_words(text, other):
    """The content-word SET of `text` with the tail it shares VERBATIM with `other` removed.

    The pair is compared, not one goal in isolation, because 'which trailing block is shared'
    is only defined relative to the other goal in the comparison -- there is no global list of
    boilerplate to subtract, and inventing one (a hard-coded '共通の守ること' marker, a length
    cutoff) would be the kind of ungrounded constant this change is meant to avoid. The shared
    tail is found per pair, from the real text, every time.

    Returns (specific_word_set, specific_token_count). When the two goals share no tail the
    result is just _dup_words(text); when one goal is entirely a tail of the other (a true
    resubmission) almost nothing is removed, because the SHARED block is only the part that is
    identical token-for-token from the end -- a reworded body stops the match as soon as the
    first differing token is reached.
    """
    ta = _dup_tokens(text)
    tb = _dup_tokens(other)
    tail = _shared_tail_len(ta, tb)
    head = ta[: len(ta) - tail] if tail else ta
    words = {w for w in head} - _DUP_STOPWORDS
    return words, len(words)


def _goal_specific_identifiers(text, other):
    """The identifiers `text` carries that are NOT part of the tail it shares verbatim with
    `other` -- the SPECIFIC things this goal names, with the appended common-rules paragraph
    (which names git, diff, sharepoint, ... in EVERY goal that carries it) subtracted.

    Same shared-tail removal as _goal_specific_words, but on identifiers (_DUP_IDENT, which
    keeps one- and two-letter runs) rather than content words. The tail's identifiers are found
    from the shared trailing tokens and removed, so two unrelated goals that merely append the
    same rules do not look as though they name the same things. Returns a set.
    """
    all_ids = {m.lower() for m in _DUP_IDENT.findall(text or "")}
    ta = _dup_tokens(text)
    tb = _dup_tokens(other)
    tail = _shared_tail_len(ta, tb)
    if tail:
        tail_ids = {m.lower()
                    for tok in ta[len(ta) - tail:]
                    for m in _DUP_IDENT.findall(tok)}
        all_ids -= tail_ids
    return all_ids


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
    is about to queue.

    The comparison is made on each PAIR's goal-SPECIFIC words -- the content words left after
    the longest run of trailing tokens the two goals share verbatim is removed (see
    _goal_specific_words). That stops a long 'common rules' paragraph, appended identically to
    many unrelated submissions, from counting as evidence that two goals are the same job: the
    owner's two real refused goals (goal1.txt, goal2.txt) share 212 trailing tokens, and their
    whole-goal Jaccard of 0.539 cleared the old bar even though their OWN text (title and body
    before the shared tail) has a Jaccard of only 0.15. A true resubmission (same text, a few
    words reworded or reordered) keeps almost all of its OWN words, so it still clears the bar.

    When stripping the shared tail leaves either side with fewer than DUP_SPECIFIC_MIN_WORDS
    goal-specific words, the residue is too small to judge on, so the whole-goal similarity
    (_dup_words) is used for that pair instead -- the same 'too little to judge' floor as
    DUP_MIN_WORDS, applied to the residue. That keeps a short goal that is almost entirely a
    shared tail from slipping through on an empty head.
    """
    whole = _dup_words(text)
    if len(whole) < DUP_MIN_WORDS:
        return None
    best = None
    for jid, existing in _in_flight_goals():
        w = _dup_words(existing)
        if len(w) < DUP_MIN_WORDS:
            continue
        # DIFFERENT SPECIFIC THINGS -> different work, whatever the word overlap. A short human
        # request ('ファイルAを解析して' vs 'ファイルBを解析して', 'sales.csv を...' vs
        # 'orders.csv を...') is mostly shared verb-and-template vocabulary; the ONE token that
        # distinguishes the jobs is the name/number/path/filename. _dup_words drops the one- and
        # two-letter runs that carry that distinction (its three-letter floor exists to keep the
        # Jaccard measure clean), so measured on content words alone the two goals score 1.0 and
        # the second is refused as a resend of the first. So before the word measure: if BOTH
        # goals name identifiers of their OWN (outside the shared common-rules tail) and share
        # NONE of them, they are about different things and this is not a duplicate. A real
        # resend keeps the same identifiers (or names none), so it is unaffected and still
        # caught below. The tail is stripped first so the git/diff/sharepoint tokens that appear
        # in every goal carrying the same appended rules are not mistaken for shared specifics.
        id_a = _goal_specific_identifiers(text, existing)
        id_b = _goal_specific_identifiers(existing, text)
        if id_a and id_b and not (id_a & id_b):
            continue
        spec_a, na = _goal_specific_words(text, existing)
        spec_b, nb = _goal_specific_words(existing, text)
        if na >= DUP_SPECIFIC_MIN_WORDS and nb >= DUP_SPECIFIC_MIN_WORDS:
            sim = _dup_similarity(spec_a, spec_b)
        else:
            # The shared tail ate almost everything -- on this pair the only thing left to
            # compare IS the overlap, so fall back to the whole-goal measure rather than
            # judging on a handful of residual words (or an empty set, which scores 0 and
            # would wave a genuine resubmission straight through).
            sim = _dup_similarity(whole, w)
        if sim >= DUP_SIMILARITY and (best is None or sim > best[1]):
            best = (jid, sim)
    return best


#: off|on. When on, a near-duplicate is REFUSED at the door exactly as before (the old behaviour,
#: kept for an operator who wants it). When off (the default, DUP_GUARD_ENABLED is False), a
#: near-duplicate is ACCEPTED and recorded in the background. Read fresh on every submission, like
#: tools/approval_policy.current_approval_mode, so turning it on takes effect on the next call with
#: no restart. Tests set it directly by patching this function so the admission decision is tested
#: in isolation from settings-file parsing.
_DUP_GUARD_SETTING = "fleet_submit_dup_guard"


def _dup_guard_on() -> bool:
    """Whether the word-overlap guard REFUSES a near-duplicate right now.

    DUP_GUARD_ENABLED is the compiled default (False -- the owner's 2026-10-26 order). The
    `fleet_submit_dup_guard` setting can turn it back on per-machine: an explicit `on` in the one
    settings file restores the old refusal; absent, unreadable, or anything but `on` leaves it off.
    Read at each call so a change lands on the next submission with no restart, and swallow any
    read error to off -- the door must never fail because a settings file could not be read.
    """
    try:
        from tools import settings_path as SP
        raw = SP.read_key(SP.settings_file(), _DUP_GUARD_SETTING)
        if raw is not None:
            return raw.strip().lower() == "on"
    except Exception:
        pass
    return bool(DUP_GUARD_ENABLED)


def _note_possible_duplicate(new_jid: str, dup_jid: str, similarity: float, text: str) -> None:
    """Record, in the background, that `new_jid` resembles the in-flight `dup_jid` -- the
    replacement for refusing the person at the door.

    Appends one line to .fleet/dup_notes.jsonl: the time, the new job id, the one it resembles,
    the similarity, and the first 80 characters of the goal (same head rule and same address
    redaction as the refusal log, so a note never carries an address or a goal body). The job's
    own payload is tagged separately by fleet_submit (possible_duplicate_of / similarity) so a
    reader of the queue sees it without opening this log.

    NEVER RAISES, and never fails the submission: a dup note is an after-the-fact record, and the
    owner's order is that a near-duplicate is ACCEPTED. A write failure here is written to
    .fleet/dup_notes_errors.log and swallowed -- the exception is recorded, not re-raised and not
    silently dropped, so a broken note leaves a trail without ever turning into a second gate.
    """
    try:
        from tools import obs_log
        obs_log.append("dup_notes.jsonl", {
            "new": str(new_jid),
            "possible_duplicate_of": str(dup_jid),
            "similarity": round(float(similarity), 4),
            "goal_head": obs_log.head(text, 80),
        })
    except Exception as exc:
        try:
            from tools import obs_log
            import time as _time
            path = os.path.join(obs_log._state_dir(), "dup_notes_errors.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("%s %s: %s\n" % (_time.strftime("%Y-%m-%dT%H:%M:%S"),
                                          type(exc).__name__, exc))
        except Exception:
            pass


def _log_refusal(reason: str, text: str, source: str, similar_to: str = "") -> None:
    """Record a refusal in .fleet/fleet_submit_refusals.jsonl. Never raises, never alters the
    reply the caller gets."""
    try:
        from tools import obs_log
        obs_log.log_refusal(reason, text, source, similar_to=similar_to)
    except Exception:
        pass


def fleet_submit(goal: str, note: str = "", source: str = "",
                 priority: bool = False, resume_conv: str = "") -> str:
    """Queue a goal for this machine's worker fleet. Returns the job id.

    goal: the whole instruction, standalone -- whatever runs it will not see this
    conversation. note: context for the human reviewing the queue; never executed.
    source: where the instruction came from.
    priority: jump the fleet's pending queue when a slot frees up. NOT a way to run sooner
    than the machine can -- the fleet still waits for a free worker slot, and a priority goal
    simply goes to the front of the line rather than the back. Default false, because a door
    where everything is urgent has no priority at all.
    resume_conv: the id (or URL) of an EXISTING Copilot conversation this goal should CONTINUE
    rather than start fresh -- the value the chat window's continuation puts on the goal item
    (e.g. "sess:..."). Carried on the job payload and threaded through every delivery route as
    the goal-item field `resume_conv`, so the worker resumes that conversation instead of
    guessing one by goal text. A goal that carries it is EXEMPT from the duplicate check: a
    follow-up is deliberately short and reads as the terse resubmission that check refuses, and
    refusing it is how a continuation silently became a new, context-free task. Default empty.

    QUEUED, NOT STARTED. Nothing runs because this was called.
    """
    resume_conv = " ".join(str(resume_conv or "").split())
    text = " ".join(str(goal or "").split())
    if not text:
        _log_refusal("missing_goal", text, source)
        return "[fleet_submit: refused -- an empty goal is not an instruction]"
    if len(text) > MAX_GOAL_CHARS:
        _log_refusal("too_long", text, source)
        return ("[fleet_submit: refused -- the goal is %d characters, over the %d limit. "
                "Send the instruction, not the document.]" % (len(text), MAX_GOAL_CHARS))
    if TR.is_local_loop_control_submission(text, source):
        _log_refusal("local_loop", text, source)
        return (
            "[fleet_submit: refused -- LOCAL_LOOP control messages are not Fleet goals. "
            "Repair/publish the LOCAL_LOOP Agent Instructions instead of routing RUN/control "
            "messages back into Fleet.]"
        )
    # NEAR-DUPLICATE, with two gates layered (both #293 and #294 survive here):
    #
    #   * A CONTINUATION IS EXEMPT ENTIRELY (#294). resume_conv names an existing conversation
    #     this goal is meant to CONTINUE, and a follow-up is deliberately short and terse
    #     ("write the changelog", "now do the other half") -- exactly the shape _duplicate_of
    #     reads as a resubmission of something already in flight. Refusing it is the specific
    #     way a continuation stopped being a continuation: the terse second turn was rejected
    #     as a near-duplicate of the first, and the conversation it meant to resume was never
    #     answered. So a goal carrying resume_conv skips the duplicate machinery altogether --
    #     no match is computed, no tag is written -- and dup stays None so the tag/dup-note
    #     block below is a no-op for it. The worker keeps the conversation apart by its id,
    #     which is what the overlap heuristic was standing in for.
    #
    #   * OTHERWISE, refuse ONLY when the guard is explicitly on; else accept and record in the
    #     background (owner order 2026-10-26, #293). _duplicate_of still runs when the guard is
    #     off, because its answer is what the background note needs -- it just no longer gates
    #     admission.
    dup = None
    if not resume_conv:
        dup = _duplicate_of(text)
        if dup is not None and _dup_guard_on():
            dup_jid, sim = dup
            _log_refusal("similar", text, source, similar_to=dup_jid)
            return ("[fleet_submit: refused -- this reads as the goal already queued or running "
                    "as %s (%d%% word overlap). If it is genuinely different work, say specifically "
                    "how; if you meant to check on that job, use fleet_queue() instead of "
                    "resubmitting it.]" % (dup_jid, round(sim * 100)))
    n = _pending_count()
    if n >= MAX_PENDING:
        _log_refusal("pending_full", text, source)
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
        # resume_conv RIDES ON THE PAYLOAD, BESIDE goal/priority, for the same reason priority
        # does: that is where the router reads a job's own fields before handing them to the
        # fleet, and from here it has to survive all three delivery routes as the goal-item
        # field `resume_conv`. Only set when non-empty, so an ordinary (non-continuation) job's
        # record is unchanged and `resume_conv not in payload` keeps meaning "start fresh".
        "payload": dict({"goal": text, "note": _clean(note, 500), "priority": bool(priority)},
                        **({"resume_conv": resume_conv} if resume_conv else {})),
        "created": time.time(),
        # PROVENANCE TRAVELS WITH THE JOB. This arrived over a tunnel from an agent, which is
        # not the same authority as a person typing into the cockpit, and the consumer is
        # entitled to treat it differently. Recording it here means the difference survives
        # into the queue instead of being lost at the door.
        "origin": {"via": "mcp", "source": _clean(source or "agent", MAX_SOURCE_CHARS)},
    }
    # ACCEPTED-BUT-SIMILAR: tag the job itself so a reader of the queue sees the resemblance
    # without opening the dup-notes log. Only set when a near-duplicate was found AND it was not
    # refused (guard off); a refused one never reaches here. The dup-notes line is written AFTER
    # the job is safely on disk, so a failed note never leaves a tagged job the log does not
    # mention, and a failed write never produces an orphan note.
    if dup is not None:
        job["payload"]["possible_duplicate_of"] = dup[0]
        job["payload"]["similarity"] = round(float(dup[1]), 4)
    try:
        TR.ensure_dirs()
        path = os.path.join(TR.TASKS, "pending", "%s.json" % jid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(job, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as exc:
        _log_refusal("other", text, source)
        return "[fleet_submit: could not queue the goal: %s: %s]" % (type(exc).__name__, exc)

    if dup is not None:
        _note_possible_duplicate(jid, dup[0], dup[1], text)

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
            "%d job(s) waiting. %s" % (jid, _waiting_count(), _consumer_sentence()))


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
