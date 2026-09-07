"""Safer wrappers around the one dangerous half of git: sending refs to a remote.

There is deliberately ONE function here that talks to a remote, so a later reader looking
for "where do we push" finds a single answer rather than a scatter of ad-hoc
``subprocess.run(["git", "push", ...])`` calls that each have to be re-audited. Two
properties are enforced at that single point:

  * A force push is never a bare ``--force``. When history has to be replaced we use
    ``--force-with-lease`` instead, which refuses if the remote ref has moved since we
    last saw it -- so a concurrent push by someone else is detected and rejected rather
    than silently overwritten. ``push_refspec`` raises before running anything if a caller
    hands it a bare ``--force``.

  * A push whose local commit already matches what the remote ref points at is skipped.
    Re-pushing an identical ref is a no-op on the server, but it still opens a network
    connection and still prints as if work happened; short-circuiting it keeps the log
    honest about when a ref actually moved.

The git invocation is injected (``runner``) so the decision logic can be exercised without
a network or a real remote. The default runner shells out with the same shape the rest of
this package uses.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field


@dataclass
class GitResult:
    """The outcome of one git invocation, or of a decision not to invoke git."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    #: The argv actually handed to git, or None when we chose not to run it.
    argv: list[str] | None = None
    #: True when the push was skipped because the remote already matched.
    skipped: bool = False
    #: A short, human-readable reason, mainly for the skip case.
    reason: str = ""


class PushRefused(ValueError):
    """A push was rejected before contacting the remote because it was unsafe."""


def _default_runner(args, *, cwd):
    """Run ``git <args>`` under ``cwd`` and return a GitResult. Matches the package style."""
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
    )
    return GitResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        argv=["git", *args],
    )


def _rev_parse(runner, cwd, ref):
    """Return the 40-char sha ``ref`` resolves to, or None if it does not resolve."""
    res = runner(["rev-parse", "--verify", "--quiet", ref], cwd=cwd)
    if res.returncode != 0:
        return None
    sha = (res.stdout or "").strip()
    return sha or None


def local_matches_remote(repo, remote, branch, *, runner=None):
    """True when local ``branch`` already points at what ``remote/branch`` points at.

    Compares the commit ids of the local branch and its remote-tracking ref. Two commits
    with the same id have the same tree by construction, so an equal commit id is the
    strongest form of "the remote already has exactly this". If either ref cannot be
    resolved (no such local branch, or the remote-tracking ref was never fetched) the
    answer is False -- we do not know they match, so we must not skip.
    """
    run = runner or _default_runner
    local = _rev_parse(run, repo, "refs/heads/%s" % branch)
    if local is None:
        return False
    remote_ref = _rev_parse(run, repo, "refs/remotes/%s/%s" % (remote, branch))
    if remote_ref is None:
        return False
    return local == remote_ref


def _is_bare_force(token):
    """True for ``--force`` / ``-f`` but NOT for ``--force-with-lease[=...]``."""
    if token == "--force" or token == "-f":
        return True
    # ``--force=`` is not a real git spelling, but reject it too rather than pass it on.
    if token.startswith("--force="):
        return True
    return False


def push_refspec(repo, remote, branch, *, force=False, extra_args=None, runner=None):
    """Push ``branch`` to ``remote``, safely.

    * ``force=True`` uses ``--force-with-lease`` -- never a bare ``--force``.
    * If the remote-tracking ref already equals the local branch, no git push is run and a
      skipped GitResult is returned.
    * A bare ``--force`` / ``-f`` passed through ``extra_args`` is refused with PushRefused,
      so the safe path cannot be re-opened by a caller sneaking the flag back in.

    Returns a GitResult. ``skipped=True`` means the remote already matched.
    """
    run = runner or _default_runner
    extras = list(extra_args or [])
    for tok in extras:
        if _is_bare_force(tok):
            raise PushRefused(
                "refusing a bare '%s': use force=True, which sends --force-with-lease "
                "so a diverged remote is rejected instead of overwritten" % tok)

    if local_matches_remote(repo, remote, branch, runner=run):
        return GitResult(
            returncode=0,
            argv=None,
            skipped=True,
            reason="%s/%s already matches local %s; nothing to push" % (remote, branch, branch),
        )

    args = ["push"]
    if force:
        args.append("--force-with-lease")
    args.extend(extras)
    args.extend([remote, "refs/heads/%s:refs/heads/%s" % (branch, branch)])
    return run(args, cwd=repo)
