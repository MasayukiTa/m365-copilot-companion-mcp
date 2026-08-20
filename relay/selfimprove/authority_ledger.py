"""An append-only, hash-chained record of the acts that change what this system MAY BECOME.

Not to be confused with `ledger.py`, which records what an experiment PREDICTED before it
looked. That one guards against rewriting a hypothesis to match the result; this one records
the acts that change the system's own permissions -- reblessing the constitution, applying a
genome. Two different ledgers because they answer two different questions, and merging them
would put "what we expected of run 41" next to "who decided the judge could change".

WHAT THIS IS NOT, SAID FIRST BECAUSE IT IS THE PART THAT GETS MISREAD

This ledger does not authorise anything and it cannot stop anything. An external review
settled the limit of what a single-machine design can buy: the mechanism that constrains an
agent runs in the same privilege domain as the agent, so every approval device here is
advisory. That verdict applies to this file too. It is written where this process can write,
by code this process can edit, so an actor able to rewrite history can rewrite this.

Given that, the thing worth buying is not enforcement -- which is not for sale at this scale
-- but ACCOUNTABILITY: that whatever happened can be reconstructed afterwards. The frozen
guard already detects an altered judge. What was missing is any record of who intended the
change and why, and the two are different questions.

The four sentences the genesis record carries are the contract, and they are there because
each names a real way to misread a ledger:

    actor is self-reported and is not verified.
    This ledger authorises nothing.
    The presence of a record does not make the act legitimate.
    The absence of a record does not mean the act did not happen.

The first two block reading "recorded" as "permitted". The last two block reading the ledger
as complete.

WHAT IT RECORDS

Only acts that change what the system may become, plus the one observation that explains why
such an act happened:

    rebless           the frozen constitution's baseline was re-signed
    rebless_revoke    a previous re-signing was withdrawn, restoring the baseline before it
    genome_apply      a genome was applied to the running scaffold
    genome_revert     an applied genome was withdrawn
    baseline_mismatch the frozen set was found altered

The mismatch belongs here even though it is an observation rather than an act. A rebless
record's `reason` is usually "the baseline no longer matched", and without the mismatch
recorded independently that reason is an unverifiable self-report. Successful verifications
are NOT recorded: a ledger nobody reads because it is mostly noise protects nothing.

WHY THE TAIL IS PRINTED

Truncation cannot be prevented here. Anything that can append can also rewrite the file from
some earlier point and continue -- the hash chain catches an edit in the middle and does not
catch a shortened tail, and closing that needs storage this process cannot write, which is
exactly the kind of control that only holds under an operating arrangement nobody guaranteed.
One cheap thing does help: every recorded act prints its sequence number and tail hash to the
operator. That output lands somewhere outside this process's reach, which makes it a weak
external anchor at no cost. It is not a solution and is not described as one.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time

SCHEMA_VERSION = 1

#: Beside the frozen anchor rather than in the repository: a tracked ledger would publish
#: whatever the system has learned, an untracked one inside the tree is lost to any clean
#: checkout, and both roots of trust belong in one place. This is NOT a security boundary --
#: this process writes here already.
DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".selfimprove_ledger.jsonl")

#: Redirectable, and RESOLVED AT CALL TIME rather than bound as a default argument. A default
#: of `path=DEFAULT_PATH` fixes the module attribute at import, so nothing can point this
#: elsewhere afterwards -- frozen.py carries a comment about being bitten by exactly that.
#: Here it had a second consequence: with no way to redirect, running the test suite would
#: have appended to the operator's real ledger, which is the one file whose whole value is
#: that its contents were not manufactured.
ENV_PATH = "MCP_SELFIMPROVE_LEDGER"


def _path(path=None) -> str:
    return path or os.environ.get(ENV_PATH) or DEFAULT_PATH

REBLESS = "rebless"
#: SEPARATE FROM REBLESS ON PURPOSE, because their post-conditions are opposites: after a
#: rebless the frozen set verifies, and after a revoke it does NOT -- the approval is gone
#: while the files are still changed, which is the whole point of withdrawing it. Filing both
#: under one type would make any reader that reconstructs state from this ledger branch on a
#: field instead of a type, and that branch is where such readers rot.
REVOKE = "rebless_revoke"
GENOME_APPLY = "genome_apply"
GENOME_REVERT = "genome_revert"
BASELINE_MISMATCH = "baseline_mismatch"
GENESIS = "genesis"
EVENTS = (REBLESS, REVOKE, GENOME_APPLY, GENOME_REVERT, BASELINE_MISMATCH)

#: Carried in the genesis record so it travels with the file rather than living only here.
CONTRACT = (
    "actor is self-reported and is not verified.",
    "This ledger authorises nothing.",
    "The presence of a record does not make the act legitimate.",
    "The absence of a record does not mean the act did not happen.",
)

SELF_INITIATED = "self-initiated"


class AuthorityLedgerError(RuntimeError):
    """Raised when a record cannot be written honestly, or the chain does not hold."""


def _digest(record: dict) -> str:
    """The record's hash, over canonical JSON with the hash field itself excluded.

    Spelled out because a verifier cannot be written against a vague rule: sorted keys, no
    spaces, UTF-8, `hash` omitted. Any drift here silently invalidates every later record.
    """
    body = {k: v for k, v in record.items() if k != "hash"}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def read(path=None) -> list:
    path = _path(path)
    if not os.path.isfile(path):
        return []
    out = []
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def tail(path=None):
    """(seq, hash) of the last record, or (None, None) for an empty ledger."""
    rows = read(_path(path))
    if not rows:
        return (None, None)
    return (rows[-1].get("seq"), rows[-1].get("hash"))


def _genesis(path: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "seq": 0,
        "ts": time.time(),
        "event": GENESIS,
        "contract": list(CONTRACT),
        "prev_hash": None,
    }


def append(event: str, *, reason: str, actor_claimed: str, authorization: str = "",
           command: str = "", changed=None, baseline_before=None, path=None,
           now=None) -> dict:
    """Append one record and return it. Creates the genesis record on first use.

    `reason` is required and may not be blank. A record that cannot say why is the shape this
    ledger exists to replace -- the git history of the baseline file already says WHAT
    changed, and said nothing about why or on whose decision.

    `authorization` is the verbatim instruction that permitted the act, or SELF_INITIATED.
    Verbatim rather than summarised: a paraphrase written by the actor is the actor's opinion
    of its own mandate, and the one thing a reader most needs to judge for themselves.

    `actor_claimed` is named for what it is. Nothing verifies it.
    """
    if event not in EVENTS:
        raise AuthorityLedgerError("%r is not one of %s" % (event, ", ".join(EVENTS)))
    if not str(reason or "").strip():
        raise AuthorityLedgerError(
            "a ledger record needs a reason. The baseline's git history already records what "
            "changed; what was missing is why, and on whose decision")
    if not str(actor_claimed or "").strip():
        raise AuthorityLedgerError("a ledger record needs an actor, even an unverified one")

    path = _path(path)
    rows = read(path)
    if not rows:
        first = _genesis(path)
        first["hash"] = _digest(first)
        _write(path, first)
        rows = [first]

    prev = rows[-1]
    record = {
        "schema_version": SCHEMA_VERSION,
        "seq": int(prev.get("seq", 0)) + 1,
        "ts": float(now if now is not None else time.time()),
        "event": event,
        # NAMED FOR WHAT IT IS. Calling this `actor` would let the field's own name imply a
        # verification nothing performs.
        "actor_claimed": str(actor_claimed),
        "authorization": str(authorization or SELF_INITIATED),
        "reason": str(reason),
        "command": str(command or ""),
        "changed": dict(changed or {}),
        # Present only where a full prior state is needed to undo the act. The rebless record
        # carries the entire previous baseline so `revoke` can reconstruct it byte-exactly
        # from an append-only source rather than from a mutable file beside the target.
        "baseline_before": baseline_before,
        "prev_hash": prev.get("hash"),
    }
    record["hash"] = _digest(record)
    _write(path, record)
    _notify(record)
    return record


#: Events worth interrupting the operator for, and what each one means when it arrives.
#:
#: WHY THE HOOK IS HERE AND NOT IN frozen.py. Every one of these passes through `append`, so
#: this is the single chokepoint; and frozen.py is both frozen and excluded from the standing
#: delegation, so putting a peripheral feature there would mean rewriting the constitution to
#: add a notification. This module is neither, so wiring it costs nothing constitutional.
#:
#: WHY MISMATCH IS THE URGENT ONE. A re-signing is usually something the operator asked for.
#: A mismatch is the frozen set having CHANGED WITHOUT ONE -- until now that was written to
#: this file and nowhere else, so nobody learned of it unless they opened the dashboard. The
#: detection existed and the telling did not.
#:
#: WHY EVERY REBLESS AND NOT JUST THE UNUSUAL ONES. Under a standing delegation the operator
#: is not present for these, and the notification's whole value is that they can recognise a
#: re-signing they did not ask for -- including one whose `authorization` field quotes words
#: they never said. Filtering to "only the excluded paths" inverts that: those are precisely
#: the acts they DID specify, so the message carries the least information. Re-signings are
#: constitutional changes, not per-run events; if the volume ever becomes noise, the volume
#: is itself the thing to look at.
_URGENT = {BASELINE_MISMATCH}
_NOTIFIED = {REBLESS, REVOKE, BASELINE_MISMATCH}


def _headline(record) -> tuple:
    event = record.get("event")
    changed = ", ".join(sorted(record.get("changed") or {})) or "-"
    auth = record.get("authorization") or SELF_INITIATED
    if event == BASELINE_MISMATCH:
        return ("Frozen set changed without a re-signing",
                "%s\n\nNobody approved this. If it was not you, the judge has moved under a "
                "running system." % changed)
    if event == REVOKE:
        return ("A re-signing was withdrawn",
                "%s\n\nreason: %s" % (changed, record.get("reason")))
    return ("The constitution was re-signed",
            "%s\n\nreason: %s\nauthorization: %s\n\nIf you did not say this, nothing here "
            "verified that you did." % (changed, record.get("reason"), auth))


def _notify(record) -> None:
    """Tell the operator. Never raises, never blocks the record.

    A NOTIFICATION IS NOT A CONTROL, and this one is emitted from the same process as the act
    it reports -- an actor that can write the record can suppress the message. It exists so an
    act nobody asked for is noticed sooner than the next time somebody opens a dashboard, not
    so that it cannot happen.
    """
    if record.get("event") not in _NOTIFIED:
        return
    try:
        from tools.notify_ops import notify_desktop
        title, body = _headline(record)
        # NO `urgency` KWARG. notify_desktop takes (title, body, app_id, icon_path) and
        # passing an argument it does not have would raise a TypeError straight into the
        # except below -- a notification that silently never fires, which is the exact shape
        # of defect this whole notification exists to catch elsewhere. Urgency rides in the
        # title, which is the part a toast shows first.
        if record.get("event") in _URGENT:
            title = "! " + title
        notify_desktop(title=title, body=body)
    except Exception:
        pass


def _write(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def record_mismatch_once(changed, *, reason, actor_claimed, path=None) -> bool:
    """Record a frozen-set mismatch, unless the same one is already the latest mismatch.

    DEDUPED BECAUSE THE CHECK RUNS CONSTANTLY. Every run verifies the frozen set, so an
    unresolved mismatch would append a record per attempt and bury the acts this ledger exists
    to make findable -- and a ledger nobody reads because it is mostly noise protects nothing.

    The mismatch is recorded at all because a rebless record's `reason` is usually "the
    baseline no longer matched", and without an independent record of the mismatch that reason
    is the actor's own unverifiable account of why it was allowed to act.

    Returns True when a record was written.
    """
    key = sorted(changed or [])
    for row in reversed(read(path)):
        if row.get("event") == BASELINE_MISMATCH:
            if sorted((row.get("changed") or {}).keys()) == key:
                return False
            break
        if row.get("event") == REBLESS:
            break                      # a rebless closes the previous mismatch episode
    try:
        append(BASELINE_MISMATCH, reason=reason, actor_claimed=actor_claimed,
               changed={rel: {"before": None, "after": None} for rel in key}, path=path)
        return True
    except Exception:
        return False


def verify(path=None) -> tuple:
    """(ok, problems). Checks the chain links and the sequence, nothing more.

    WHAT IT CANNOT SEE, stated so nobody quotes an OK as more than it is: a ledger rewritten
    from some point and re-chained verifies clean, and so does one whose tail was removed. A
    hash chain proves internal consistency, not that nothing was dropped.
    """
    rows = read(_path(path))
    problems = []
    if not rows:
        return (True, ["the ledger is empty; that is indistinguishable from one never used"])
    if rows[0].get("event") != GENESIS or rows[0].get("seq") != 0:
        problems.append("the first record is not the genesis record")
    prev_hash = None
    for i, row in enumerate(rows):
        want = _digest(row)
        if row.get("hash") != want:
            problems.append("record %s: hash does not match its contents" % row.get("seq"))
        if row.get("prev_hash") != prev_hash:
            problems.append("record %s: does not link to the previous record" % row.get("seq"))
        if row.get("seq") != i:
            problems.append("record %s: sequence is not contiguous (expected %d)"
                            % (row.get("seq"), i))
        prev_hash = row.get("hash")
    return (not problems, problems)


def describe_tail(path=None) -> str:
    """One line for the operator's terminal -- the weak external anchor described above."""
    path = _path(path)
    seq, digest = tail(path)
    if seq is None:
        return "ledger: empty"
    return "ledger: seq=%d tail=%s (%s)" % (seq, str(digest)[:16], path)
