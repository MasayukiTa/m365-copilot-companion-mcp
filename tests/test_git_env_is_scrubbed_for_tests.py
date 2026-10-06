"""A test that runs `git init` under a hook's GIT_DIR must not touch the pushing repository.

Root cause found 2026-09-30: pre-push exports GIT_DIR; test_the_detached_scan_can_see_every_spelling
ran `git init tmp` and so set core.bare = true in the shared .git/config of the repo being pushed.
conftest.scrub_git_env removes the variables before any test runs.
"""
import os
import subprocess

import conftest


def _init_under(env, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path / "inner")], check=True, env=env)


def test_git_init_under_an_inherited_git_dir_does_not_touch_that_dir(tmp_path):
    outer = tmp_path / "outer.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outer)], check=True)
    before = (outer / "config").read_text()
    # A "work-tree-ish" outer repo, as a hook sees it, is then flipped by a stray init:
    (outer / "config").write_text(before.replace("bare = true", "bare = false"))
    before = (outer / "config").read_text()
    env = dict(os.environ, GIT_DIR=str(outer), GIT_WORK_TREE=str(tmp_path),
               GIT_INDEX_FILE=str(tmp_path / "idx"))
    conftest.scrub_git_env(env)
    assert not any(k in env for k in conftest.GIT_LOCATION_ENV)
    _init_under(env, tmp_path)
    assert (outer / "config").read_text() == before
    assert (tmp_path / "inner" / ".git").is_dir()


def test_without_the_scrub_the_outer_dir_is_hit(tmp_path):
    """The instrument check: the hazard is real, so the test above is not vacuous."""
    outer = tmp_path / "outer.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outer)], check=True)
    (outer / "config").write_text((outer / "config").read_text().replace("bare = true", "bare = false"))
    before = (outer / "config").read_text()
    env = dict(os.environ, GIT_DIR=str(outer))
    _init_under(env, tmp_path)
    assert (outer / "config").read_text() != before


def test_the_test_session_itself_carries_no_git_location_variables():
    assert not any(k in os.environ for k in conftest.GIT_LOCATION_ENV)
