# -*- coding: utf-8 -*-
"""Operator tool: promote an organisation version, and roll its definition TEXT back (slice D6, D6-b).

It appends to `<fleet dir>/org_promotions.jsonl` (relay/org/promotion.py) and keeps the text of
the overridable parts under `<fleet dir>/org_defs/` (relay/org/def_store.py). Nothing calls this
script except a person. There is no approval card, daemon, poll or schedule: a rollback runs
when this command runs.

  python scripts/org_promotion.py --fleet-dir DIR status
  python scripts/org_promotion.py --fleet-dir DIR promote [--evidence TEXT] [--note TEXT]
  python scripts/org_promotion.py --fleet-dir DIR rollback --reason TEXT [--to GENOTYPE_ID] [--part LABEL]

`promote` records the version in the source right now and stores the text of its overridable
parts (and the text of the version promoted before it). `rollback` restores the stored text of
the earlier version into the override store and appends one ledger line; it refuses, writing
nothing, when that text was never stored. The restored text takes effect for the NEXT process to
start: a definition module reads the store once, at import. Only relay.planner.PLAN_PROMPT can be
restored; any other --part is refused. `status` also shows whether the source still matches the
ledger's current version (drift). Exit status 0 on success, 1 when refused.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

DEFAULT_PART = "relay.planner.PLAN_PROMPT"
_WARN_OFF = ("warning: org_version_record is off, so runs do not record which version they ran "
             "under; the rollback still proceeds")


def defs_dir(fleet_dir) -> str:
    return os.path.join(os.path.abspath(str(fleet_dir)), "org_defs")


def current_parts(labels=None) -> dict:
    """label -> text of the overridable parts as this process holds them right now."""
    import importlib
    from relay.org import def_store
    out = {}
    for label in (labels or def_store.OVERRIDABLE):
        module, attr = label.rsplit(".", 1)
        out[label] = getattr(importlib.import_module(module), attr)
    return out


def promote_with_text(fleet_dir, snap, evidence="", note="", parts=None, now=None) -> dict:
    """Store the text of the overridable parts, then record the promotion. Nothing is written to
    the ledger when the text cannot be stored."""
    from relay.org import def_store, definitions, promotion
    path = promotion.ledger_path(fleet_dir)
    gid = definitions.genotype_id(snap)
    if not gid:
        return {"status": "refused", "reason": "the version could not be read in full"}
    st = promotion.status(path)
    if st["current"] == gid:
        return {"status": "refused", "reason": "already the current version"}
    try:
        def_store.save_version(gid, current_parts() if parts is None else parts,
                               previous_id=st["current"], directory=defs_dir(fleet_dir))
    except (def_store.StoreError, OSError) as exc:
        return {"status": "refused", "reason": "the text could not be stored: %s" % (exc,)}
    return promotion.promote(path, snap, evidence, note, now=now)


def rollback_text(fleet_dir, reason, to=None, part=DEFAULT_PART, now=None) -> dict:
    """Restore the stored text of the earlier version and append one ledger line.

    Refused, with the ledger and the override store untouched, when `part` is not overridable,
    when the ledger would refuse, or when the target's text was never stored. Directly executed:
    no approval step."""
    from relay.org import def_store, promotion
    try:
        def_store.check_label(part)
    except def_store.StoreError as exc:
        return {"status": "refused", "reason": str(exc)}
    path = promotion.ledger_path(fleet_dir)
    directory = defs_dir(fleet_dir)
    st = promotion.status(path)
    target = str(to) if to else st["previous"]
    if not str(reason or "").strip():
        return {"status": "refused", "reason": "a reason is required"}
    if not st["current"]:
        return {"status": "refused", "reason": "nothing has been promoted"}
    if not target:
        return {"status": "refused", "reason": "there is no earlier version to return to"}
    if target == st["current"]:
        return {"status": "refused", "reason": "that is already the current version"}
    if target not in st["known"]:
        return {"status": "refused", "reason": "that version is not in the ledger"}
    try:
        stored = def_store.load_version(target, directory)
        if part not in stored["parts"]:
            return {"status": "refused",
                    "reason": "version %s has no stored text for %s" % (target, part)}
        old = def_store.read_overrides(directory)
        new = dict(old)
        new[part] = stored["parts"][part]
        def_store.write_overrides(new, directory)
    except (def_store.StoreError, OSError) as exc:
        return {"status": "refused", "reason": str(exc)}
    res = promotion.rollback(path, reason, to=target, now=now)
    if res.get("status") != "recorded":
        try:                                     # the ledger did not take it: put the store back
            if old:
                def_store.write_overrides(old, directory)
            else:
                os.unlink(os.path.join(directory, def_store.OVERRIDES))
        except OSError:
            pass
        return res
    res["part"] = part
    res["takes_effect"] = "next process start"
    return res


def main(argv=None) -> int:
    from relay.org import definitions, promotion, versions
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fleet-dir", required=True, help="state directory that holds the ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("promote")
    p.add_argument("--evidence", default="")
    p.add_argument("--note", default="")
    r = sub.add_parser("rollback")
    r.add_argument("--reason", required=True)
    r.add_argument("--to", default="")
    r.add_argument("--part", default=DEFAULT_PART)
    args = ap.parse_args(argv)
    path = promotion.ledger_path(args.fleet_dir)
    if args.cmd == "status":
        out = dict(promotion.status(path))
        out["drift"] = promotion.drift(path)
        res = out
        ok = True
    elif args.cmd == "promote":
        res = promote_with_text(args.fleet_dir, definitions.snapshot(), args.evidence, args.note)
        ok = res["status"] == "recorded"
    else:
        try:
            if versions.mode() != "on":
                print(_WARN_OFF, file=sys.stderr)
        except Exception:
            print(_WARN_OFF, file=sys.stderr)
        res = rollback_text(args.fleet_dir, args.reason, to=args.to or None, part=args.part)
        ok = res["status"] == "recorded"
    print(json.dumps(res, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
