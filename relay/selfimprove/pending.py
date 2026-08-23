"""Decisions that fell outside the standing delegation, kept until somebody decides them.

WHAT THIS IS FOR. Re-signing the frozen constitution is delegated; re-signing the machinery
that defines the delegation is not, so `frozen.py` refuses those and prints what it would have
needed. The refusal is correct. What happened next was not: the agent reported it, the turn
ended, and unless the operator remembered, the proposal was gone. Two were nearly lost in one
day that way -- a message in tools/security.py that instructed an impossible action, and a
missing undo hint in the frozen CLI's own output.

This does not add enforcement and must not be described as if it did. Everything here runs in
the same privilege domain as the agent: the queue can be written, read and emptied by the same
process that fills it. What it buys is that "stopped" becomes "queued" -- the proposal survives
the turn, it is visible without anyone remembering it exists, and consuming one leaves a record.

APPEND-ONLY, and stored under .fleet, which is untracked. Entries quote proposed diffs to
security-relevant files; that is runtime state, not something to publish. Resolving an item
appends a row rather than rewriting one, for the same reason the authority ledger does: a
queue that can be edited in place cannot say what was in it yesterday.

CLI:
    python -m relay.selfimprove.pending --list
    python -m relay.selfimprove.pending --resolve <id> --authorization "<the operator's words>"
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Runtime state, deliberately untracked: entries quote proposed diffs to the files the
#: delegation excludes, and those diffs are not for publication.
QUEUE_PATH = os.path.join(_REPO, ".fleet", "selfimprove", "pending_decisions.jsonl")

OPEN = "open"
DONE = "done"
DROPPED = "dropped"


def _key(files, reason: str) -> str:
    """Identity of a PROPOSAL, not of an attempt.

    A refused re-signing is usually retried, and a queue that grows a row per attempt buries
    the thing it exists to surface. Two attempts at the same change on the same files are one
    pending decision.
    """
    h = hashlib.sha256()
    h.update("\n".join(sorted(str(f) for f in (files or []))).encode("utf-8"))
    h.update(b"\x00")
    h.update((reason or "").strip().encode("utf-8"))
    return h.hexdigest()[:12]


def _rows() -> list:
    try:
        with io.open(QUEUE_PATH, encoding="utf-8") as fh:
            out = []
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue          # several processes append here; a torn tail is normal
                if isinstance(rec, dict):
                    out.append(rec)
            return out
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _append(rec: dict) -> None:
    os.makedirs(os.path.dirname(QUEUE_PATH) or ".", exist_ok=True)
    with io.open(QUEUE_PATH, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def add(files, reason: str, *, diff: str = "", command: str = "", ts=None,
        notify: bool = True) -> str:
    """Queue a proposal that needs a decision. Returns its id. Never raises.

    `command` is what the operator would run to accept it, with the authorization left for
    them to fill in. A queue entry that does not say how to act on it is a reminder, and
    reminders are what this replaces.
    """
    try:
        pid = _key(files, reason)
        if any(r.get("id") == pid and r.get("event") == "queued" for r in _rows()):
            return pid                      # already standing; retrying is not a new decision
        _append({
            "event": "queued",
            "id": pid,
            "ts": float(ts if ts is not None else time.time()),
            "files": sorted(str(f) for f in (files or [])),
            "reason": (reason or "").strip(),
            "diff": diff or "",
            "command": command or "",
        })
        if notify:
            _notify(pid, files, reason)
        return pid
    except Exception:
        return ""


def status_of(pid: str) -> str:
    """OPEN until a resolution row appears for it; the last resolution wins."""
    state = ""
    for r in _rows():
        if r.get("id") != pid:
            continue
        if r.get("event") == "queued" and not state:
            state = OPEN
        elif r.get("event") == "resolved":
            state = r.get("status") or DONE
    return state


def items(include_resolved: bool = False) -> list:
    """Queued proposals, newest last, each carrying its current status."""
    queued = {}
    order = []
    for r in _rows():
        pid = r.get("id")
        if r.get("event") == "queued" and pid not in queued:
            queued[pid] = dict(r, status=OPEN)
            order.append(pid)
        elif r.get("event") == "resolved" and pid in queued:
            queued[pid]["status"] = r.get("status") or DONE
            queued[pid]["resolved_ts"] = r.get("ts")
            queued[pid]["authorization"] = r.get("authorization") or ""
    out = [queued[p] for p in order]
    if not include_resolved:
        out = [i for i in out if i.get("status") == OPEN]
    return out


def resolve(pid: str, *, authorization: str = "", status: str = DONE, ts=None) -> bool:
    """Record that a queued decision was acted on, and on whose word. Never raises.

    The authorization is stored exactly as given and is NOT verified -- nothing here can
    verify it, and the authority ledger already says so about itself. What it buys is that a
    consumed item can be read back later and checked against what the operator remembers.
    """
    try:
        if not any(r.get("id") == pid and r.get("event") == "queued" for r in _rows()):
            return False
        _append({
            "event": "resolved",
            "id": pid,
            "ts": float(ts if ts is not None else time.time()),
            "status": status if status in (DONE, DROPPED) else DONE,
            "authorization": (authorization or "").strip(),
        })
        return True
    except Exception:
        return False


def _notify(pid: str, files, reason: str) -> None:
    """Say it once, and land the reader on the screen that lists these. Never raises.

    Deliberately not a file of commands to paste: that was the previous shape of this path and
    the operator's summary of it was "and then what am I supposed to do with it".
    """
    try:
        from tools.notify_ops import notify_desktop, open_authority_dashboard
        head = ", ".join(sorted(str(f) for f in (files or []))) or "(unknown files)"
        body = (reason or "").strip()
        if len(body) > 160:
            body = body[:157] + "..."
        notify_desktop(title="判断待ち: " + head,
                       body=(body + "\n自己改善ダッシュボードに一覧があります。"))
        open_authority_dashboard()
    except Exception:
        pass


def _cli(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show pending decisions")
    ap.add_argument("--all", action="store_true", help="include resolved ones")
    ap.add_argument("--resolve", metavar="ID", default="",
                    help="mark a queued decision as acted on")
    ap.add_argument("--drop", metavar="ID", default="",
                    help="mark a queued decision as not going to happen")
    ap.add_argument("--authorization", default="",
                    help="the operator's decision, quoted verbatim")
    args = ap.parse_args(argv)

    if args.resolve or args.drop:
        pid = args.resolve or args.drop
        ok = resolve(pid, authorization=args.authorization,
                     status=DONE if args.resolve else DROPPED)
        print(("resolved: " if ok else "no such queued decision: ") + pid)
        return 0 if ok else 2

    rows = items(include_resolved=args.all)
    if not rows:
        print("nothing pending.")
        return 0
    for r in rows:
        print("%s  [%s]  %s" % (r.get("id"), r.get("status"), ", ".join(r.get("files") or [])))
        print("    %s" % (r.get("reason") or ""))
        if r.get("command"):
            print("    $ %s" % r["command"])
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
