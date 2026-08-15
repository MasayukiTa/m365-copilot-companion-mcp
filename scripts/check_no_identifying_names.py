"""Fail if a tracked file carries something that identifies the operator or their employer.

WHY THIS EXISTS RATHER THAN A NOTE IN A README

The rule is absolute and it has been broken twice. The history was rewritten once, in July, to
remove exactly these strings; they came back. This round they came back through a path no
review looks at: `frozen --snapshot` wrote the checkout's absolute path into a tracked baseline
file, so every snapshot pushed a username and a directory name to a PUBLIC repository, and
nobody reads a generated file. A rule kept by remembering lasts until the next generated file.

WHY IT MATCHES SHAPES AND NOT NAMES

The obvious implementation is a list of the forbidden words. That list would then BE the
disclosure: the words would live in this file, in a public repository, permanently, and a
history rewrite that scrubs the words also mangles the list that defines them -- which is
exactly what happened on the first attempt.

So nothing here spells any of them out.

  * The employee id has a SHAPE: a capital letter followed by six or more digits. A shape
    catches the id without naming it, and catches a colleague's too.
  * A home directory path on Windows carries a username by construction, whoever it is. An
    absolute path under Users/ in a tracked file is the defect, independent of the name.
  * The remaining names -- an employer, a hostname, a sibling project -- have no shape. They
    come from IDENTITY_NAMES in the environment, a comma-separated list, set locally and in CI
    as a secret. Absent, the check still runs and says which part it could not perform, so a
    missing secret degrades to a narrower check rather than to a silent pass.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

#: Where the unshaped names come from. A comma-separated list; never a default in this file.
NAMES_ENV = "IDENTITY_NAMES"

#: An employee id, by shape. Deliberately broad: a near-miss costs a moment, a miss is public.
ID_SHAPE = re.compile(r"\b[A-Z]\d{6,}\b")

#: A Windows home directory, which names whoever owns it -- unless the segment is one of the
#: names that identify nobody. Test fixtures need a plausible path, and flagging
#: `C:/Users/Public/...` would train a reader to ignore this check, which is worse than not
#: having it at all.
HOME_SHAPE = re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}([A-Za-z0-9_.~<>-]+)", re.I)

NON_IDENTIFYING_USERS = {"public", "default", "defaultuser", "example", "test", "testuser",
                         "user", "<user>", "<home>", "alice", "bob", "someone", "you",
                         "runner", "administrator", "hostedtoolcache",
                         # Placeholders. A docstring or fixture that illustrates the SHAPE of
                         # a path is a description of the problem, not an instance of it, and
                         # flagging those teaches a reader to skim past this check.
                         "...", "\\...", "x", "me", "name", "username", "<you>", "<name>"}

#: This file, which describes the check, and .gitignore, which has to name what it ignores.
ALLOWED = {".gitignore", "scripts/check_no_identifying_names.py"}

TEXT_SUFFIXES = (".py", ".md", ".txt", ".json", ".yml", ".yaml", ".ps1", ".sh", ".cs",
                 ".bat", ".vbs", ".cfg", ".ini", ".toml", ".html", ".css", ".js", ".jsonl")


def configured_names():
    raw = os.environ.get(NAMES_ENV, "")
    return [n.strip() for n in raw.split(",") if n.strip()]


def tracked_files(repo="."):
    out = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True)
    return [p for p in out.stdout.splitlines() if p.strip()]


def offences(repo=".", names=None):
    """[(path, what, line_number, line)] for every tracked text file that identifies someone."""
    names = configured_names() if names is None else names
    name_re = (re.compile("|".join(re.escape(n) for n in names), re.I)) if names else None
    found = []
    for rel in tracked_files(repo):
        if rel in ALLOWED or not rel.endswith(TEXT_SUFFIXES):
            continue
        try:
            with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    for what, pattern in (("employee-id shape", ID_SHAPE),
                                          ("home directory path", HOME_SHAPE),
                                          ("configured name", name_re)):
                        if pattern is None:
                            continue
                        m = pattern.search(line)
                        if not m:
                            continue
                        if (pattern is HOME_SHAPE
                                and m.group(1).lower() in NON_IDENTIFYING_USERS):
                            continue
                        found.append((rel, what, n, line.strip()[:120]))
                        break
                    else:
                        continue
                    break              # one report per file is enough to act on
        except OSError:
            continue
    return found


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    repo = argv[0] if argv else "."
    names = configured_names()
    found = offences(repo)

    if not names:
        print("NOTE: %s is not set, so only the shaped checks ran (employee id, home path). "
              "Set it to the comma-separated names that must not appear." % NAMES_ENV)

    if not found:
        print("nothing identifying in %d tracked files (%d configured name(s))"
              % (len(tracked_files(repo)), len(names)))
        return 0

    print("IDENTIFYING CONTENT IN TRACKED FILES (%d):" % len(found))
    for rel, what, n, line in found:
        print("  %s:%d  [%s]  %s" % (rel, n, what, line))
    print("")
    print("This repository is public and the rule has been broken twice. Remove these from the")
    print("working tree; if they were pushed, the history needs rewriting too, which is a")
    print("separate and more expensive job.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
