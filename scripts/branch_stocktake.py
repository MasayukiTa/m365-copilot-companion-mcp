#!/usr/bin/env python3
"""Reconcile how many branches are left against how many were declared.

The prompt behind this script is a plain operator question: "14 branches are still
there, but you declared 5 -- what gives?" (ledger line #209). The honest answer is
not a number pulled from memory; it is a reconciliation between two concrete lists:

  1. the branches that actually still exist (remote and/or local), and
  2. the pull requests those branches belong to, by state.

The discrepancy is almost always the same shape: the declared count is "branches
with work still in flight" (an OPEN PR, or no PR at all yet), while the leftover
count also includes branches whose PR already MERGED or was CLOSED and were simply
never pruned. This module classifies every leftover branch into exactly one bucket
so the gap is explained line by line instead of asserted.

It is deliberately pure: callers pass in the branch names and the PR records they
already have (e.g. from `git branch` / `git for-each-ref` and `gh pr list --json`),
and get back a structured reconciliation. No subprocess, no network -- so it is
testable on any runner and cannot depend on the machine it happens to run on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

# Branches that are never "leftover work": the trunk and the symbolic HEAD pointer.
_NEVER_LEFTOVER = {"main", "master", "HEAD"}

# PR states we treat as "the branch's work is no longer in flight" -- i.e. a branch
# left behind only because nobody pruned it.
_SETTLED_STATES = {"MERGED", "CLOSED"}


def normalize_branch(name: str) -> str:
    """Strip a remote prefix and whitespace so 'origin/foo' and 'foo' compare equal.

    'origin/HEAD -> origin/main' and a bare 'origin' entry collapse to markers the
    caller can drop. Only the first 'remote/' segment is removed; branch names that
    themselves contain slashes (feature/x) are preserved.
    """
    name = (name or "").strip()
    if not name:
        return ""
    # 'origin/HEAD -> origin/main' from `git branch -a`
    if " -> " in name:
        name = name.split(" -> ", 1)[0].strip()
    # Drop a leading 'remotes/' that `git branch -a` prepends.
    if name.startswith("remotes/"):
        name = name[len("remotes/"):]
    # Drop a single leading remote segment (origin/, upstream/, ...). We only strip
    # it when the first segment looks like a remote, which for this repo is origin.
    # To stay generic we strip the first segment only if the caller asked via the
    # higher-level function; here we leave multi-segment names intact and let
    # reconcile_branches handle remote prefixes explicitly.
    return name


def strip_remote_prefix(name: str, remotes: Sequence[str] = ("origin",)) -> str:
    """Remove a known remote prefix (origin/foo -> foo). Idempotent for local names."""
    name = normalize_branch(name)
    for r in remotes:
        pref = r + "/"
        if name.startswith(pref):
            return name[len(pref):]
    return name


@dataclass
class BranchStatus:
    """One leftover branch and why it is still around."""

    branch: str
    bucket: str  # 'open_pr' | 'no_pr' | 'settled_pr'
    pr_number: int | None = None
    pr_state: str | None = None


@dataclass
class Reconciliation:
    """The full answer to 'N branches remain but you declared D -- what gives?'"""

    leftover: list[BranchStatus] = field(default_factory=list)
    declared: int = 0

    @property
    def leftover_count(self) -> int:
        return len(self.leftover)

    @property
    def in_flight(self) -> list[BranchStatus]:
        """Branches whose work is still in flight: an OPEN PR, or no PR yet."""
        return [b for b in self.leftover if b.bucket in ("open_pr", "no_pr")]

    @property
    def settled(self) -> list[BranchStatus]:
        """Branches kept only because a MERGED/CLOSED PR's branch was never pruned."""
        return [b for b in self.leftover if b.bucket == "settled_pr"]

    @property
    def in_flight_count(self) -> int:
        return len(self.in_flight)

    @property
    def settled_count(self) -> int:
        return len(self.settled)

    @property
    def reconciles(self) -> bool:
        """True when the declared number equals the branches still in flight.

        That is the common, honest explanation of the gap: the operator counted
        in-flight work; the leftover total also carried un-pruned settled branches.
        """
        return self.declared == self.in_flight_count

    @property
    def unexplained_gap(self) -> int:
        """Branches beyond the declared number that in-flight work does NOT explain.

        Zero means the gap is fully accounted for by un-pruned settled branches.
        A positive number means more branches are in flight than were declared --
        a real discrepancy, not just stale branches.
        """
        return max(0, self.in_flight_count - self.declared)


def _index_prs_by_branch(
    prs: Iterable[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    """Map headRefName -> the most relevant PR record.

    A branch can appear on several PRs over time (reopened, reused). We prefer an
    OPEN record over a settled one so a reused branch is reported as in-flight.
    """
    chosen: dict[str, Mapping[str, object]] = {}
    for pr in prs:
        ref = str(pr.get("headRefName") or "").strip()
        if not ref:
            continue
        state = str(pr.get("state") or "").upper()
        prev = chosen.get(ref)
        if prev is None:
            chosen[ref] = pr
            continue
        prev_state = str(prev.get("state") or "").upper()
        # Prefer OPEN; otherwise keep the first seen.
        if state == "OPEN" and prev_state != "OPEN":
            chosen[ref] = pr
    return chosen


def reconcile_branches(
    branches: Sequence[str],
    prs: Iterable[Mapping[str, object]],
    declared: int,
    remotes: Sequence[str] = ("origin",),
) -> Reconciliation:
    """Classify every leftover branch and reconcile against the declared count.

    Args:
        branches: branch names as emitted by git (remote prefixes and the HEAD
            pointer line are tolerated and normalized away). Duplicates that
            collapse to the same local name after stripping a remote prefix are
            counted once.
        prs: PR records, each a mapping with at least 'headRefName' and 'state'
            and optionally 'number' (shape of `gh pr list --json ...`).
        declared: the number the operator declared ("5" in line #209).
        remotes: remote names whose prefix should be stripped.

    Returns:
        A Reconciliation with one BranchStatus per distinct leftover branch.
    """
    by_branch = _index_prs_by_branch(prs)

    seen: set[str] = set()
    result = Reconciliation(declared=int(declared))
    for raw in branches:
        name = strip_remote_prefix(raw, remotes=remotes)
        if not name or name in _NEVER_LEFTOVER:
            continue
        # A bare remote token ('origin') or an empty normalization is not a branch.
        if name in remotes:
            continue
        if name in seen:
            continue
        seen.add(name)

        pr = by_branch.get(name)
        if pr is None:
            result.leftover.append(BranchStatus(branch=name, bucket="no_pr"))
            continue
        state = str(pr.get("state") or "").upper()
        number = pr.get("number")
        number = int(number) if isinstance(number, (int, float)) else None
        if state == "OPEN":
            bucket = "open_pr"
        elif state in _SETTLED_STATES:
            bucket = "settled_pr"
        else:
            # Unknown/empty state: treat conservatively as still in flight so it is
            # never silently dropped from the explanation.
            bucket = "no_pr"
        result.leftover.append(
            BranchStatus(branch=name, bucket=bucket, pr_number=number, pr_state=state or None)
        )
    return result


def format_report(rec: Reconciliation) -> str:
    """Render a short, deterministic text report of the reconciliation."""
    lines: list[str] = []
    lines.append(
        f"leftover branches: {rec.leftover_count}  |  declared: {rec.declared}"
    )
    lines.append(
        f"  in flight (open PR or no PR): {rec.in_flight_count}"
    )
    lines.append(
        f"  settled but un-pruned (merged/closed PR): {rec.settled_count}"
    )
    verdict = "reconciles" if rec.reconciles else "DOES NOT reconcile"
    lines.append(
        f"  verdict: {verdict} (declared == in-flight? {rec.reconciles}; "
        f"unexplained extra in-flight: {rec.unexplained_gap})"
    )
    if rec.settled:
        lines.append("  -- settled branches safe to prune:")
        for b in sorted(rec.settled, key=lambda x: x.branch):
            num = f"#{b.pr_number}" if b.pr_number else "(no number)"
            lines.append(f"       {b.branch}  [{b.pr_state} {num}]")
    if rec.in_flight:
        lines.append("  -- branches still in flight:")
        for b in sorted(rec.in_flight, key=lambda x: x.branch):
            if b.bucket == "open_pr":
                num = f"#{b.pr_number}" if b.pr_number else "(no number)"
                lines.append(f"       {b.branch}  [OPEN {num}]")
            else:
                lines.append(f"       {b.branch}  [no PR]")
    return "\n".join(lines)


def _load_json(path: str):
    import json

    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--branches",
        required=True,
        help="Path to a text file with one branch name per line (git branch -a output is fine).",
    )
    parser.add_argument(
        "--prs",
        required=True,
        help="Path to a JSON array of PR records (gh pr list --json headRefName,state,number).",
    )
    parser.add_argument(
        "--declared",
        type=int,
        required=True,
        help="The number of branches that were declared as remaining work.",
    )
    parser.add_argument(
        "--remote",
        action="append",
        default=None,
        help="Remote prefix to strip (repeatable). Defaults to 'origin'.",
    )
    args = parser.parse_args(argv)

    with open(args.branches, "r", encoding="utf-8") as fh:
        branches = [ln.rstrip("\n") for ln in fh if ln.strip()]
    prs = _load_json(args.prs)
    remotes = tuple(args.remote) if args.remote else ("origin",)
    rec = reconcile_branches(branches, prs, args.declared, remotes=remotes)
    print(format_report(rec))
    # Exit non-zero only when the gap is NOT explained by settled branches, i.e.
    # there is genuinely more in-flight work than declared.
    return 1 if rec.unexplained_gap > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
