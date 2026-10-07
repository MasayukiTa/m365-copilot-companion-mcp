"""Tests for relay.git_push_safety: the two guarantees, exercised by calling the code.

The git invocation is faked so these run with no network and no real remote. Each test
calls push_refspec / local_matches_remote directly and inspects the argv the fake runner
was handed, which is exactly what a real git would have received.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from relay import git_push_safety as gps  # noqa: E402


class FakeGit:
    """Records every argv and answers rev-parse from a supplied ref table."""

    def __init__(self, refs):
        # refs maps a git ref string -> sha, or missing -> unresolvable
        self.refs = dict(refs)
        self.calls = []

    def __call__(self, args, *, cwd):
        self.calls.append(list(args))
        if args[:1] == ["rev-parse"]:
            ref = args[-1]
            sha = self.refs.get(ref)
            if sha is None:
                return gps.GitResult(returncode=1, stdout="", stderr="", argv=["git", *args])
            return gps.GitResult(returncode=0, stdout=sha + "\n", argv=["git", *args])
        # a push (or anything else) is reported as success
        return gps.GitResult(returncode=0, stdout="", argv=["git", *args])

    def pushes(self):
        return [c for c in self.calls if c[:1] == ["push"]]


def test_force_push_uses_lease_never_bare_force():
    # local ahead of remote -> a push is warranted; force must become --force-with-lease.
    fake = FakeGit({
        "refs/heads/feat": "a" * 40,
        "refs/remotes/origin/feat": "b" * 40,
    })
    res = gps.push_refspec("/repo", "origin", "feat", force=True, runner=fake)
    assert res.skipped is False
    pushes = fake.pushes()
    assert len(pushes) == 1
    argv = pushes[0]
    assert "--force-with-lease" in argv
    assert "--force" not in argv
    assert "-f" not in argv
    # the refspec targets the intended branch on the intended remote
    assert "origin" in argv
    assert "refs/heads/feat:refs/heads/feat" in argv


def test_non_force_push_has_no_force_flag():
    fake = FakeGit({
        "refs/heads/feat": "a" * 40,
        "refs/remotes/origin/feat": "b" * 40,
    })
    gps.push_refspec("/repo", "origin", "feat", force=False, runner=fake)
    argv = fake.pushes()[0]
    assert "--force-with-lease" not in argv
    assert "--force" not in argv


def test_push_skipped_when_remote_already_matches():
    same = "c" * 40
    fake = FakeGit({
        "refs/heads/feat": same,
        "refs/remotes/origin/feat": same,
    })
    res = gps.push_refspec("/repo", "origin", "feat", force=True, runner=fake)
    assert res.skipped is True
    assert res.returncode == 0
    assert fake.pushes() == []          # no push ran at all
    assert "already matches" in res.reason


def test_push_runs_when_remote_ref_absent():
    # First push of a brand-new branch: no remote-tracking ref yet -> must NOT be skipped.
    fake = FakeGit({
        "refs/heads/feat": "a" * 40,
        # no refs/remotes/origin/feat
    })
    res = gps.push_refspec("/repo", "origin", "feat", runner=fake)
    assert res.skipped is False
    assert len(fake.pushes()) == 1


def test_bare_force_in_extra_args_is_refused():
    fake = FakeGit({
        "refs/heads/feat": "a" * 40,
        "refs/remotes/origin/feat": "b" * 40,
    })
    for bad in (["--force"], ["-f"], ["--force=origin"]):
        with pytest.raises(gps.PushRefused):
            gps.push_refspec("/repo", "origin", "feat", extra_args=bad, runner=fake)
    assert fake.pushes() == []          # refused before any push


def test_local_matches_remote_false_when_local_branch_missing():
    fake = FakeGit({"refs/remotes/origin/feat": "b" * 40})
    assert gps.local_matches_remote("/repo", "origin", "feat", runner=fake) is False


def test_local_matches_remote_true_on_equal_sha():
    same = "d" * 40
    fake = FakeGit({
        "refs/heads/feat": same,
        "refs/remotes/origin/feat": same,
    })
    assert gps.local_matches_remote("/repo", "origin", "feat", runner=fake) is True
