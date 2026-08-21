"""Named refs into the archive, so an operator can hold more than one line and compare them.

WHAT A BRANCH IS, AND WHAT IT DELIBERATELY IS NOT

A branch is a LABEL POINTING AT AN ARCHIVE ROW. Nothing more. git solved this shape already:
a branch is a named mutable ref to an immutable commit, and the commit hash does not contain
the branch name. The same split is what keeps this honest --

    the label       mutable, human, may be deleted and re-made, never enters a hash
    the genome_id   immutable, content-addressed, the thing the archive actually holds

so a genome is never copied here. The archive is append-only, which makes `genome_id -> genome`
a fixed function, and resolving a ref is therefore deterministic. The moment a branch file held
its own copy of a genome there would be two answers to "what is branch X", and the interesting
question would become which one is stale.

WHY LABELS AT ALL

They are not decoration. Without them an operator compares `d4470fbca8af` against
`942eb26c19d2` and, within a day, is running on a branch nobody remembers creating -- which is
the failure mode branches introduce and the one thing this module has to prevent rather than
cause. `describe_active()` exists for exactly that: it resolves whatever harness is live back to
a name, and says so plainly when there isn't one.

`main` IS NOT A BRANCH HERE, AND THAT IS THE POINT

The base harness is CONSTRUCTED (`manifest.base_manifest()`), not remembered, so it cannot be
lost however many branches exist or vanish. Making it a ref would give it a second definition
that could drift from the code, and `reset_to_base()` -- the one way home -- would start
meaning "go to whatever that ref says" instead of "go to what shipped". The name is reserved so
nobody can create that ambiguity.

NOTHING HERE ACTIVATES ANYTHING

Resolving a branch produces a manifest and a path a CHILD PROCESS can be pointed at through
`MCP_HARNESS_MANIFEST`. No function in this module writes the active manifest. That is the
invariant that keeps "main is always reachable" true: a comparison changes what a subprocess
runs and never what this machine runs.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from relay.selfimprove import manifest as M

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(REPO, ".fleet", "selfimprove", "branches.json")

#: Reserved. See the module docstring: base is constructed, and a ref for it would be a second
#: definition able to drift from the code.
RESERVED = frozenset({"main", "base", "HEAD", "head"})

#: A hard ceiling rather than a pruning policy. Deleting a ref loses only a name -- the archive
#: row survives and the branch can be re-made from the same genome_id -- so keeping the number
#: small costs nothing and stops the drawer filling with lines nobody can account for.
MAX_BRANCHES = 5

_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


class BranchError(RuntimeError):
    """A branch operation that must not silently half-happen."""


def _path(path=None) -> str:
    return path or os.environ.get("MCP_SELFIMPROVE_BRANCHES", "").strip() or DEFAULT_PATH


def read(path=None) -> dict:
    """Every ref, as {label: {genome_id, created_at, last_run_at, note}}. {} if none."""
    try:
        with open(_path(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write(refs: dict, path=None) -> None:
    target = _path(path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(refs, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, target)


def _record(event, reason, label):
    """Branch creation and deletion belong in the authority ledger. Never raises.

    UNDER THEIR OWN EVENT NAMES. The first version reused GENOME_APPLY/GENOME_REVERT because
    those existed and the words fit loosely -- and put four label creations into the same
    bucket as seven real activations, in the one place a reader goes to ask whether the
    running harness changed. Creating a branch is not activating one: it changes what an
    operator could later choose to run, which is worth recording and is a different fact.
    """
    try:
        from relay.selfimprove import authority_ledger as AL
        AL.append(event, reason="branch %s: %s" % (label, reason),
                  actor_claimed="selfimprove.branches", authorization=AL.SELF_INITIATED)
    except Exception as exc:
        print("[branches] could not record %s for %s: %s: %s"
              % (event, label, type(exc).__name__, exc), flush=True)


def validate_label(label: str) -> str:
    """The label, or raise. Checked before anything is written."""
    text = str(label or "").strip()
    if not text:
        raise BranchError("a branch needs a name; an unnamed ref is a genome id with extra steps")
    if text in RESERVED:
        raise BranchError(
            "%r is reserved. The base harness is constructed by manifest.base_manifest(), not "
            "stored, so it cannot be lost and must not be shadowed by a ref that could drift "
            "from the code -- reset_to_base() has to keep meaning 'what shipped'" % text)
    if len(text) > 40:
        raise BranchError("branch names are at most 40 characters; %d given" % len(text))
    bad = sorted(set(text) - _ALLOWED)
    if bad:
        raise BranchError("branch names use letters, digits, - and _ only; found %r"
                          % "".join(bad))
    return text


def create(label: str, genome_id: str, *, archive, note: str = "", path=None,
           now=None) -> dict:
    """Point `label` at an archive row. Refuses rather than creating a ref that cannot resolve.

    The archive is checked HERE, not at resolve time. A ref to a genome that does not exist is
    a promise the system cannot keep, and the moment to find that out is while the operator is
    looking at the screen rather than twenty minutes into a comparison.
    """
    label = validate_label(label)
    refs = read(path)
    if label in refs:
        raise BranchError("branch %r already exists (points at %s); delete it first, which "
                          "costs only the name" % (label, refs[label].get("genome_id")))
    if len(refs) >= MAX_BRANCHES:
        raise BranchError(
            "%d branches is the limit. Deleting one loses only a name -- the archive row stays "
            "and the branch can be re-made from the same genome id -- so the cost of staying "
            "under the ceiling is nothing, and the cost of going over it is a drawer of lines "
            "nobody can account for" % MAX_BRANCHES)

    entry = archive.get(genome_id)
    if entry is None:
        raise BranchError("no archive row with id %r; a ref that cannot resolve is a promise "
                          "the system cannot keep" % genome_id)
    # THE VERDICT, NOT `_selectable`. That also enforces MAX_UNVALIDATED_DEPTH, which is a
    # rule about how far the LOOP may wander from a proven ancestor before it has to validate
    # -- a discipline for automatic parent selection. An operator naming a branch by hand is
    # not wandering, and refusing them on depth would be applying a rule outside the problem
    # it was written for. The verdict is different: a genome the loop refused must not become
    # runnable again by being given a name.
    if not archive.__class__._verdict_ok(entry):
        raise BranchError(
            "archive row %s has verdict %r, which is not selectable. A genome the loop refused "
            "must not become runnable again by being given a name -- that is the door a "
            "rejected candidate would walk back in through"
            % (genome_id, entry.get("gate_verdict")))

    refs[label] = {"genome_id": genome_id,
                   "created_at": int(time.time() if now is None else now()),
                   "last_run_at": None,
                   "note": str(note or "")}
    _write(refs, path)
    _record("branch_create", "created -> %s" % genome_id, label)
    return dict(refs[label])


def delete(label: str, *, path=None) -> bool:
    """Forget the name. The archive row is untouched, so this is cheap and reversible."""
    refs = read(path)
    if label not in refs:
        return False
    gid = refs[label].get("genome_id")
    del refs[label]
    _write(refs, path)
    _record("branch_delete", "deleted (pointed at %s; the row is untouched)" % gid, label)
    return True


def touch(label: str, *, path=None, now=None) -> None:
    """Stamp a branch as having been run. Staleness is what makes a forgotten branch visible."""
    refs = read(path)
    if label in refs:
        refs[label]["last_run_at"] = int(time.time() if now is None else now())
        _write(refs, path)


def resolve(label: str, *, archive, path=None) -> dict:
    """{label, genome_id, genome, manifest, harness_id}. Raises if the ref cannot resolve."""
    refs = read(path)
    if label not in refs:
        raise BranchError("no branch named %r; have %s"
                          % (label, ", ".join(sorted(refs)) or "none"))
    gid = refs[label].get("genome_id")
    entry = archive.get(gid)
    if entry is None:
        raise BranchError("branch %r points at %s, which is not in the archive" % (label, gid))
    genome = entry.get("genome") or {}
    manifest, hid = M.materialize(genome)
    return {"label": label, "genome_id": gid, "genome": genome,
            "manifest": manifest, "harness_id": hid}


def materialize_to_file(label: str, *, archive, path=None, dir_=None) -> tuple:
    """Write the branch's manifest to a temp file for a child process. Returns (path, info).

    A FILE FOR A SUBPROCESS, NEVER THE ACTIVE MANIFEST. `runtime_config.write_active` changes
    what THIS machine runs and is not called from anywhere in this module. A comparison points
    children at these files through MCP_HARNESS_MANIFEST, which is why running one can never
    cost the operator their way back to base.
    """
    info = resolve(label, archive=archive, path=path)
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8",
                                     dir=dir_, newline="\n")
    json.dump(info["manifest"], fh, ensure_ascii=False, indent=2, sort_keys=True)
    fh.close()
    return fh.name, info


def describe_active(*, archive, path=None, active=None) -> dict:
    """What is running right now, resolved back to a name if it has one.

    THE ONLY DETECTOR FOR THE FAILURE BRANCHES INTRODUCE.

    "Running on a branch nobody remembers" has no other symptom: the fleet works, the runs
    complete, and the numbers look like numbers. Reporting `{"kind": "unnamed"}` is what makes
    it visible, so this returns that state explicitly rather than falling back to "base".
    """
    from relay.selfimprove import runtime_config as RC
    manifest = active if active is not None else RC.active_manifest(refresh=True)
    hid = M.harness_id(manifest)
    if hid == M.harness_id(M.base_manifest()):
        return {"kind": "base", "label": None, "harness_id": hid}
    for label in sorted(read(path)):
        try:
            if resolve(label, archive=archive, path=path)["harness_id"] == hid:
                return {"kind": "branch", "label": label, "harness_id": hid}
        except BranchError:
            continue
    return {"kind": "unnamed", "label": None, "harness_id": hid}
