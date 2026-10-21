"""Record of which organisation version was promoted, and of rolling one back (slice D6, record-only).

WHAT DECISION THIS SERVES. Whether promotion and rollback of a role/protocol version (D6) can be
run safely at all: before anything is ever swapped, keep the ledger that a swap would need. It
answers "which version is the promoted one now, which one came before it, and what would a
rollback return to", using the genotype id that relay/org/definitions.genotype_id already gives a
version (the pair of role and protocol hashes folded into one name).

WHAT THIS DOES NOT DO. It never edits a prompt, a role, a workflow, or any file under
relay/selfimprove (those carry the frozen surface and the real apply/revert path; D6 of the plan
says to go through them, and this slice does not replace them). It does not decide a promotion:
the evidence is recorded as the caller states it, not judged here. Nothing in a run reads the
ledger, and no run writes it. It is written only by an operator running scripts/org_promotion.py,
so there is no setting and no behaviour of a live run that it could change.

THE LEDGER, `<fleet dir>/org_promotions.jsonl`, append-only, one JSON object per line:
  {"kind":"promote",  "ts", "genotype_id", "roles_hash", "workflows_hash", "evidence", "note"}
  {"kind":"rollback", "ts", "from_genotype", "to_genotype", "reason"}
No goal text and no prompt text.

THE EFFECTIVE SEQUENCE. A promote adds its version at the end; a rollback adds the version it
returns to at the end. The current version is the last entry. The default rollback target is the
nearest earlier entry that differs from the current one. Because a rollback is itself an entry,
rolling back twice goes forward again to the version that was just backed out of, the swap
behaviour relay/selfimprove/runtime_config.revert_active documents (a plain restore would leave
the backed-out version held nowhere). Nothing is ever removed from the ledger.

NO SIZE CAP. Lines are written only by a person running the script, a handful a day at most, and
no measurement of growth exists; a cap here would have no recorded basis, so there is none.

FAILURE-SAFE. Every function returns a result dict instead of raising; the ledger is never
rewritten. A line this version does not know is skipped, so an older reader tolerates a newer
writer.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time

FILENAME = "org_promotions.jsonl"
PROMOTE = "promote"
ROLLBACK = "rollback"

_LOCK = threading.Lock()


def ledger_path(fleet_dir) -> str:
    """Where the ledger lives for a state directory. The caller names the directory; no path to
    an operator's live state is fixed in this module."""
    return os.path.join(os.path.abspath(str(fleet_dir)), FILENAME)


def load(path):
    """(entries, unreadable) from one ledger. Read-only; a missing file is empty.

    entries  the lines of kind promote or rollback that carry their required fields, in file
             order. Unknown kinds are skipped without being counted (a newer writer is not
             damage); a line that is not a JSON object, or lacks a required field, is counted."""
    entries, bad = [], 0
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(obj, dict):
                    bad += 1
                    continue
                kind = obj.get("kind")
                if kind == PROMOTE:
                    ok = bool(obj.get("genotype_id")) and isinstance(obj.get("ts"), (int, float))
                elif kind == ROLLBACK:
                    ok = (bool(obj.get("from_genotype")) and bool(obj.get("to_genotype"))
                          and isinstance(obj.get("ts"), (int, float)))
                else:
                    continue
                if ok:
                    entries.append(obj)
                else:
                    bad += 1
    except OSError:
        pass
    return entries, bad


def sequence(entries) -> list:
    """The effective sequence of versions: each promote's id, each rollback's target, in order."""
    seq = []
    for e in entries:
        seq.append(str(e["genotype_id"] if e["kind"] == PROMOTE else e["to_genotype"]))
    return seq


def status(path) -> dict:
    """{"current", "previous", "known", "entries", "unreadable"}. `previous` is what a rollback
    with no explicit target would return to ("" when there is none)."""
    entries, bad = load(path)
    seq = sequence(entries)
    current = seq[-1] if seq else ""
    previous = ""
    for gid in reversed(seq[:-1]):
        if gid != current:
            previous = gid
            break
    return {"current": current, "previous": previous, "known": sorted(set(seq)),
            "entries": len(entries), "unreadable": bad}


def _append(path, rec) -> bool:
    data = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
    return True


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def promote(path, snap, evidence="", note="", now=None) -> dict:
    """Record that the version `snap` (definitions.snapshot()) is promoted. Writes one line.

    Refused, writing nothing, when the version is unreadable (empty genotype id, so it is never
    given a real name) or is already the current one. Applies nothing."""
    try:
        from relay.org import definitions
        gid = definitions.genotype_id(snap)
        if not gid:
            return {"status": "refused", "reason": "the version could not be read in full"}
        with _LOCK:
            if status(path)["current"] == gid:
                return {"status": "refused", "reason": "already the current version"}
            _append(path, {"kind": PROMOTE, "ts": float(time.time() if now is None else now),
                           "genotype_id": gid, "roles_hash": snap["roles_hash"],
                           "workflows_hash": snap["workflows_hash"],
                           "evidence": _one_line(evidence), "note": _one_line(note)})
        return {"status": "recorded", "genotype_id": gid}
    except Exception:
        return {"status": "error", "reason": "the promotion could not be recorded"}


def rollback(path, reason, to=None, now=None) -> dict:
    """Record a rollback to `to`, or to the previous version when `to` is not given.

    Refused, writing nothing, when no reason is given, when there is nothing earlier to return
    to, or when `to` was never in the ledger or is the current version. Applies nothing: the
    definitions in the source are not touched, and `drift` (below) shows whether they still
    differ from what the ledger says is current."""
    try:
        why = _one_line(reason)
        if not why:
            return {"status": "refused", "reason": "a reason is required"}
        with _LOCK:
            st = status(path)
            cur = st["current"]
            if not cur:
                return {"status": "refused", "reason": "nothing has been promoted"}
            target = str(to) if to else st["previous"]
            if not target:
                return {"status": "refused", "reason": "there is no earlier version to return to"}
            if target == cur:
                return {"status": "refused", "reason": "that is already the current version"}
            if target not in st["known"]:
                return {"status": "refused", "reason": "that version is not in the ledger"}
            _append(path, {"kind": ROLLBACK, "ts": float(time.time() if now is None else now),
                           "from_genotype": cur, "to_genotype": target, "reason": why})
        return {"status": "recorded", "from": cur, "to": target}
    except Exception:
        return {"status": "error", "reason": "the rollback could not be recorded"}


def drift(path, snap=None) -> dict:
    """Compare the ledger's current version with the definitions in the source right now.

    {"state": "in_sync" | "drift" | "empty" | "unreadable", "ledger": id, "source": id}.
    Read-only. `empty`: nothing promoted. `unreadable`: the source version could not be read."""
    try:
        from relay.org import definitions
        snap = definitions.snapshot() if snap is None else snap
        src = definitions.genotype_id(snap)
        cur = status(path)["current"]
        if not cur:
            state = "empty"
        elif not src:
            state = "unreadable"
        else:
            state = "in_sync" if src == cur else "drift"
        return {"state": state, "ledger": cur, "source": src}
    except Exception:
        return {"state": "unreadable", "ledger": "", "source": ""}
