# -*- coding: utf-8 -*-
"""What this working tree has changed, in the four categories that need different decisions.

WHY, AND WHERE IT COMES FROM. The external analysis this repository has been taking ideas from
lists a `change-scope` reporter as its third item -- low priority, and explicitly not a fix for
the baseline rot the other two address. What it does support is a rule this repository already
has and enforces by hand: **never `git add -A`; stage the files this work changed, named**.

That rule exists because the working tree here carries directories that are neither ignored nor
ours -- scratch output, a security-tooling checkout, analysis dumps -- and several of them hold
identifying content. `scripts/check_no_identifying_names.py` says so on every run: "untracked
files carry identifying content (86). They are not public, and they are one `git add` from being
so." Staging by hand is the control, and staging by hand is also where a file gets missed.

FOUR CATEGORIES, BECAUSE THEY ARE FOUR DECISIONS:

    committed   already in HEAD on this branch but not on its upstream -- nothing to stage
    staged      in the index, will go in the next commit
    unstaged    tracked and modified, and will NOT go in unless named
    untracked   not in git at all -- the category where `-A` does the damage

DETERMINISTIC GIT. fsmonitor can report a stale index, an optional lock can make a read fail
under a concurrent operation, and a localised git prints status words this cannot parse. All
three are disabled for the child process rather than worked around in the parser.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*args: str) -> str:
    """Run git with the ambient state that could change its answer switched off."""
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LANG"] = env["LC_ALL"] = "C"
    out = subprocess.run(
        ["git", "-C", REPO, "-c", "core.fsmonitor=", "-c", "core.untrackedCache=false"]
        + list(args),
        capture_output=True, timeout=120, env=env)
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace")


def scope() -> dict[str, list[str]]:
    """{category: [paths]}. Empty lists rather than missing keys, so a caller can iterate."""
    staged, unstaged, untracked = [], [], []
    for line in _git("status", "--porcelain=v1", "--untracked-files=normal").splitlines():
        if len(line) < 4:
            continue
        x, y, path = line[0], line[1], line[3:].strip()
        # A RENAME REPORTS "old -> new"; the new name is the one anybody acts on.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if x == "?" and y == "?":
            untracked.append(path)
            continue
        if x != " ":
            staged.append(path)
        if y != " ":
            unstaged.append(path)

    committed = []
    ahead = _git("rev-list", "--count", "@{upstream}..HEAD").strip()
    if ahead.isdigit() and int(ahead) > 0:
        committed = [p for p in _git("diff", "--name-only", "@{upstream}..HEAD").splitlines() if p]

    return {"committed": committed, "staged": sorted(set(staged)),
            "unstaged": sorted(set(unstaged)), "untracked": sorted(set(untracked))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--add-line", action="store_true",
                    help="print a `git add` command naming the tracked changes only, so the "
                         "untracked ones stay a decision rather than a default")
    args = ap.parse_args(argv)

    s = scope()
    for kind in ("committed", "staged", "unstaged", "untracked"):
        paths = s[kind]
        print("%s (%d)%s" % (kind, len(paths), ":" if paths else ""))
        for p in paths:
            print("  %s" % p)
    if args.add_line:
        tracked = sorted(set(s["staged"]) | set(s["unstaged"]))
        print()
        print("git add " + " ".join(tracked) if tracked else "# nothing tracked to stage")
        if s["untracked"]:
            print("# %d untracked path(s) deliberately NOT included -- name them if they are "
                  "yours" % len(s["untracked"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
