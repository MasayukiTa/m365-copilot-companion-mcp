"""Fail if a tracked file carries a name that must never enter this repository.

WHY THIS EXISTS RATHER THAN A NOTE IN A README

The rule is absolute and it has been broken twice. The history was rewritten once, in July, to
remove exactly these strings; they came back. This round they were reintroduced by a tool the
loop runs on itself -- `frozen --snapshot` wrote the checkout's absolute path into a tracked
baseline file, so every snapshot pushed a username and a directory name to a PUBLIC
repository, and nothing in any review looked at that file because it is generated.

A rule enforced by remembering is a rule that lasts until the next generated file.

WHAT IT CHECKS

Tracked files only -- an untracked scratch file is nobody's business, and .gitignore itself has
to be able to name what it ignores. The check is on the working tree, so it catches a leak
before it is committed; the history is a separate problem with a separate remedy.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

#: The names, as case-insensitive substrings. Deliberately short and deliberately blunt: a
#: near-miss is cheap to fix and a miss is a public disclosure.
FORBIDDEN = ("<user>", "<org>", "the eval host", "<sibling>")

#: Files that may legitimately mention a forbidden substring. Only .gitignore, which has to be
#: able to ignore a path by name, and this checker, which has to name what it forbids.
ALLOWED = {".gitignore", "scripts/check_no_identifying_names.py"}

#: Extensions worth reading. A binary match is almost always a false positive and a very slow
#: one; the names in question are typed by people into text.
TEXT_SUFFIXES = (".py", ".md", ".txt", ".json", ".yml", ".yaml", ".ps1", ".sh", ".cs",
                 ".bat", ".vbs", ".cfg", ".ini", ".toml", ".html", ".css", ".js", ".jsonl")


def tracked_files(repo="."):
    out = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True)
    return [p for p in out.stdout.splitlines() if p.strip()]


def offences(repo="."):
    """[(path, name, line_number, line)] for every tracked text file that names one."""
    found = []
    pattern = re.compile("|".join(re.escape(n) for n in FORBIDDEN), re.I)
    for rel in tracked_files(repo):
        if rel in ALLOWED or not rel.endswith(TEXT_SUFFIXES):
            continue
        full = os.path.join(repo, rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    m = pattern.search(line)
                    if m:
                        found.append((rel, m.group(0), n, line.strip()[:120]))
                        break          # one report per file is enough to act on
        except OSError:
            continue
    return found


def main(argv=None) -> int:
    repo = (argv or sys.argv[1:] or ["."])[0]
    found = offences(repo)
    if not found:
        print("no identifying names in %d tracked files" % len(tracked_files(repo)))
        return 0
    print("FORBIDDEN NAMES IN TRACKED FILES (%d):" % len(found))
    for rel, name, n, line in found:
        print("  %s:%d  [%s]  %s" % (rel, n, name, line))
    print("")
    print("These must not be in this repository -- it is public, and the rule has been broken")
    print("twice before. Remove them from the working tree; if they were pushed, the history")
    print("needs rewriting too, which is a separate and more expensive job.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
