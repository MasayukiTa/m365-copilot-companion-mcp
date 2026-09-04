"""One file walk for every search tool, with the directories nobody is searching pruned out.

WHY THIS EXISTS. `Path.rglob("*")` descends into everything, and on this repository that is
76,129 files -- of which 61,757 live in `.venv` and 2,651 in `.git`. Every grep and every
find_files paid for all of them, several threads at a time, and the transient Path objects of
one such walk are large enough that the allocator grows to hold the concurrent peak and keeps
it. Measured 2026-08-26 with both of the earlier memory fixes already in place: 343 MB to
1297 MB in under four minutes, +260 MB/min -- indistinguishable from the rate before either
fix, because bounding what a search HOLDS does nothing about what it TOUCHES.

The pruned names are the ones a developer tool is expected to skip. Not, precisely, "what
ripgrep skips by default" -- rg skips most of these by honouring .gitignore rather than by
carrying a list -- and saying so would have been a claim nobody checked. This is a real change
in what can be found, so it is announced in the result rather than assumed, it is announced
only when something was ACTUALLY skipped, and one environment variable turns it off.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Directories no search descends into. Not a performance list -- a correctness one: a hit
#: inside .venv is a hit in somebody else's source, and a hit inside .git is a hit in a
#: compressed object nobody can act on.
PRUNED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", "site-packages",
})
#: `env` was in this list and is now not. It is a perfectly ordinary directory name for
#: deployment configuration, and a project that keeps one would have had it vanish from every
#: search with the note blaming "vendored directories" -- the failure mode this list exists to
#: prevent, caused by the list itself. The bare-venv case it was meant to catch is covered by
#: `venv`.


def pruning_enabled() -> bool:
    """False when the caller has asked for everything, including the vendored world."""
    return os.environ.get("MCP_SEARCH_INCLUDE_ALL", "").strip().lower() not in (
        "1", "true", "yes", "on")


def iter_files(base: Path, skipped=None):
    """Yield every file under `base`, skipping PRUNED_DIRS. A generator, never a list.

    os.walk rather than rglob because rglob cannot be told not to descend -- the pruning has
    to happen while walking, which is the entire saving.

    `skipped`, when given a set, collects the names actually pruned. Callers disclose on that
    rather than on the setting: a note saying "vendored directories were not searched" is a
    lie about a tree that contains none, and about a search of a single file.
    """
    prune = pruning_enabled()
    for root, dirs, files in os.walk(str(base)):
        if prune:
            keep = []
            for d in dirs:
                if d in PRUNED_DIRS:
                    if skipped is not None:
                        skipped.add(d)
                else:
                    keep.append(d)
            dirs[:] = keep
        # SORTED, SO THE SAME QUESTION GETS THE SAME ANSWER TWICE.
        #
        # os.walk yields whatever the filesystem hands it: NTFS enumerates a directory in name
        # order, ext4 in hash order. Every caller that stops early or breaks a tie by
        # first-seen therefore returned a DIFFERENT result on the two platforms for identical
        # input -- find_files' equal-mtime tie-break, and grep's "which files did I reach
        # before max_matches", which decides whether the note about skipped files is ever
        # recorded at all. Both were caught the same way: green on Windows, red on a Linux
        # runner, with the tests asserting sorted order because that is what the author saw.
        #
        # A tool an agent uses to decide what to do next must not answer differently on two
        # machines for reasons that have nothing to do with the question. Sorting names within
        # each directory costs one sort per directory against a stat() per candidate.
        dirs.sort()
        for name in sorted(files):
            yield Path(root) / name


def is_pruned(path: Path, base: Path) -> bool:
    """Whether `path` sits inside a pruned directory below `base`.

    For the callers that cannot prune while walking -- a non-recursive glob hands back its
    own results -- so that the same question asked three ways does not give two answers.
    """
    if not pruning_enabled():
        return False
    try:
        rel = path.relative_to(base)
    except ValueError:
        return False
    return any(part in PRUNED_DIRS for part in rel.parts[:-1])


def pruned_note(skipped=None) -> str:
    """What a reader has to know to trust a "(no matches)". "" when nothing was pruned.

    Takes what was ACTUALLY skipped. It used to answer from the setting alone, so every
    result carried the notice -- including searches of a single file, and of trees with no
    vendored directory in them. A disclosure that is always printed stops being read.
    """
    if not pruning_enabled() or not skipped:
        return ""
    names = ", ".join(sorted(skipped)[:5])
    return ("%s were not searched; set MCP_SEARCH_INCLUDE_ALL=1 to include them" % names)
