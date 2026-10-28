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
import time
from pathlib import Path

#: Search bounds are read from the settings file so a running server can be told to allow a
#: heavier search without a restart -- the same promise the fleet's other EACH_GATE knobs make.
#: The defaults are the values these searches used as hard-coded constants before #328, chosen
#: so a legitimate heavy search is not broken: the owner raises a bound by editing the setting
#: (or passing an explicit per-call argument), never by the bound silently being low. Declared
#: in tools/settings_keys.py (search_timeout_seconds / search_concurrency / search_max_depth)
#: with the reason: 2026-10-27..28 the gateway jammed three times, each time on an unbounded
#: find_files / glob, recoverable then only by restarting the server.


def _setting(key, cast, default):
    """Read one search bound from the settings file, cast it, fall back to `default`.

    Never raises: a search must not die because a setting is missing or malformed. Read fresh
    per search (a search is already the expensive thing, so one extra file read is noise), so
    an operator who raises a bound reaches the next search with no restart.
    """
    try:
        from . import settings_path as _sp
        raw = _sp.read_key(_sp.settings_file(), key)
        if raw is None or str(raw).strip() == "":
            return default
        return cast(str(raw).strip())
    except Exception:
        return default


#: Hard-coded fallbacks == the pre-#328 constants. A bound a caller passes explicitly wins
#: over the setting, which wins over these.
_DEFAULT_TIMEOUT_SECONDS = 20
_DEFAULT_CONCURRENCY = 4
#: None == no depth limit, which is what every search did before #328. The depth guard exists
#: so a caller CAN cap descent (issue #328 proposal 3 lists max depth as one of the bounds a
#: search may be required to carry), and so the owner can set a floor-wide cap; the default
#: stays None so no existing legitimate deep search changes behaviour until someone asks.
_DEFAULT_MAX_DEPTH = None


def search_timeout_seconds() -> int:
    """Wall-clock budget for one search walk, from settings or the pre-#328 default of 20."""
    v = _setting("search_timeout_seconds", lambda s: int(float(s)), _DEFAULT_TIMEOUT_SECONDS)
    return v if isinstance(v, int) and v > 0 else _DEFAULT_TIMEOUT_SECONDS


def search_concurrency() -> int:
    """How many searches may run at once, from settings or the pre-#328 default of 4."""
    v = _setting("search_concurrency", lambda s: int(float(s)), _DEFAULT_CONCURRENCY)
    return v if isinstance(v, int) and v > 0 else _DEFAULT_CONCURRENCY


def search_max_depth():
    """Deepest directory level a walk descends, from settings, or None (no limit, the default).

    Depth is counted from the search root: a file directly under the root is depth 0. A value
    of 0 therefore searches only the root directory itself. A missing or non-positive setting
    means None (unbounded), so nothing a legitimate deep search does changes until the owner
    sets a cap or a caller passes max_depth explicitly.
    """
    v = _setting("search_max_depth", lambda s: int(float(s)), _DEFAULT_MAX_DEPTH)
    if v is None:
        return None
    return v if isinstance(v, int) and v >= 0 else None

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


#: Kept as a module name for callers/tests that imported it before #328; the live value now
#: comes from search_timeout_seconds() so a setting change is honoured without a restart.
SEARCH_TIMEOUT_SECONDS = _DEFAULT_TIMEOUT_SECONDS


class WalkDeadline:
    def __init__(self, seconds=None):
        # An explicit `seconds` (a per-call override) wins; otherwise the live setting; the
        # setting itself falls back to the pre-#328 default. So a caller can ask for a heavier
        # search, an operator can raise the floor without a restart, and neither path can make
        # the budget zero or negative (search_timeout_seconds already clamps that).
        if seconds is not None and seconds > 0:
            self.seconds = int(seconds)
        else:
            self.seconds = search_timeout_seconds()
        self.end = time.monotonic() + self.seconds
        self.cut = False

    def expired(self):
        if time.monotonic() >= self.end:
            self.cut = True
        return self.cut


def iter_files(base: Path, skipped=None, deadline=None, max_depth=None):
    """Yield every file under `base`, skipping PRUNED_DIRS. A generator, never a list.

    os.walk rather than rglob because rglob cannot be told not to descend -- the pruning has
    to happen while walking, which is the entire saving.

    `skipped`, when given a set, collects the names actually pruned. Callers disclose on that
    rather than on the setting: a note saying "vendored directories were not searched" is a
    lie about a tree that contains none, and about a search of a single file.

    `max_depth` caps how deep the walk descends, counted from `base`: a file directly under
    `base` is depth 0, so max_depth=0 searches only `base` itself. None (the default) is the
    pre-#328 behaviour -- no depth limit. An explicit argument wins; otherwise the settings
    value (search_max_depth) is used, so the owner can set a floor-wide cap without a restart.
    Pruning by depth happens WHILE walking (dirs[:] is trimmed) so the cost of the deeper tree
    is never paid, exactly like the vendored-dir pruning beside it.
    """
    own_deadline = deadline is None
    deadline = deadline or WalkDeadline()
    prune = pruning_enabled()
    if max_depth is None:
        max_depth = search_max_depth()
    base_str = str(base)
    for root, dirs, files in os.walk(base_str):
        if deadline.expired():
            if own_deadline:
                raise TimeoutError("search deadline exceeded; results may be incomplete")
            return
        if prune:
            keep = []
            for d in dirs:
                if d in PRUNED_DIRS:
                    if skipped is not None:
                        skipped.add(d)
                else:
                    keep.append(d)
            dirs[:] = keep
        if max_depth is not None:
            # Depth of the directory whose children we are about to consider. os.path.relpath
            # of root against base is "." at the root (depth 0 for the files it holds), and one
            # path component deeper per level. A file in `root` sits at `depth`; descending
            # into a child directory would put its files at `depth + 1`. So once depth reaches
            # max_depth we still YIELD this directory's files but DO NOT descend further.
            rel = os.path.relpath(root, base_str)
            depth = 0 if rel == os.curdir else rel.count(os.sep) + 1
            if depth >= max_depth:
                dirs[:] = []
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
            if deadline.expired():
                if own_deadline:
                    raise TimeoutError("search deadline exceeded; results may be incomplete")
                return
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
