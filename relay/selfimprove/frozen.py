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
import hashlib
import json
import os
import sys
from typing import Iterable

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Repo-relative paths that form the frozen judge / constitution. Missing files at runtime are not an
# error here (they map to "MISSING"); `frozen_intact` treats a baseline-present-now-MISSING file as a
# violation, which is the safety-relevant direction.
FROZEN_MANIFEST = [
    "bench/swe_grade_swebench.py",        # the swebench grader
    "bench/the eval host_batch_grade.py",         # the batch grader
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
    """sha256 hex of a file read as binary."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
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


def snapshot_baseline(repo_root: str = REPO, baseline_path: str = DEFAULT_BASELINE,
                      force: bool = False) -> dict:
    """Compute the frozen checksums and write them as the baseline json. Returns the baseline dict.

    TWO REFUSALS, both of which used to be silent successes.

    A baseline that already exists is NOT overwritten. Snapshotting is how a tamper is
    laundered: edit the grader, re-snapshot, and `frozen_intact` says INTACT forever after.
    The function is exported from the package and reachable by anything running in-process,
    which is precisely the loop this guard exists to constrain. Re-blessing is a deliberate
    human act -- `--snapshot --force` from the CLI -- and never something code does for
    itself mid-run.

    A manifest entry that resolves to no file is refused outright. MISSING was accepted as a
    legitimate baseline value, which made two dead paths (constitution docs untracked months
    earlier) pin nothing at all while the verifier cheerfully reported "11 files match". It
    also made deletion a passing state: remove the grader, snapshot, MISSING == MISSING.
    """
    sums = compute_checksums(repo_root)
    absent = sorted(rel for rel, h in sums.items() if h == MISSING)
    if absent:
        raise BaselineRefused(
            "cannot baseline a frozen set with missing files -- these pin nothing: %s"
            % ", ".join(absent))
    if os.path.isfile(baseline_path) and not force:
        raise BaselineRefused(
            "a baseline already exists at %s; re-snapshotting would bless any change made "
            "since it was written. Pass force=True (CLI: --snapshot --force) only as a "
            "deliberate human act." % baseline_path)
    data = {"repo_root": repo_root, "checksums": sums}
    os.makedirs(os.path.dirname(baseline_path) or ".", exist_ok=True)
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


def load_baseline(baseline_path: str = DEFAULT_BASELINE) -> dict | None:
    """Load the baseline json, or None if it does not exist / is unreadable."""
    if not os.path.isfile(baseline_path):
        return None
    try:
        with open(baseline_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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
            with open(baseline_path, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() != anchor:
                    changed.append("BASELINE_REWRITTEN")
        except OSError:
            changed.append("BASELINE_UNREADABLE")

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


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="relay.selfimprove.frozen",
                                 description="Frozen-constitution checksum guard.")
    ap.add_argument("--repo", default=REPO, help="repo root (default: %(default)s)")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE, help="baseline json path")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--snapshot", action="store_true", help="compute + write the baseline")
    g.add_argument("--verify", action="store_true", help="check current frozen set vs baseline")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing baseline (re-bless the constitution)")
    args = ap.parse_args(argv)

    if args.snapshot:
        try:
            data = snapshot_baseline(args.repo, args.baseline, force=args.force)
        except BaselineRefused as exc:
            print("REFUSED: %s" % exc)
            return 2
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
