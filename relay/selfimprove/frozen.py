"""Frozen-constitution checksum guard for the autonomous self-improvement loop.

This is the safety core of L2 (cf. bench/SELF_GROWTH_L4_DESIGN.md sec 0). A self-improving agent
may edit the SCAFFOLD (its hands) but must NEVER edit the JUDGE -- the things that define success or
enforce honesty. This module pins a frozen "constitution" set by sha256 so the loop can ABORT if any
frozen file changed between iterations: a change to the grader, the enforcing guards, or this
manifest itself means the agent may be reward-hacking, and the run must stop.

The frozen set (FROZEN_MANIFEST): the swebench grader scripts, the ENFORCING guards
(relay/selfimprove/guards.py), the two constitution docs, and this manifest module itself.

NOT frozen -- the burned registry (relay/selfimprove/burned.jsonl). It is append-only *state*, not a
judge, and carries a different invariant: it must never be rewritten or shrunk, only extended. That
invariant is checked separately by `burned_append_only(old_lines, new_lines)`, which is True iff the
new content is a prefix-extension superset of the old (every old line still present, in order, at the
front, with zero or more lines appended).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
from typing import Iterable

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Repo-relative paths that form the frozen judge / constitution. Missing files at runtime are not an
# error here (they map to "MISSING"); `frozen_intact` treats a baseline-present-now-MISSING file as a
# violation, which is the safety-relevant direction.
FROZEN_MANIFEST = [
    "bench/swe_grade_swebench.py",        # the swebench grader
    "bench/evalhost_batch_grade.py",         # the batch grader
    "relay/selfimprove/guards.py",        # the ENFORCING guards (significance_gate, BurnedRegistry, ...)
    "docs/SECURITY.md",                   # constitution doc: the stated security model
    "relay/selfimprove/frozen.py",        # this manifest itself

    # THE SECURITY CONSTITUTION. Absent until an independent review pointed out that the
    # controller's "the judge was intact" check could not see an edit to the permission
    # gate or to external-content distrust -- the two boundaries the evolution loop is most
    # likely to find inconvenient. The manifest covered the thing that MEASURES and left out
    # the thing that PERMITS.
    "tools/security.py",                  # the unlock / per-IP permission gate
    "tools/_untrusted.py",                # external content is data, never instructions
    "tools/folder_policy.py",             # which folders may be touched at all

    # The evolution machinery's own boundary. manifest.py holds EVOLVABLE_COMPONENTS and
    # FORBIDDEN_COMPONENTS: a loop that can quietly widen its own allowlist has no allowlist,
    # and decision.py is where a gate could be made optional again.
    "relay/selfimprove/manifest.py",
    "relay/selfimprove/decision.py",

    # THE JUDGE THAT ACTUALLY RUNS NOW. The manifest froze the swebench graders and stopped
    # there, from a time when those were the only graders. CompanionBench decides candidate
    # acceptance today, and every one of these files is a place where an episode could be
    # made easier, a security check softened, or a pool quietly re-pointed -- with the
    # frozen check reporting INTACT throughout.
    "bench/companionbench/episode.py",
    "bench/companionbench/pools.py",
    "bench/companionbench/runner.py",
    "bench/companionbench/episodes/core.py",
    "bench/companionbench/episodes/office.py",
    "bench/companionbench/episodes/runtime.py",
    "bench/companionbench/episodes/sealed.py",
]

DEFAULT_BASELINE = os.path.join(os.path.dirname(__file__), "frozen_baseline.json")

#: Where the baseline's own digest is kept: the operator's home, resolved at runtime, so no
#: absolute path is written into the source. Outside every checkout is the entire point --
#: an anchor stored beside the thing it anchors is decoration.
ANCHOR_ENV = "SELFIMPROVE_FROZEN_ANCHOR_FILE"
DEFAULT_ANCHOR = os.path.join(os.path.expanduser("~"), ".selfimprove_frozen_anchor")

MISSING = "MISSING"


def _anchor_path() -> str:
    return os.environ.get(ANCHOR_ENV, "").strip() or DEFAULT_ANCHOR


def anchor_state() -> str:
    """"present" | "absent" | "redirected". Which one matters to the caller.

    The env override exists so tests and a second checkout can point somewhere else. It also
    means a candidate running in-process can set it to a path that does not exist and turn
    the anchor check off, because a missing anchor simply skipped it. Absent is unavoidable
    on a fresh clone and cannot be fatal; REDIRECTED is different -- somebody chose it -- and
    is treated as a violation when the default anchor exists and the redirect does not.
    """
    override = os.environ.get(ANCHOR_ENV, "").strip()
    if not override:
        return "present" if os.path.isfile(DEFAULT_ANCHOR) else "absent"
    if os.path.isfile(override):
        return "present"
    return "redirected" if os.path.isfile(DEFAULT_ANCHOR) else "absent"


def _read_anchor() -> str:
    try:
        with open(_anchor_path(), encoding="utf-8") as fh:
            return (fh.read() or "").strip()
    except OSError:
        return ""


def _sha256(path: str) -> str:
    """sha256 hex of a file's content with line endings normalised to LF.

    WHY NORMALISED, WHICH IS NOT AN OBVIOUS THING FOR AN INTEGRITY CHECK TO DO.

    This hashed raw bytes, and on a Windows checkout with `core.autocrlf` git materialises
    CRLF. The result: the frozen set reported the judge as tampered with, permanently, on a
    tree where nothing had been touched. It was found by a baseline mismatch on
    manifest.py that turned out to be 233 CRLF pairs and not one changed character.

    Two costs, and the second is the serious one. Every scheduled run was blocked, since an
    intact frozen set is a precondition. And an integrity check that cries wolf on a clean
    tree is one that gets bypassed -- the failure mode is not that it stays noisy, it is that
    someone adds a flag to skip it and the check quietly stops existing.

    What normalising gives up: an attacker who can change line endings and nothing else. In
    Python source that changes no behaviour, so the check loses nothing it was protecting.
    What it buys: the same verdict for the same source on Linux CI and a Windows workstation,
    which the raw-byte version could not give even in principle.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def compute_checksums(repo_root: str = REPO, manifest: Iterable[str] = FROZEN_MANIFEST) -> dict:
    """Return {repo-relative path -> sha256 hex} for each manifest file; missing files map to MISSING."""
    out: dict[str, str] = {}
    for rel in manifest:
        full = os.path.join(repo_root, rel)
        out[rel] = _sha256(full) if os.path.isfile(full) else MISSING
    return out


class BaselineRefused(Exception):
    """Raised when re-blessing the constitution would destroy the thing it is for."""


#: A STANDING DELEGATION DOES NOT COVER THE MACHINERY THAT ENFORCES IT.
#:
#: The operator delegated re-signing on 2026-08-20 and kept Skills approval as a per-act human
#: decision. But the rule that says Skills stay human lives in the constitution, and the
#: constitution's re-signing is what was just delegated -- so that line could be erased by one
#: delegated act, and so could this exclusion, and so could the revoke path that makes
#: rollback possible. A boundary whose definition sits inside the delegated region is not a
#: boundary.
#:
#: Touching any of these therefore falls OUT of the standing delegation and back to a
#: specified decision. Everything else the operator delegated stays delegated.
DELEGATION_EXCLUDED = (
    "relay/selfimprove/frozen.py",            # this guard, its revoke path, this list
    "relay/selfimprove/manifest.py",          # what may be evolved, and what never may
    "tools/security.py",                      # the unlock boundary
    "tools/folder_policy.py",
    "tools/_untrusted.py",
    "docs/SECURITY.md",
)

#: What a caller passes as `--authorization` when acting under the standing delegation rather
#: than a decision made about this particular act. Named so the ledger shows which it was.
STANDING_DELEGATION = "standing-delegation"


#: Held while the baseline is written or withdrawn. A self-improvement run verifies and may
#: re-sign; a human may revoke from the dashboard at the same moment. Both write the baseline
#: AND the anchor, and a half-applied pair is the one state nothing here can diagnose --
#: `frozen_intact` would report a rewritten baseline and nobody would know which write lost.
#: "Nothing is running when I press it" is an intention, not a guarantee.
_BASELINE_LOCK = os.path.join(os.path.dirname(DEFAULT_BASELINE), ".baseline.lock")


@contextlib.contextmanager
def _baseline_lock(timeout_s: float = 20.0):
    """Cross-process exclusive lock. O_CREAT|O_EXCL because this must behave the same on
    Windows, and the critical section is three file writes long. Mirrors the same pattern in
    ledger.py rather than introducing a second locking idiom."""
    import time as _time
    deadline = _time.time() + timeout_s
    fd = None
    while fd is None:
        try:
            fd = os.open(_BASELINE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _time.time() > deadline:
                raise BaselineRefused(
                    "another process is writing the baseline (%s). Re-signing and revoking "
                    "both rewrite the baseline and its anchor, and interleaving them leaves a "
                    "pair nothing can diagnose afterwards." % _BASELINE_LOCK)
            _time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.unlink(_BASELINE_LOCK)
        except OSError:
            pass


def snapshot_baseline(repo_root: str = REPO, baseline_path: str = DEFAULT_BASELINE,
                      force: bool = False) -> dict:
    """Compute the frozen checksums and write them as the baseline json. Returns the baseline dict.

    TWO REFUSALS, both of which used to be silent successes.

    A baseline that already exists is NOT overwritten. Snapshotting is how a tamper is
    laundered: edit the grader, re-snapshot, and `frozen_intact` says INTACT forever after.
    The function is exported from the package and reachable by anything running in-process,
    which is precisely the loop this guard exists to constrain.

    WHAT `force` REQUIRES, restated 2026-08-20 because the previous wording had stopped being
    true. It said re-signing was "never something code does for itself mid-run". An external
    review established that nothing here enforces that: the agent holds a shell, can run this
    CLI, and the anchor updates with it. The sentence described a discipline and read like a
    boundary, which is the worse of the two failure modes -- the operator of this system had
    in fact assumed the re-signing was already automatic, which is what a rule that cannot be
    enforced eventually teaches everyone.

    So the requirement is stated as what is actually defended: a re-signing must be a
    SPECIFIED decision by the operator -- specified meaning they knew this particular act was
    included, not that they approved a batch that happened to contain it -- and both the
    approval and the act are recorded in the authority ledger, with the operator's
    instruction quoted verbatim. `--reason` is mandatory for that reason.

    What is NOT claimed: that this stops anything. The ledger records; it does not authorise
    and cannot prevent. See authority_ledger.py's opening note.

    A manifest entry that resolves to no file is refused outright. MISSING was accepted as a
    legitimate baseline value, which made two dead paths (constitution docs untracked months
    earlier) pin nothing at all while the verifier cheerfully reported "11 files match". It
    also made deletion a passing state: remove the grader, snapshot, MISSING == MISSING.
    """
    with _baseline_lock():
        return _snapshot_locked(repo_root, baseline_path, force)


def _snapshot_locked(repo_root, baseline_path, force):
    sums = compute_checksums(repo_root)
    absent = sorted(rel for rel, h in sums.items() if h == MISSING)
    if absent:
        raise BaselineRefused(
            "cannot baseline a frozen set with missing files -- these pin nothing: %s"
            % ", ".join(absent))
    if os.path.isfile(baseline_path) and not force:
        raise BaselineRefused(
            "a baseline already exists at %s; re-snapshotting would accept any change made "
            "since it was written. Pass force=True (CLI: --snapshot --force --reason ...) "
            "only for a change the operator specified." % baseline_path)
    # NO ABSOLUTE PATH IN A COMMITTED FILE. `repo_root` was written verbatim, so every
    # snapshot recorded the checkout's full path -- and this file is tracked and pushed, in
    # this case to a public repository, which put a username and a directory name into the
    # history on every run. The path was never used for anything either: `frozen_intact`
    # takes its own repo_root argument and the recorded one was only ever read back for
    # display. What the baseline is FOR is the checksums.
    data = {"repo_root": "<repo>", "checksums": sums}
    os.makedirs(os.path.dirname(baseline_path) or ".", exist_ok=True)
    # A CACHE, NOT THE RECORD. The authoritative copy of the previous baseline goes into the
    # ledger, which is append-only and chained; this file is a plain mutable one beside the
    # thing it is meant to protect. It exists because restoring from it is fast, and `revoke`
    # falls back to the ledger whenever it is missing or does not match.
    if os.path.isfile(baseline_path):
        try:
            shutil.copyfile(baseline_path, baseline_path + ".prev")
        except OSError:
            pass
    with open(baseline_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    # Anchor the baseline outside the tree, so a later rewrite of it is detectable.
    if baseline_path == DEFAULT_BASELINE:
        try:
            with open(baseline_path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            with open(_anchor_path(), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(digest + "\n")
        except OSError:
            pass                            # an unwritable home is not a reason to fail here
    return data


def _baseline_digest(baseline_path: str) -> str:
    """The anchor's view of the baseline file, with line endings normalised.

    RAW BYTES WERE WRONG HERE FOR THE SAME REASON THEY WERE WRONG FOR THE CHECKSUMS. git
    materialises this file with CRLF on a Windows checkout, so the anchor written at snapshot
    time stopped matching the moment the file was checked out, stashed, or switched across a
    branch -- and `frozen_intact` then reported BASELINE_REWRITTEN on a baseline nobody had
    touched. Measured: 22 CRLF pairs, LF-normalised digest matches the anchor exactly.

    That false positive is not cosmetic. An intact frozen set is a precondition of the
    scheduled run, so the anchor alone was enough to keep `nightly()` permanently blocked --
    a second, independent reason on top of the AttributeError in `nightly` itself.
    """
    with open(baseline_path, "rb") as fh:
        return hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()


def load_baseline(baseline_path: str = DEFAULT_BASELINE) -> dict | None:
    """Load the baseline json, or None if it does not exist / is unreadable."""
    if not os.path.isfile(baseline_path):
        return None
    try:
        with open(baseline_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _note_mismatch(changed, baseline_path) -> None:
    """One ledger record per distinct mismatch. Never fatal, never noisy."""
    if not changed:
        return
    try:
        from relay.selfimprove import authority_ledger as _led
        _led.record_mismatch_once(
            changed,
            reason="the frozen set no longer matches %s" % os.path.basename(baseline_path),
            actor_claimed="relay.selfimprove.frozen.frozen_intact")
    except Exception:
        pass


def frozen_intact(repo_root: str = REPO,
                  baseline_path: str = DEFAULT_BASELINE,
                  manifest: Iterable[str] | None = None) -> tuple[bool, list[str]]:
    """Verify the frozen set against the baseline.

    Returns (ok, changed_paths). ok is True iff the current checksum of every manifest path equals
    the baseline's. changed_paths lists every path whose checksum differs from the baseline or has
    become MISSING. If no baseline exists, returns (False, ["NO_BASELINE"]) -- the loop must snapshot
    before it can trust anything.
    """
    base = load_baseline(baseline_path)
    if base is None:
        return False, ["NO_BASELINE"]
    baseline_sums = base.get("checksums", {})
    # The manifest is an ARGUMENT defaulting to code -- never data read from the baseline.
    # Tests pin their own fake constitution through it; nothing else should pass it.
    manifest = list(FROZEN_MANIFEST if manifest is None else manifest)
    changed: list[str] = []

    # THE BASELINE DOES NOT GET TO SAY WHAT IS PROTECTED. It used to: the loop iterated over
    # the baseline's own keys, so a baseline of `{"checksums": {}}` iterated zero times and
    # reported INTACT -- an independent review demonstrated exactly that in four lines. The
    # authoritative list is FROZEN_MANIFEST, which is code, and which is itself in the
    # frozen set. A manifest entry the baseline never pinned is a violation, not a silence.
    for rel in manifest:
        if rel not in baseline_sums:
            changed.append("UNPINNED:" + rel)
    # An entry the manifest no longer names is also worth reporting: it means the baseline
    # and the code disagree about the constitution, and which one is stale is not knowable
    # from here.
    for rel in baseline_sums:
        if rel not in manifest:
            changed.append("UNKNOWN_IN_BASELINE:" + rel)

    current = compute_checksums(repo_root, manifest)
    for rel in manifest:
        expected = baseline_sums.get(rel)
        if expected is None:
            continue                       # already reported as UNPINNED
        if current.get(rel, MISSING) != expected:
            changed.append(rel)

    # THE BASELINE FILE ITSELF. Everything above still trusts a file inside the working
    # tree, so anything that can edit the grader can edit its recorded checksum in the same
    # breath. `snapshot_baseline` refusing to overwrite does not help -- that is a function,
    # not a permission. The anchor is a digest of the baseline kept OUTSIDE every checkout;
    # when it exists, a rewritten baseline is caught. When it does not (a fresh clone) we
    # cannot detect the substitution, and say so rather than implying a guarantee.
    # Only THE baseline is anchored -- a temp baseline written by a test or a one-off audit
    # was never anchored and must not be reported as a rewrite of something else.
    anchor = _read_anchor() if baseline_path == DEFAULT_BASELINE else ""
    if baseline_path == DEFAULT_BASELINE and anchor_state() == "redirected":
        # Pointing the anchor at nothing while a real one exists is not a configuration, it
        # is the check being switched off from inside the process it constrains.
        changed.append("ANCHOR_REDIRECTED_AWAY_FROM_EXISTING")
    if anchor:
        try:
            if _baseline_digest(baseline_path) != anchor:
                changed.append("BASELINE_REWRITTEN")
        except OSError:
            changed.append("BASELINE_UNREADABLE")

    _note_mismatch(changed, baseline_path)
    return (not changed), changed


def burned_append_only(old_lines: Iterable[str], new_lines: Iterable[str]) -> bool:
    """True iff `new_lines` is a prefix-extension superset of `old_lines`.

    The burned registry must only ever grow: every old line is still present, in order, at the front
    of new, with zero or more lines appended. Any rewrite, reorder, or shrink (an old line removed or
    changed) returns False -- the loop would otherwise be able to re-use already-seen instances by
    quietly editing the ledger.
    """
    old = list(old_lines)
    new = list(new_lines)
    if len(new) < len(old):
        return False
    return new[:len(old)] == old


def last_rebless(ledger_path=None):
    """The most recent rebless record, or None. Reads the append-only ledger, not `.prev`."""
    try:
        from relay.selfimprove import authority_ledger as _led
        for row in reversed(_led.read(ledger_path)):
            if row.get("event") == _led.REBLESS:
                return row
    except Exception:
        pass
    return None


def revoke_baseline(baseline_path: str = DEFAULT_BASELINE, *, reason: str = "",
                    authorization: str = "", ledger_path=None) -> dict:
    """Withdraw the last re-signing: put the previous baseline and anchor back.

    THIS UNDOES AN APPROVAL, NOT A CHANGE. The files that were accepted are still whatever
    they are; only the record saying they were accepted goes away. So the expected state
    immediately afterwards is a BROKEN frozen check and a system that refuses to run -- that
    is the effect, not a side effect. Undoing the code itself is version control's job, and
    having this touch it would put two systems in charge of one thing.

    The previous baseline comes from the LEDGER, which is append-only and chained. The `.prev`
    file beside the baseline is only consulted as a fallback, because anything able to
    overwrite the baseline can also delete the copy sitting next to it.
    """
    with _baseline_lock():
        return _revoke_locked(baseline_path, reason, authorization, ledger_path)


def _revoke_locked(baseline_path, reason, authorization, ledger_path):
    record = last_rebless(ledger_path)
    previous = (record or {}).get("baseline_before")
    source = "ledger record seq=%s" % (record or {}).get("seq")
    if not previous:
        prev_file = baseline_path + ".prev"
        if not os.path.isfile(prev_file):
            raise BaselineRefused(
                "nothing to revoke to: the last rebless record carries no previous baseline "
                "and %s does not exist. A re-signing made before this was recorded cannot be "
                "undone here -- recover the baseline from version control instead." % prev_file)
        with open(prev_file, encoding="utf-8") as fh:
            previous = json.load(fh)
        source = prev_file

    with open(baseline_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(previous, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    if baseline_path == DEFAULT_BASELINE:
        try:
            with open(_anchor_path(), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(_baseline_digest(baseline_path) + "\n")
        except OSError:
            pass

    try:
        from relay.selfimprove import authority_ledger as _led
        _led.append(_led.REVOKE,
                    reason=reason or "withdrawing the previous re-signing",
                    actor_claimed="relay.selfimprove.frozen CLI",
                    authorization=authorization or _led.SELF_INITIATED,
                    command="frozen --revoke",
                    changed={"relay/selfimprove/frozen_baseline.json":
                             {"before": "(the re-signed baseline)", "after": source}},
                    path=ledger_path)
    except Exception:
        pass
    return previous


def _excluded_from_delegation(changed) -> list:
    return sorted(rel for rel in (changed or []) if rel in DELEGATION_EXCLUDED)


def _record_rebless(args, before, after) -> None:
    """Append the act to the authority ledger and print its tail. Never fatal.

    Printing the tail is the only external anchor available here: the chain catches an edit in
    the middle of the ledger and not a shortened one, and this line lands in a transcript
    outside the writing process's reach. It is weak and is not described as more.
    """
    try:
        from relay.selfimprove import authority_ledger as _led
        old = (before or {}).get("checksums", {})
        new = (after or {}).get("checksums", {})
        changed = {rel: {"before": old.get(rel), "after": new.get(rel)}
                   for rel in sorted(set(old) | set(new)) if old.get(rel) != new.get(rel)}
        _led.append(_led.REBLESS, reason=args.reason,
                    actor_claimed="relay.selfimprove.frozen CLI",
                    authorization=args.authorization or _led.SELF_INITIATED,
                    command="frozen --snapshot%s" % (" --force" if args.force else ""),
                    changed=changed,
                    # THE WHOLE PREVIOUS BASELINE, not just the differing rows. Rollback has to
                    # work from the ledger alone -- the .prev file beside the baseline is a
                    # convenience that anything able to overwrite the baseline can also delete.
                    baseline_before=before or None)
        print(_led.describe_tail())
    except Exception as exc:                      # never take the snapshot down with it
        print("(the act could not be recorded to the ledger: %s: %s)"
              % (type(exc).__name__, exc))



def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="relay.selfimprove.frozen",
                                 description="Frozen-constitution checksum guard.")
    ap.add_argument("--repo", default=REPO, help="repo root (default: %(default)s)")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE, help="baseline json path")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--snapshot", action="store_true", help="compute + write the baseline")
    g.add_argument("--verify", action="store_true", help="check current frozen set vs baseline")
    g.add_argument("--revoke", action="store_true",
                   help="withdraw the last re-signing: restore the previous baseline and "
                        "anchor. This undoes the APPROVAL, not the code -- the frozen check "
                        "will fail afterwards, which is the point")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing baseline (re-sign the constitution)")
    ap.add_argument("--reason", default="",
                    help="why this re-signing is correct. Required with --force: the git "
                         "history of the baseline already records WHAT changed, and what was "
                         "missing was why and on whose decision")
    ap.add_argument("--authorization", default="",
                    help="the operator's instruction, quoted verbatim, that specified this "
                         "act. A paraphrase is the actor's own reading of its mandate")
    args = ap.parse_args(argv)

    if args.revoke:
        try:
            revoke_baseline(args.baseline, reason=args.reason,
                            authorization=args.authorization)
        except BaselineRefused as exc:
            print("REFUSED: %s" % exc)
            return 2
        ok, changed = frozen_intact(args.repo, args.baseline)
        print("revoked: the previous baseline is back at %s" % args.baseline)
        print("frozen set intact: %s%s" % (ok, "" if ok else "  <- expected: the approval is "
                                                             "gone while the files are not"))
        if changed:
            print("still differing: %s" % ", ".join(changed))
        print()
        print("to finish rolling back, from the repo root:")
        print("  git log --oneline -- %s        # find the commit that made the change"
              % ", ".join(sorted(changed)) if changed else "  (nothing differs)")
        print("  git revert <commit>                     # undo the code as well")
        print("or, to accept the change after all:")
        print("  python -m relay.selfimprove.frozen --snapshot --force --reason ...")
        try:
            from relay.selfimprove import authority_ledger as _led
            print(_led.describe_tail())
        except Exception:
            pass
        return 0

    if args.snapshot:
        before = load_baseline(args.baseline) or {}
        if args.force and not str(args.reason).strip():
            print("REFUSED: --force needs --reason. Re-signing accepts every change made "
                  "since the baseline was written, and a record that cannot say why is the "
                  "gap this requirement exists to close.")
            return 2
        # THE HOLE IN THE STANDING DELEGATION. Re-signing is delegated; re-signing the
        # machinery that enforces the delegation is not, because a boundary whose definition
        # sits inside the delegated region can be erased by one delegated act.
        would_change = [rel for rel, h in compute_checksums(args.repo).items()
                        if (before.get("checksums") or {}).get(rel) != h]
        excluded = _excluded_from_delegation(would_change)
        if excluded and str(args.authorization).strip() in ("", STANDING_DELEGATION,
                                                            "self-initiated"):
            print("REFUSED: this re-signing touches %s, which the standing delegation does "
                  "not cover -- these files define what may be evolved, what the delegation "
                  "excludes, and how a re-signing is withdrawn. Pass --authorization with the "
                  "operator's decision about THIS change." % ", ".join(excluded))
            return 2
        try:
            data = snapshot_baseline(args.repo, args.baseline, force=args.force)
        except BaselineRefused as exc:
            print("REFUSED: %s" % exc)
            return 2
        _record_rebless(args, before, data)
        print("snapshot written: %s" % args.baseline)
        for rel, h in sorted(data["checksums"].items()):
            print("  %s  %s" % (h[:16] if h != MISSING else MISSING.ljust(16), rel))
        return 0

    ok, changed = frozen_intact(args.repo, args.baseline)
    if ok:
        print("frozen set INTACT (%d files match baseline)" % len(FROZEN_MANIFEST))
        return 0
    print("frozen set CHANGED -- ABORT")
    for rel in changed:
        print("  changed: %s" % rel)
    return 1


if __name__ == "__main__":
    sys.exit(_main())
