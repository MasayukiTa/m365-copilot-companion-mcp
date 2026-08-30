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

#: An employee id, by shape.
#:
#: THE FIRST VERSION OF THIS NEVER MATCHED THE ACTUAL ID. It was `[A-Z]\d{6,}` -- a letter and
#: then six or more DIGITS -- and the real id interleaves letters with digits, so the check
#: written to catch it could not. It passed the repository throughout, and the leak was caught
#: by the home-directory rule instead, by luck: the id happened to be a folder name.
#:
#: The shape now is what such ids look like: seven to twelve upper-case alphanumerics starting
#: with a letter, at least five of them digits. That excludes git sha prefixes (lower case),
#: HTTP200, SHA256, ISO8601 and M365, all of which appear in this repository, and includes
#: both the observed id and the letter-then-digits form the old pattern was aiming at.
#: The candidate token; the digit count is applied to the TOKEN in `_IdShape.search`. Written
#: as a filter rather than as one clever regex because the clever version counted digits
#: anywhere in the LINE -- so `FASTEST` matched whenever the sentence around it happened to
#: contain five digits, and the check reported thirty-three false positives across the repo.
_ID_CANDIDATE = re.compile(r"\b[A-Z][A-Z0-9]{6,11}\b")
_ID_MIN_DIGITS = 5

#: An employee id carries a SHORT letter prefix. Without this the shape also matched
#: `TEST20260625` (a dated test fixture) and `C2F03A33` (a GUID fragment in vendored COM
#: interop) -- both of which identify nobody, and a check that cries about them is one people
#: learn to run with their eyes closed.
_ID_MAX_LETTERS = 3

#: A GUID: eight hex, then four, then four. Its first group satisfies the id shape exactly.
#: Built from a character class rather than written with \b, because an earlier edit put a
#: literal backspace here -- the pattern then required a control character at both ends and
#: never matched anything, silently.
_GUID = re.compile("[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}")


class _IdShape:
    """A regex-shaped object so the caller can treat it like the other patterns."""

    pattern = _ID_CANDIDATE.pattern

    def search(self, text):
        text = text or ""
        for m in _ID_CANDIDATE.finditer(text):
            token = m.group(0)
            digits = sum(c.isdigit() for c in token)
            letters = len(token) - digits
            if digits < _ID_MIN_DIGITS or letters > _ID_MAX_LETTERS:
                continue
            # A GUID's first group is eight hex characters and passes every test above. The
            # exclusion is on the SURROUNDING SHAPE rather than on the token being hex,
            # because an id like A123456 is also valid hex and excluding all hex would drop a
            # real one to spare a vendored COM interop file.
            if _GUID.search(text[max(0, m.start() - 2):m.end() + 30]):
                continue
            return m
        return None


ID_SHAPE = _IdShape()

#: A Windows home directory, which names whoever owns it -- unless the segment is one of the
#: names that identify nobody. Test fixtures need a plausible path, and flagging
#: `C:/Users/Public/...` would train a reader to ignore this check, which is worse than not
#: having it at all.
HOME_SHAPE = re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}([A-Za-z0-9_.~<>-]+)", re.I)

#: Segments that identify nobody. A docstring or fixture illustrating the SHAPE of a path is
#: a description of the problem rather than an instance of it, and flagging those teaches a
#: reader to skim past this check -- which is worse than not having it.
#:
#: "alice", "bob", "runner" and "administrator" were here and have been removed. Every one of
#: them can be somebody's actual account name, and a placeholder list that swallows a real one
#: is the same failure as no list at all. The names left are ones Windows reserves or that no
#: account is called.
NON_IDENTIFYING_USERS = {"public", "default", "defaultuser", "example", "test", "testuser",
                         "user", "username", "name", "hostedtoolcache",
                         "<user>", "<home>", "<you>", "<name>",
                         "...", "\\...", "x", "me"}

#: This file, which describes the check, and .gitignore, which has to name what it ignores.
ALLOWED = {".gitignore", "scripts/check_no_identifying_names.py"}

TEXT_SUFFIXES = (".py", ".md", ".txt", ".json", ".yml", ".yaml", ".ps1", ".sh", ".cs",
                 ".bat", ".vbs", ".cfg", ".ini", ".toml", ".html", ".css", ".js", ".jsonl")


def configured_names(repo="."):
    """The names to look for, from the environment or from .env.

    THE ENVIRONMENT ALONE WAS NOT ENOUGH. Nobody exports this before running the check by
    hand, so every local run took the "not set" branch, ran the shaped checks only, and
    printed "nothing identifying in N tracked files" -- a pass. Twenty-five occurrences of an
    eval host's ssh alias accumulated across five scripts under exactly that reading, in a
    public repository, and were found only when someone thought to set the variable.

    .env is gitignored, so it can hold the names without putting them in the repository, and
    it is already where this project keeps values of that kind. The environment still wins,
    so CI's secret is unaffected.
    """
    raw = os.environ.get(NAMES_ENV, "")
    if not raw.strip():
        try:
            with open(os.path.join(repo, ".env"), encoding="utf-8-sig") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    if k.strip() == NAMES_ENV:
                        raw = v.strip().strip('"')
                        break
        except OSError:
            pass
    return [n.strip() for n in raw.split(",") if n.strip()]


class CheckFailed(RuntimeError):
    """The check could not be performed. Never the same thing as finding nothing."""


def tracked_files(repo="."):
    """Every tracked path, or raise. A failed git call used to yield an empty list, and an
    empty list reads as "nothing identifying in 0 tracked files" -- a pass."""
    out = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True)
    if out.returncode != 0:
        raise CheckFailed("git ls-files failed in %s: %s"
                          % (repo, (out.stderr or "").strip()[:200]))
    files = [p for p in out.stdout.splitlines() if p.strip()]
    if not files:
        raise CheckFailed("git reported no tracked files in %s, which is not a repository "
                          "this check can vouch for" % repo)
    return files


#: Most hits listed from a single file. A file with more is already a decision to make in
#: one go, and the report should stay readable.
MAX_HITS_PER_FILE = 20


def offences(repo=".", names=None):
    """[(path, what, line_number, line)] for every tracked text file that identifies someone."""
    # THE REPO UNDER CHECK, not the current directory. The .env fallback read whichever
    # directory the process happened to start in, so checking a temp repository picked up
    # this one's names -- which is both the wrong answer and a test that passes for the
    # wrong reason.
    names = configured_names(repo) if names is None else names
    name_re = (re.compile("|".join(re.escape(n) for n in names), re.I)) if names else None
    found = []
    for rel in tracked_files(repo):
        if rel in ALLOWED:
            continue
        # THE PATH ITSELF. A file called after a person or a project discloses it without any
        # content being read, and a suffix whitelist never looks at the name at all.
        for what, pattern in (("employee-id shape in a path", ID_SHAPE),
                              ("configured name in a path", name_re)):
            if pattern is not None and pattern.search(rel):
                found.append((rel, what, 0, rel))
                break
        # EVERY OCCURRENCE IN THE FILE, NOT THE FIRST. This used to stop at the first hit per
        # file, on the reasoning that one report is enough to act on. It is not, and the way
        # it fails is worse than a longer report:
        #
        # 2026-08-28, scripts/win/lean_capture_isolate.py carried the same home directory on
        # two lines. CI printed "IDENTIFYING CONTENT IN TRACKED FILES (1)" and named line 14.
        # A reader -- this one -- fixed line 14 and would have pushed it as done. The count is
        # what does the damage: (1) reads as ONE PROBLEM, so the report actively argues that
        # the file is clean once the named line is gone.
        #
        # It would have been caught on the next CI run, so nothing escaped. But a leak check
        # that understates a leak spends its credibility to save a few lines of output.
        #
        # Bounded per file so one generated file cannot become the whole log.
        hits_here = 0
        try:
            with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if hits_here >= MAX_HITS_PER_FILE:
                        found.append((rel, "and more, not listed", 0,
                                      "stopped after %d hits in this file"
                                      % MAX_HITS_PER_FILE))
                        break
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
                        hits_here += 1
                        break          # one label per LINE; the next line is still checked
        except OSError as exc:
            # NOT SKIPPED SILENTLY. A file the check could not read is a file it cannot
            # vouch for, and the whole point of this script is that "we did not look" must
            # never come out looking like "we looked and it was fine".
            found.append((rel, "unreadable, so unchecked", 0, str(exc)[:100]))
    return found


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = [a for a in argv if not a.startswith("--")]
    strict = "--require-names" in argv
    repo = args[0] if args else "."
    names = configured_names(repo)

    try:
        found = offences(repo)
    except CheckFailed as exc:
        print("CHECK COULD NOT RUN: %s" % exc)
        return 2

    if not names:
        # STRICT IS FOR THE BRANCH THAT MATTERS. A fork PR has no secrets, so requiring the
        # names everywhere would fail every outside contribution for a reason they cannot
        # fix; requiring them nowhere means the rule called absolute is enforced only when
        # somebody remembered to set a variable.
        print("NOTE: %s is not set, so only the shaped checks ran (employee id, home path)."
              % NAMES_ENV)
        if strict:
            print("REFUSING TO PASS: --require-names was given and there are none. On the "
                  "branch this protects, a missing secret is a missing check, not a clean "
                  "result.")
            return 2

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
