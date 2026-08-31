"""What may be written into a predictions file as an instance's patch.

The worktrees are git worktrees and the cleanup is rmtree(ignore_errors=True). On Windows that
removes the checked-out files and fails silently on the locked `.git` entry, leaving a
directory containing nothing but a pointer into the main repository. `git -C <that>` resolves
to THE HARNESS'S OWN REPOSITORY, so the capture step would write whatever was uncommitted in
it into the predictions file as that instance's patch -- and a grader would score it.

Measured on the live tree: all forty surviving worktrees reported this repository's latest
commit as HEAD and a dirty count of 36, which was the checkout being edited at the time.
"""
import os
import subprocess

import pytest


def _git(*args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _repo(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    _git("init", "-q", cwd=str(d))
    _git("config", "user.email", "t@example.invalid", cwd=str(d))
    _git("config", "user.name", "t", cwd=str(d))
    (d / "a.txt").write_text("one\n", encoding="utf-8")
    _git("add", "-A", cwd=str(d))
    _git("commit", "-qm", "base", cwd=str(d))
    return d


def test_a_husk_resolves_to_its_parent_repository():
    """The property that made this dangerous, stated as a fact about git rather than a guess:
    a directory inside a repository, with no repository of its own, IS that repository as far
    as `git -C` is concerned."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sub = os.path.join(here, "bench")
    top = _git("rev-parse", "--show-toplevel", cwd=sub).stdout.strip()
    assert os.path.normcase(os.path.abspath(top)) != os.path.normcase(os.path.abspath(sub))


def test_the_capture_checks_the_directory_is_the_repository_root(tmp_path):
    """The guard, run directly: a path whose toplevel is not itself must be refused."""
    outer = _repo(tmp_path, "outer")
    inner = outer / "inner"
    inner.mkdir()
    top = _git("rev-parse", "--show-toplevel", cwd=str(inner)).stdout.strip()
    assert os.path.normcase(os.path.abspath(top)) != os.path.normcase(os.path.abspath(str(inner)))


def test_a_real_repository_passes_the_same_check(tmp_path):
    """The guard must not close the door on the case capture exists for."""
    r = _repo(tmp_path, "real")
    top = _git("rev-parse", "--show-toplevel", cwd=str(r)).stdout.strip()
    assert os.path.normcase(os.path.abspath(top)) == os.path.normcase(os.path.abspath(str(r)))


def test_staged_changes_are_invisible_to_a_bare_diff(tmp_path):
    """WHY THE DIFF IS TAKEN AGAINST HEAD. `git diff` alone shows unstaged changes to tracked
    files, so a worker that staged its edit produced nothing -- and nothing is exactly what a
    wrong answer looks like, so the two were indistinguishable."""
    r = _repo(tmp_path, "staged")
    (r / "a.txt").write_text("two\n", encoding="utf-8")
    _git("add", "-A", cwd=str(r))
    assert _git("diff", cwd=str(r)).stdout.strip() == ""
    assert "two" in _git("diff", "HEAD", cwd=str(r)).stdout


def test_an_untracked_new_file_is_invisible_to_both(tmp_path):
    """A fix that adds a file is a fix. Neither diff sees it, so capture lists untracked files
    separately."""
    r = _repo(tmp_path, "untracked")
    (r / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert _git("diff", "HEAD", cwd=str(r)).stdout.strip() == ""
    others = _git("ls-files", "--others", "--exclude-standard", cwd=str(r)).stdout.split()
    assert others == ["new.py"]


def test_the_capture_source_takes_the_diff_against_head_and_lists_untracked():
    import io
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "pro_capture.py"), encoding="utf-8").read()
    assert '"diff", "HEAD"' in src
    assert '"ls-files", "--others", "--exclude-standard"' in src
    assert '"rev-parse", "--show-toplevel"' in src


def test_skipped_instances_are_announced():
    """A skipped instance is one the run did not measure. Silence is how a husk's
    parent-repository diff would have passed for an answer."""
    import io
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "pro_capture.py"), encoding="utf-8").read()
    assert "SKIPPED" in src and "skipped %d" in src


def test_an_instance_already_captured_is_not_walked_again(tmp_path, monkeypatch):
    """The skip list is where a real failure is seen.

    pro_wt_map.json accumulates across batches, so every later batch re-walked every earlier
    instance -- whose container capture had destroyed on purpose after recording its patch.
    Each failed with "no running container" and was reported as a SKIP: batch 2 read
    "captured 8, skipped 8", and by batch 5 there would have been 32 such lines with any real
    failure among them.
    """
    import io as _io
    import re as _re
    import ast as _ast
    src = _io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "bench", "pro_capture.py"), encoding="utf-8").read()
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, (_ast.FunctionDef, _ast.Module)):
            d = _ast.get_docstring(node)
            if d:
                src = src.replace(d, "")
    code = "\n".join(_re.sub(r"#.*$", "", ln) for ln in src.splitlines())
    assert "already = {" in code, "nothing computes the set of instances already captured"
    assert "if inst in already:" in code, "the loop does not skip them"
    # and it must be keyed on a NON-EMPTY patch: an instance recorded with an empty patch is
    # one nobody worked, and it deserves another attempt rather than being treated as done.
    m = _re.search(r"already = \{[^}]*\}", code, _re.S)
    assert "strip()" in m.group(0), (
        "an empty patch must not count as captured; that is an instance nobody worked")


# ── the counters, which added up to more work than was done ───────────────────────────────
#
# MEASURED. Batch 1 of the final run contained exactly ONE instance and reported
# "captured 1, skipped 1". The instance produced a 3,054,501-byte diff -- a worker had
# regenerated something wholesale rather than fixing a bug -- so the size guard correctly
# refused it, blanked the patch, recorded the skip, AND still incremented `captured`.
#
# A total that exceeds the work done is not cosmetic. The skip list is where a real failure is
# seen, and a captured count that includes the skips is how "we got 40" comes to be believed
# about a run that got fewer.

import io as _io
import re as _re

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = _io.open(os.path.join(_REPO, "bench", "pro_capture.py"), encoding="utf-8").read()

def test_an_oversize_diff_is_not_counted_as_captured():
    """SOURCE-LEVEL, and stated as such: this script drives git worktrees and containers. What
    is asserted is that the increment is guarded, in BOTH branches -- the routed one and the
    local one -- because the bug existed in both and fixing one would leave the other."""
    guarded = _re.findall(r"if not (?:_over|over):\s*\n\s*captured \+= 1", _SRC)
    assert len(guarded) == 2, (
        "expected the captured counter to be guarded in both the routed and local branches, "
        "found %d" % len(guarded))


def test_no_unguarded_increment_remains():
    """The whole defect was an unconditional `captured += 1` sitting after a skip was recorded.
    If one comes back, this fails."""
    for m in _re.finditer(r"captured \+= 1", _SRC):
        before = _SRC[:m.start()].rstrip().splitlines()[-1].strip()
        assert before.startswith("if not "), (
            "an unguarded `captured += 1` follows %r" % before[:60])


def test_the_skip_is_still_recorded_loudly():
    """Not counting it as captured must not turn into not mentioning it. A skipped instance is
    one this run did not measure, and silence there is how a real failure stops being noticed."""
    assert 'skipped.append((inst, "diff of %d bytes exceeds %d; not a fix"' in _SRC
    assert _SRC.count("not a fix") >= 2, "the size guard no longer reports in both branches"


def test_the_size_ceiling_is_still_enforced():
    """One instance once captured 105,722,582 bytes and made the predictions file 115 MB on a
    box with 2.7 GB free. The ceiling is the reason that cannot happen again."""
    assert "MAX_PATCH_BYTES = 1_000_000" in _SRC
