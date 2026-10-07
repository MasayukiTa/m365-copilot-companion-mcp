# -*- coding: utf-8 -*-
"""The one command-line way to push a branch from this repository.

Wraps relay.git_push_safety so that a push from a terminal gets the same two guarantees
the library enforces: a forced push is always --force-with-lease (a bare --force is
refused), and a branch whose remote-tracking ref already equals the local tip is skipped.

  python scripts/safe_push.py                    push the current branch to origin
  python scripts/safe_push.py --force            replace history, but only if the remote
                                                 has not moved since we last fetched
  python scripts/safe_push.py --check            only report whether the remote already
                                                 matches; never pushes
  python scripts/safe_push.py --remote R --branch B

Exit code: 0 pushed or skipped, 1 git refused the push, 2 the request itself was unsafe.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import git_push_safety as gps   # noqa: E402
from tools.childproc import run as _run    # noqa: E402  -- locale-safe child output


def _current_branch(repo):
    out = _run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"])
    return out.stdout.strip() if out.returncode == 0 else ""


def main(argv=None, repo=None):
    ap = argparse.ArgumentParser(description="Push a branch with --force-with-lease safety.")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="", help="default: the current branch")
    ap.add_argument("--force", action="store_true",
                    help="replace remote history, using --force-with-lease")
    ap.add_argument("--check", action="store_true",
                    help="report whether the remote already matches, do not push")
    ap.add_argument("--repo", default=repo or os.getcwd())
    args, extra = ap.parse_known_args(argv)

    branch = args.branch or _current_branch(args.repo)
    if not branch or branch == "HEAD":
        print("safe_push: no branch to push (detached HEAD?); pass --branch", file=sys.stderr)
        return 2

    if args.check:
        same = gps.local_matches_remote(args.repo, args.remote, branch)
        print("%s/%s %s local %s" % (args.remote, branch,
                                     "already matches" if same else "differs from", branch))
        return 0

    try:
        res = gps.push_refspec(args.repo, args.remote, branch,
                               force=args.force, extra_args=extra)
    except gps.PushRefused as exc:
        print("safe_push: refused: %s" % exc, file=sys.stderr)
        return 2
    if res.skipped:
        print("safe_push: skipped: %s" % res.reason)
        return 0
    if res.stdout:
        print(res.stdout, end="")
    if res.stderr:
        print(res.stderr, end="", file=sys.stderr)
    return 0 if res.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
