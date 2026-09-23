# -*- coding: utf-8 -*-
"""The identity guard has to be RUN, and until 2026-09-17 nothing made that happen.

scripts/check_no_identifying_names.py already knew how to refuse a staged employee id, and
already exited 1 when it saw one. On 2026-09-17 it said nothing useful because it was run
BEFORE `git add -A` and never again: at that moment the offending files were untracked, which
it reports as an advisory warning and a clean exit -- correctly, because an untracked file is
not public. One `git add -A` later they were public, the commit was pushed, and the fix was a
history rewrite for the third time.

So the defect was not in the guard. It was that the only thing scheduling the guard was a
person remembering to type it, and the moment that matters -- between staging and committing
-- has no person in it.

These tests pin the hook, not the guard. scripts/test_the_identity_guard_speaks_before_the_push.py
owns the guard's own behaviour on staged files; duplicating it here would mean two tests fail
for one cause and neither says which.
"""
from __future__ import annotations

import io
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools.childproc import run as _run   # noqa: E402  -- locale-safe child output

HOOK = os.path.join(REPO, ".githooks", "pre-commit")


def _hook_text():
    return io.open(HOOK, encoding="utf-8", errors="replace").read()


def test_the_hook_exists_and_is_tracked():
    """In .git/hooks it would be invisible to review and absent from every fresh clone --
    a rule kept by remembering, wearing a script's clothes."""
    assert os.path.isfile(HOOK), HOOK
    out = _run(["git", "-C", REPO, "ls-files", "--error-unmatch",
                ".githooks/pre-commit"])
    assert out.returncode == 0, "the hook is not tracked: %s" % (out.stderr or "").strip()


def test_the_hook_runs_the_identity_guard():
    body = _hook_text()
    assert "scripts/check_no_identifying_names.py" in body


def test_the_hook_blocks_rather_than_reports():
    """A hook that printed and exited 0 would be the same failure it exists to prevent."""
    body = _hook_text()
    assert "exit 1" in body


def test_a_guard_that_could_not_run_does_not_count_as_a_pass():
    """The guard returns 2 when it cannot run at all. Letting 2 through would turn a broken
    check into a silent one, which is the shape of every defect in this file's history."""
    body = _hook_text()
    # `cmd || { ...; exit 1; }` blocks on ANY non-zero status, which is what is wanted here;
    # what must not appear is a status that is singled out and forgiven.
    assert "|| {" in body or "if !" in body
    assert "-eq 2" not in body and "= 2 ]" not in body, \
        "the hook special-cases the could-not-run status"


def test_the_hook_does_not_offer_itself_a_way_around(tmp_path):
    """--no-verify exists and cannot be taken away; the hook must not suggest it as a step."""
    body = _hook_text()
    # It is named once, as the thing not to do. Twice would be an instruction.
    assert body.count("--no-verify") <= 1


# ── pre-push: the ratchets read the index, and the index is what gets pushed ──────────────

PUSH_HOOK = os.path.join(REPO, ".githooks", "pre-push")


def _push_hook_text():
    return io.open(PUSH_HOOK, encoding="utf-8", errors="replace").read()


def test_the_push_hook_exists_and_is_tracked():
    """**同じ形で2回 main を赤くした（2026-09-22, 09-23）。**掃くガードは `git ls-files` を
    見るので、「スイートを走らせた木」と「push する木」が違えば、CI が最初の読み手になる。"""
    assert os.path.isfile(PUSH_HOOK), PUSH_HOOK
    out = _run(["git", "-C", REPO, "ls-files", "--error-unmatch", ".githooks/pre-push"])
    assert out.returncode == 0, "the pre-push hook is not tracked: %s" % (out.stderr or "").strip()


def test_the_push_hook_runs_the_ratchets_that_read_the_index():
    """名指しで確認する。**この4つが、赤くなった2件とその同型**。"""
    body = _push_hook_text()
    for guard in ("scripts/check_nothing_new_is_invisible.py",
                  "tools/test_child_output_is_not_decoded_by_luck.py",
                  "tools/test_no_new_launch_inherits_its_console_by_accident.py",
                  "tools/test_a_backslash_in_a_literal_means_what_it_says.py",
                  "ui/test_one_list_says_what_the_ui_compiles.py"):
        assert guard in body, "the pre-push hook no longer runs %s" % guard
    assert "--strict" in body, \
        "the invisibility check runs in NOTE mode, where it cannot stop a push"


def test_the_push_hook_blocks_rather_than_reports():
    body = _push_hook_text()
    assert "exit 1" in body
    assert body.count("--no-verify") <= 1


def test_the_push_hook_stays_affordable():
    """**遅いフックは `--no-verify` されて、そこから先は何も守らない。**
    掃くラチェット全部は実測 7分37秒で、この4つは 40秒。高いほうを入れない判断は
    フック自身に書いてある。入れ直すなら、その計測ごと更新すること。"""
    body = _push_hook_text()
    assert "tools/test_nothing_new_is_built_without_a_caller.py" not in body, \
        "the unreached fixed point is in the push hook; it is minutes, and a hook people " \
        "bypass protects nothing. It runs in the full suite and in preflight."
    assert "7m37s" in body and "40s" in body, \
        "the hook no longer records what the subset was chosen against"


def test_the_installer_makes_every_hook_executable(tmp_path):
    """**POSIX で実行ビットが無いフックは、落ちるのではなく黙って飛ばされる。**
    ファイル名決め打ちの chmod は、2つ目のフックが増えた日に静かに破れる。"""
    if os.name == "nt":
        pytest.skip("Windows has no execute bit -- os.chmod here only toggles read-only, so "
                    "this asserts nothing locally. It is the POSIX clones it protects, and CI "
                    "runs it on ubuntu.")
    from scripts.install_git_hooks import install, HOOKS_DIR
    repo = tmp_path / "clone2"
    (repo / HOOKS_DIR).mkdir(parents=True)
    for name in ("pre-commit", "pre-push"):
        (repo / HOOKS_DIR / name).write_text("#!/bin/sh\n", encoding="utf-8")
    _run(["git", "init", "-q", str(repo)], check=True)
    install(str(repo))
    for name in ("pre-commit", "pre-push"):
        mode = (repo / HOOKS_DIR / name).stat().st_mode & 0o777
        assert mode & 0o100, "%s is not executable after install (mode %o)" % (name, mode)


def test_the_installer_points_the_clone_at_the_tracked_hooks(tmp_path):
    from scripts.install_git_hooks import install, current_hooks_path, HOOKS_DIR
    repo = tmp_path / "clone"
    (repo / HOOKS_DIR).mkdir(parents=True)
    (repo / HOOKS_DIR / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    _run(["git", "init", "-q", str(repo)], check=True)
    assert current_hooks_path(str(repo)) == ""
    assert install(str(repo)) == HOOKS_DIR
    assert current_hooks_path(str(repo)) == HOOKS_DIR


def test_this_clone_has_the_hook_installed():
    """Environment, not code -- so it SKIPS rather than fails where no local clone is being
    protected. CI checks tracked files after the fact; this is about the workstation where a
    commit is actually made, and a red CI job for a developer's unconfigured clone would train
    people to ignore it."""
    out = _run(["git", "-C", REPO, "config", "--get", "core.hooksPath"])
    configured = out.stdout.strip()
    if os.environ.get("CI"):
        pytest.skip("CI checks tracked files directly; hooks protect a workstation")
    assert configured == ".githooks", (
        "this clone will not run the identity guard before a commit. "
        "Run: python scripts/install_git_hooks.py  (got %r)" % configured)
