"""Who owns the browser resources, recorded with an expiry and reconciled against reality.

WHY THIS EXISTS. Three of the costliest defects in one day were the same thing: a resource
with no recorded owner. A finished run left a Copilot page open and it sat for nine and a half
hours (the browser measured 341 MB median without such a page and 697 MB with one, across
4,549 samples of which 61.6% had one). A measurement script's browser had no teardown and
nobody responsible, found idling at 331 MB. And a run began on top of the previous run's
residue, so every memory figure taken during it was contaminated.

THE TRAP THIS DESIGN IS BUILT AROUND. Recording ownership is not obviously an improvement over
deriving it. A process killed before it can write "finished" leaves a ledger claiming a run is
alive for ever, and a launch gate that believes the ledger then refuses every future run --
turning a leak into a lockout. Deriving state at least self-corrects.

So nothing here is believed on its own:

  * every claim carries a run id and a LEASE, so a claim that stopped being renewed expires;
  * a claim is only honoured while its pid is actually alive, checked against the process
    table, which is the one source that cannot go stale;
  * reconcile() compares the ledger with the machine and returns what is genuinely orphaned,
    so the answer is the intersection of what was recorded and what is real -- never either
    one alone.

The ledger says what SHOULD be owned. The process table says what IS. Only where they
disagree is there anything to do.
"""
from __future__ import annotations

import json
import os
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(REPO, ".fleet", "ownership.jsonl")

#: How long a claim stands without being renewed. Longer than any sweep interval and far
#: shorter than the nine and a half hours a leaked page once sat unnoticed: an owner that has
#: not spoken for this long is not an owner, whatever the file says.
LEASE_S = float(os.environ.get("MCP_OWNERSHIP_LEASE_S", "300"))


def _now():
    return time.time()


def claim(kind: str, key: str, *, run_id: str, pid: int = None, note: str = "") -> dict:
    """Record that `run_id` (running as `pid`) owns `key`, for LEASE_S from now.

    Appended rather than rewritten: an append cannot lose an existing claim if the process
    dies mid-write, and the reader already has to cope with several claims for one key.
    """
    rec = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ts": _now(),
        "kind": kind,
        "key": key,
        "run_id": run_id,
        "pid": int(pid if pid is not None else os.getpid()),
        "expires": _now() + LEASE_S,
        "note": note[:200],
        "state": "held",
    }
    _append(rec)
    return rec


def release(kind: str, key: str, *, run_id: str) -> dict:
    """Record that the claim is over. Idempotent -- releasing twice is not an error."""
    rec = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ts": _now(),
        "kind": kind,
        "key": key,
        "run_id": run_id,
        "pid": os.getpid(),
        "state": "released",
    }
    _append(rec)
    return rec


def renew(kind: str, key: str, *, run_id: str) -> dict:
    """Push the lease out. A long run must say it is still here, or its claim expires."""
    return claim(kind, key, run_id=run_id, note="renew")


def _append(rec: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass                      # a ledger write must never take down a run


def read_claims(path: str = None) -> dict:
    """The current claim per (kind, key): last record wins, released keys drop out."""
    out = {}
    try:
        with open(path or LEDGER, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                k = (rec.get("kind"), rec.get("key"))
                if rec.get("state") == "released":
                    out.pop(k, None)
                else:
                    out[k] = rec
    except OSError:
        pass
    return out


def live_claims(alive, path: str = None, now: float = None) -> dict:
    """Claims that are still both unexpired AND owned by a living process.

    `alive(pid) -> bool` is injected so this is testable and so the caller decides how to ask
    the machine. A claim failing either test is not a live claim: the lease covers a process
    that died without releasing, and the pid check covers a lease renewed shortly before the
    owner was killed.
    """
    now = _now() if now is None else now
    live = {}
    for k, rec in read_claims(path).items():
        if float(rec.get("expires") or 0) < now:
            continue
        try:
            if not alive(int(rec.get("pid") or 0)):
                continue
        except Exception:
            continue
        live[k] = rec
    return live


def reconcile(observed, alive, path: str = None, now: float = None) -> dict:
    """Compare what exists with what is claimed. Returns what to do about the difference.

    `observed` is {(kind, key): description} for what the machine actually has right now.

    Returns {"orphaned": {...}, "claimed": {...}, "stale": {...}}:
      orphaned -- exists, and no live claim covers it. This is the leak.
      claimed  -- exists and is covered. Leave it alone.
      stale    -- claimed but does not exist. The ledger is behind; nothing to clean.

    Deliberately returns rather than acts. The caller decides whether an orphan is closed, and
    a function that both decides and destroys is one nobody can test safely.
    """
    live = live_claims(alive, path=path, now=now)
    orphaned, claimed = {}, {}
    for key, desc in (observed or {}).items():
        (claimed if key in live else orphaned)[key] = desc
    stale = {k: v for k, v in live.items() if k not in (observed or {})}
    return {"orphaned": orphaned, "claimed": claimed, "stale": stale}
