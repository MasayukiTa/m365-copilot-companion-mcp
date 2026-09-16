# -*- coding: utf-8 -*-
"""Point this clone at the tracked hooks, and say whether it worked.

A hook that lives in .git/hooks is invisible to review, absent on every fresh clone, and
impossible to test -- so it is a rule kept by remembering, wearing a script's clothes.
`core.hooksPath` moves the hooks into the repository, where a diff shows them and a test can
assert they still do what they claim.

RUN THIS ONCE PER CLONE. It is deliberately not run automatically from anywhere: a repository
that silently rewrites a developer's git config on import is a worse problem than the one it
solves.
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS_DIR = ".githooks"


def current_hooks_path(repo=REPO):
    out = subprocess.run(["git", "-C", repo, "config", "--get", "core.hooksPath"],
                         capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def install(repo=REPO):
    subprocess.run(["git", "-C", repo, "config", "core.hooksPath", HOOKS_DIR],
                   check=True, capture_output=True, text=True)
    # Git on POSIX needs the bit; on Windows it is ignored, and setting it anyway keeps a
    # clone made on one platform working on the other.
    hook = os.path.join(repo, HOOKS_DIR, "pre-commit")
    try:
        os.chmod(hook, 0o755)
    except OSError:
        pass
    return current_hooks_path(repo)


def main():
    was = current_hooks_path()
    now = install()
    if now != HOOKS_DIR:
        print("FAILED: core.hooksPath is %r, expected %r" % (now, HOOKS_DIR))
        return 1
    print("core.hooksPath = %s%s" % (now, "" if was == now else "  (was %r)" % was))
    print("pre-commit now refuses a commit whose STAGED files carry identifying content.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
