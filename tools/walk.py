"""One file walk for every search tool, with the directories nobody is searching pruned out.

WHY THIS EXISTS. `Path.rglob("*")` descends into everything, and on this repository that is
76,129 files -- of which 61,757 live in `.venv` and 2,651 in `.git`. Every grep and every
find_files paid for all of them, several threads at a time, and the transient Path objects of
one such walk are large enough that the allocator grows to hold the concurrent peak and keeps
it. Measured 2026-08-26 with both of the earlier memory fixes already in place: 343 MB to
1297 MB in under four minutes, +260 MB/min -- indistinguishable from the rate before either
fix, because bounding what a search HOLDS does nothing about what it TOUCHES.

The pruned names are the ones a developer tool is expected to skip and ripgrep skips by
default. This is a real change in what can be found, so it is announced in the result rather
than assumed, and one environment variable turns it off.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Directories no search descends into. Not a performance list -- a correctness one: a hit
#: inside .venv is a hit in somebody else's source, and a hit inside .git is a hit in a
#: compressed object nobody can act on.
PRUNED_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", "site-packages",
})


def pruning_enabled() -> bool:
    """False when the caller has asked for everything, including the vendored world."""
    return os.environ.get("MCP_SEARCH_INCLUDE_ALL", "").strip().lower() not in (
        "1", "true", "yes", "on")


def iter_files(base: Path):
    """Yield every file under `base`, skipping PRUNED_DIRS. A generator, never a list.

    os.walk rather than rglob because rglob cannot be told not to descend -- the pruning has
    to happen while walking, which is the entire saving.
    """
    prune = pruning_enabled()
    for root, dirs, files in os.walk(str(base)):
        if prune:
            dirs[:] = [d for d in dirs if d not in PRUNED_DIRS]
        for name in files:
            yield Path(root) / name


def pruned_note() -> str:
    """What a reader has to know to trust a "(no matches)". "" when nothing was pruned."""
    if not pruning_enabled():
        return ""
    return ("vendored and VCS directories (%s) were not searched; set "
            "MCP_SEARCH_INCLUDE_ALL=1 to include them"
            % ", ".join(sorted(PRUNED_DIRS)[:4] + ["..."]))
