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

REPO = r"C:\Users\USER\companion-mcp"

# Repo-relative paths that form the frozen judge / constitution. Missing files at runtime are not an
# error here (they map to "MISSING"); `frozen_intact` treats a baseline-present-now-MISSING file as a
# violation, which is the safety-relevant direction.
FROZEN_MANIFEST = [
    "bench/swe_grade_swebench.py",        # the swebench grader
    "bench/the eval host_batch_grade.py",         # the batch grader
    "relay/selfimprove/guards.py",        # the ENFORCING guards (significance_gate, BurnedRegistry, ...)
    "bench/SELF_IMPROVEMENT_CONTROLLER.md",  # constitution doc
    "bench/SELF_GROWTH_L4_DESIGN.md",        # constitution doc
    "relay/selfimprove/frozen.py",        # this manifest itself
]

DEFAULT_BASELINE = os.path.join(os.path.dirname(__file__), "frozen_baseline.json")

MISSING = "MISSING"


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


def snapshot_baseline(repo_root: str = REPO, baseline_path: str = DEFAULT_BASELINE) -> dict:
    """Compute the frozen checksums and write them as the baseline json. Returns the baseline dict."""
    sums = compute_checksums(repo_root)
    data = {"repo_root": repo_root, "checksums": sums}
    os.makedirs(os.path.dirname(baseline_path) or ".", exist_ok=True)
    with open(baseline_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
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
                  baseline_path: str = DEFAULT_BASELINE) -> tuple[bool, list[str]]:
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
    current = compute_checksums(repo_root, baseline_sums.keys() or FROZEN_MANIFEST)
    changed: list[str] = []
    for rel, expected in baseline_sums.items():
        if current.get(rel, MISSING) != expected:
            changed.append(rel)
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
    args = ap.parse_args(argv)

    if args.snapshot:
        data = snapshot_baseline(args.repo, args.baseline)
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
