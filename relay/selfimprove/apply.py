"""Genome APPLIER + frozen-safe git-commit helper for the self-improvement loop.

This is the SAFE half of the "apply a chosen genome" step (cf. bench/SELF_GROWTH_L4_DESIGN.md
sec 0 frozen judge + sec 1 autonomy ladder). The loop selects a genome from the archive
(relay/selfimprove/archive.py); this module RECORDS that selection in an applied-genome STORE and
provides the only path by which the loop is allowed to git-commit -- a path that REFUSES unless the
frozen constitution is intact and every touched file is in the scaffold allowlist (its "hands").

What this module does:
  - active_genome / apply_genome / revert  -- the applied-genome store (one level of undo)
  - safe_commit                            -- frozen-checked, allowlist-checked, dry-run-by-default

What this module deliberately does NOT do:
  - It does NOT edit relay/quality_cards.py (or any scaffold source). Applying a genome records
    INTENT only; the scaffold actually READING the applied genome (merging knobs/cards into
    quality_cards.py) is the DEFERRED parent step. A long measurement is currently re-importing
    quality_cards.py, and editing it mid-run would corrupt that run.

    TODO(parent, post-measurement): wire quality_cards.py to read active_genome() so applied knobs
    and card texts take effect. That merge is the parent's job AFTER the running measurement finishes
    -- do not do it here.

stdlib only; deterministic; no network; no BOM. frozen is imported READ-ONLY.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from typing import Iterable

from relay.selfimprove import authority_ledger as ledger, frozen

# Default applied-genome store, next to the other selfimprove state files.
DEFAULT_STORE = os.path.join(os.path.dirname(__file__), "active_genome.json")

# The "empty base" genome -- what active_genome() returns before anything has been applied. Mirrors
# the genome shape in archive.py (knobs + cards + parent_id + note).
_BASE_GENOME = {"knobs": {}, "cards": {}, "parent_id": None, "note": "base"}

# Paths the loop is ALLOWED to commit -- the scaffold "hands" (sec 0: the agent may edit the SCAFFOLD,
# never the JUDGE). Anything also present in frozen.FROZEN_MANIFEST is FORBIDDEN even if listed here;
# the allowlist is necessary, not sufficient. Paths are repo-relative, forward-slash.
SCAFFOLD_ALLOWLIST = [
    "relay/selfimprove/active_genome.json",
    "relay/quality_cards.py",
    "relay/coding_discipline.py",
    "bench/swe_batch_setup.py",
]


# --------------------------------------------------------------------------------------------------
# Applied-genome store
# --------------------------------------------------------------------------------------------------


def _base_copy() -> dict:
    """A fresh copy of the empty base genome (so callers can't mutate the module constant)."""
    return {
        "knobs": {},
        "cards": {},
        "parent_id": None,
        "note": "base",
    }


# -- the ledger -------------------------------------------------------------------------------
# Applying a genome changes what the running scaffold IS, and until now it left only the store
# file behind: what the genome says, and nothing about when it was applied or why. The ledger
# records the act. It does not authorise it and cannot prevent it -- see ledger.py's opening
# note, which applies to every caller including this one.
#
# NEVER RAISES INTO THE CALLER. A ledger that can break an apply would be a new failure mode
# bought for a record-keeping benefit, and the first unwritable home directory would take the
# self-improvement loop down with it. A record that could not be written is worse than no
# ledger only if somebody believes the ledger is complete, which is exactly what its own
# contract says not to believe.
def _store_digest(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()[:16]
    except OSError:
        return None


def _record(event, *, reason, changed=None):
    try:
        ledger.append(event, reason=reason,
                      # SELF-REPORTED, and the loop says so about itself. Nothing here can
                      # verify who ran it, and a field that named a human would be a claim
                      # this code is in no position to make.
                      actor_claimed="relay.selfimprove.apply",
                      authorization=os.environ.get("MCP_SELFIMPROVE_AUTHORIZATION", "")
                                    or ledger.SELF_INITIATED,
                      command="apply_genome/revert (in-process)",
                      changed=changed)
    except Exception:
        pass



def active_genome(store_path: str = DEFAULT_STORE) -> dict:
    """Return the currently-applied genome, or the empty base if no store exists yet.

    A missing or unreadable store maps to the base genome {"knobs": {}, "cards": {}, "parent_id":
    None, "note": "base"} -- a clean default the scaffold can always read.
    """
    if not os.path.isfile(store_path):
        return _base_copy()
    try:
        with open(store_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _base_copy()
    return data if isinstance(data, dict) else _base_copy()


def apply_genome(genome: dict, store_path: str = DEFAULT_STORE) -> dict:
    """Record `genome` as the currently-applied genome and return it.

    Backs up the current store to <store>.prev first (one level of undo for revert()), then writes
    `genome` as pretty JSON with a trailing newline and no BOM. This is the only "apply" -- it records
    INTENT. The scaffold actually reading the store (merging into quality_cards.py) is the deferred
    parent step (see module docstring TODO).
    """
    prev_path = store_path + ".prev"
    before = _store_digest(store_path)
    if os.path.isfile(store_path):
        shutil.copyfile(store_path, prev_path)
    os.makedirs(os.path.dirname(store_path) or ".", exist_ok=True)
    with open(store_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(genome, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    _record(ledger.GENOME_APPLY,
            reason=str(genome.get("note") or "no note on the genome"),
            changed={store_path: {"before": before, "after": _store_digest(store_path)}})
    return genome


def revert(store_path: str = DEFAULT_STORE) -> bool:
    """Restore the store from <store>.prev if it exists. One level of undo.

    Returns True if a backup existed and was restored, False otherwise. The backup is left in place.
    """
    prev_path = store_path + ".prev"
    if not os.path.isfile(prev_path):
        return False
    before = _store_digest(store_path)
    shutil.copyfile(prev_path, store_path)
    _record(ledger.GENOME_REVERT, reason="one level of undo from %s" % prev_path,
            changed={store_path: {"before": before, "after": _store_digest(store_path)}})
    return True


# --------------------------------------------------------------------------------------------------
# Frozen-safe commit helper
# --------------------------------------------------------------------------------------------------


def _allowed(path: str) -> bool:
    """True iff `path` is in the scaffold allowlist AND not in the frozen manifest."""
    return path in SCAFFOLD_ALLOWLIST and path not in frozen.FROZEN_MANIFEST


def safe_commit(paths: Iterable[str], message: str, *, repo: str = frozen.REPO,
                baseline_path: str | None = None, dry_run: bool = True) -> dict:
    """Commit scaffold paths -- but only if the frozen constitution is intact and every path is a
    scaffold "hand". Default-safe: dry_run defaults True, so nothing is committed unless the caller
    explicitly passes dry_run=False.

    Order of checks:
      1. frozen check -- frozen.frozen_intact(repo, baseline). If the frozen set changed, REFUSE
         (the loop may be reward-hacking; sec 0).
      2. allowlist check -- every path must be in SCAFFOLD_ALLOWLIST and not in FROZEN_MANIFEST.
      3. dry_run (default) -- report what WOULD be committed; do NOT run git.
      4. dry_run=False -- `git -C repo add <paths>` then `git -C repo commit -m message`.

    Returns a dict; "ok" is False on any refusal (and nothing is committed).
    """
    paths = list(paths)

    # 1. frozen check
    ok, changed = frozen.frozen_intact(repo, baseline_path or frozen.DEFAULT_BASELINE)
    if not ok:
        return {"ok": False, "reason": "frozen set changed: %s" % changed, "committed": False}

    # 2. allowlist check
    for p in paths:
        if not _allowed(p):
            return {
                "ok": False,
                "reason": "path not in scaffold allowlist / is frozen: %s" % p,
                "committed": False,
            }

    # 3. dry run (default) -- never touch git
    if dry_run:
        return {"ok": True, "committed": False, "would_commit": paths, "message": message}

    # 4. real commit -- only when the caller explicitly insisted
    subprocess.run(["git", "-C", repo, "add", *paths], check=False)
    rc = subprocess.run(["git", "-C", repo, "commit", "-m", message], check=False).returncode
    return {"ok": True, "committed": True, "paths": paths, "rc": rc}
