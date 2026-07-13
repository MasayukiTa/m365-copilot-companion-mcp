"""Hermetic unit tests for bench/review_build_goals.py.

Drives enumerate_files against a REAL tmp `git init` repo fixture (no network, no shared
state) -- that is the one required impurity the module has, per its own docstring.

  .venv\\Scripts\\python.exe -m pytest bench/test_review_build_goals.py -q
"""
import json
import os
import subprocess

import pytest

from bench.review_build_goals import (
    BINARY_EXTS,
    FINDINGS_BEGIN,
    FINDINGS_END,
    REVIEW_DIMENSIONS,
    REVIEW_RUBRIC,
    SECURITY_RUBRIC,
    build_dimension_goal,
    build_refute_goal,
    build_review_goal,
    dimensions_for_kind,
    enumerate_files,
    group_files,
    main,
    write_goals_jsonl,
)
from relay.refuter import LENS_PROMPTS, REFUTER_INSTRUCTION


def _run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, "cmd %r failed: %s\n%s" % (cmd, r.stdout, r.stderr)
    return r


def _init_repo(root):
    _run(["git", "init"], root)
    _run(["git", "config", "user.email", "fake@example.invalid"], root)
    _run(["git", "config", "user.name", "Fake Tester"], root)


def _write(root, rel, content="hello\n"):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(rel) else None
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return full


def _commit_all(root, msg="init"):
    _run(["git", "add", "-A"], root)
    _run(["git", "commit", "-m", msg], root)


@pytest.fixture
def repo(tmp_path):
    root = str(tmp_path / "fakerepo")
    os.makedirs(root, exist_ok=True)
    _init_repo(root)
    return root


# --- enumerate_files: mode="all" ----------------------------------------------------------

def test_enumerate_all_basic(repo):
    _write(repo, "a.py", "print(1)\n")
    _write(repo, "sub/b.py", "print(2)\n")
    _commit_all(repo)

    files = enumerate_files("all", repo)
    assert files == sorted(["a.py", "sub/b.py"])


def test_enumerate_all_excludes_binary_ext(repo):
    _write(repo, "keep.py", "print(1)\n")
    _write(repo, "asset.png", "not really png bytes but has the ext\n")
    assert ".png" in BINARY_EXTS
    _commit_all(repo)

    files = enumerate_files("all", repo)
    assert files == ["keep.py"]


def test_enumerate_all_excludes_oversize(repo):
    _write(repo, "small.py", "x = 1\n")
    _write(repo, "big.py", "x = 1\n" * 100000)  # well over 200_000 bytes
    _commit_all(repo)

    files = enumerate_files("all", repo, max_bytes=200_000)
    assert files == ["small.py"]

    # a caller-supplied larger cap lets it back in
    files2 = enumerate_files("all", repo, max_bytes=10_000_000)
    assert files2 == sorted(["small.py", "big.py"])


def test_enumerate_all_skips_deleted_file(repo):
    """git ls-files only lists what's tracked at HEAD, so 'deleted' here means: tracked,
    then removed from disk without `git rm` (mirrors a worktree with an in-flight delete
    that hasn't been staged) -- enumerate_files must skip it instead of raising."""
    _write(repo, "gone.py", "x = 1\n")
    _write(repo, "stays.py", "x = 2\n")
    _commit_all(repo)
    os.remove(os.path.join(repo, "gone.py"))

    files = enumerate_files("all", repo)
    assert files == ["stays.py"]


def test_enumerate_all_dedupes_and_sorts(repo):
    _write(repo, "z.py")
    _write(repo, "a.py")
    _write(repo, "m.py")
    _commit_all(repo)
    files = enumerate_files("all", repo)
    assert files == sorted(files)
    assert len(files) == len(set(files))


# --- enumerate_files: mode="diff" ---------------------------------------------------------

def test_enumerate_diff_unstaged(repo):
    _write(repo, "a.py", "one\n")
    _write(repo, "b.py", "one\n")
    _commit_all(repo)

    _write(repo, "a.py", "one\ntwo\n")  # modify, unstaged

    files = enumerate_files("diff", repo)
    assert files == ["a.py"]


def test_enumerate_diff_cached(repo):
    _write(repo, "a.py", "one\n")
    _commit_all(repo)

    _write(repo, "a.py", "one\ntwo\n")
    _run(["git", "add", "a.py"], repo)

    # unstaged diff sees nothing (change is staged)
    assert enumerate_files("diff", repo, cached=False) == []
    # --cached sees the staged change
    assert enumerate_files("diff", repo, cached=True) == ["a.py"]


def test_enumerate_diff_base_ref(repo):
    _write(repo, "a.py", "one\n")
    _commit_all(repo, "c1")
    _write(repo, "a.py", "one\ntwo\n")
    _commit_all(repo, "c2")
    _write(repo, "b.py", "new\n")
    _commit_all(repo, "c3")

    files = enumerate_files("diff", repo, base_ref="HEAD~2")
    assert files == sorted(["a.py", "b.py"])


def test_enumerate_diff_skips_deleted_file(repo):
    _write(repo, "gone.py", "x = 1\n")
    _write(repo, "stays.py", "x = 2\n")
    _commit_all(repo)
    os.remove(os.path.join(repo, "gone.py"))
    _write(repo, "stays.py", "x = 2\nmore\n")

    files = enumerate_files("diff", repo)
    assert files == ["stays.py"]


def test_enumerate_diff_excludes_binary_and_oversize(repo):
    _write(repo, "a.py", "one\n")
    _write(repo, "asset.png", "x\n")
    _write(repo, "big.py", "x = 1\n" * 100000)
    _commit_all(repo, "c1")

    _write(repo, "a.py", "one\ntwo\n")
    _write(repo, "asset.png", "y\n")
    _write(repo, "big.py", "x = 1\n" * 100001)

    files = enumerate_files("diff", repo)
    assert files == ["a.py"]


def test_enumerate_tolerates_git_failure(tmp_path):
    """Not a git repo at all -> git ls-files/diff fails; must return [] without raising."""
    not_a_repo = str(tmp_path / "not_a_repo")
    os.makedirs(not_a_repo, exist_ok=True)
    assert enumerate_files("all", not_a_repo) == []
    assert enumerate_files("diff", not_a_repo) == []


def test_enumerate_bad_mode_raises(repo):
    with pytest.raises(ValueError):
        enumerate_files("bogus", repo)


# --- group_files ---------------------------------------------------------------------------

def test_group_files_boundaries():
    assert group_files([], 3) == []
    assert group_files(["a"], 5) == [["a"]]
    assert group_files(["a", "b", "c", "d", "e"], 2) == [["a", "b"], ["c", "d"], ["e"]]
    assert group_files(["a", "b", "c"], 1) == [["a"], ["b"], ["c"]]
    assert group_files(["a", "b", "c"], 3) == [["a", "b", "c"]]


def test_group_files_never_drops_or_duplicates():
    files = ["f%d.py" % i for i in range(37)]
    groups = group_files(files, 5)
    flattened = [f for g in groups for f in g]
    assert flattened == files
    assert all(len(g) <= 5 for g in groups)
    assert len(groups[-1]) == 2  # 37 = 7*5 + 2


def test_group_files_size_below_one_treated_as_one():
    assert group_files(["a", "b"], 0) == [["a"], ["b"]]
    assert group_files(["a", "b"], -1) == [["a"], ["b"]]


# --- build_review_goal -----------------------------------------------------------------------

@pytest.mark.parametrize("kind, rubric", [("review", REVIEW_RUBRIC), ("security", SECURITY_RUBRIC)])
def test_build_review_goal_shape(kind, rubric):
    group = ["pkg/mod.py", "pkg/other_mod.py"]
    goal = build_review_goal(group, "C:/fakerepo", kind)

    assert set(goal.keys()) == {"text", "cwd"}
    assert goal["cwd"] == "C:/fakerepo"
    assert "checks" not in goal
    for f in group:
        assert f in goal["text"]
    assert FINDINGS_BEGIN in goal["text"]
    assert FINDINGS_END in goal["text"]
    assert "DONE" in goal["text"]


def test_build_review_goal_bad_kind_raises():
    with pytest.raises(ValueError):
        build_review_goal(["a.py"], "C:/fakerepo", "bogus")


def test_review_and_security_rubrics_differ():
    assert REVIEW_RUBRIC != SECURITY_RUBRIC
    assert "セキュリティ" in SECURITY_RUBRIC
    assert "重複" in REVIEW_RUBRIC or "簡潔化" in REVIEW_RUBRIC


# --- write_goals_jsonl: round-trip matching fleet_runner's line format -----------------------

def test_write_goals_jsonl_round_trip(tmp_path):
    goals = [
        {"text": "review these files: 日本語テキスト", "cwd": "C:/fakerepo"},
        {"text": "second goal", "cwd": "C:/fakerepo"},
    ]
    out = str(tmp_path / "nested" / "goals.jsonl")
    n = write_goals_jsonl(goals, out)
    assert n == 2
    assert os.path.isfile(out)

    with open(out, encoding="utf-8") as f:
        lines = [l for l in f.read().split("\n") if l]

    assert len(lines) == 2
    for line, original in zip(lines, goals):
        # mirrors relay/fleet_runner.py:_read_goals -- a line starting "{" is JSON-parsed
        assert line.startswith("{")
        assert json.loads(line) == original
        # ensure_ascii=False: Japanese text is literal, not \uXXXX-escaped
    assert "日本語テキスト" in lines[0]


def test_write_goals_jsonl_empty_list(tmp_path):
    out = str(tmp_path / "goals.jsonl")
    n = write_goals_jsonl([], out)
    assert n == 0
    assert os.path.isfile(out)
    assert open(out, encoding="utf-8").read() == ""


# --- main() end-to-end (still hermetic: fixture repo, no network) ---------------------------

def test_main_end_to_end(repo, tmp_path):
    _write(repo, "a.py", "one\n")
    _write(repo, "b.py", "two\n")
    _write(repo, "asset.png", "x\n")
    _commit_all(repo)

    out = str(tmp_path / "out" / "goals.jsonl")
    rc = main(["--repo-root", repo, "--mode", "all", "--kind", "security",
               "--group-size", "1", "--out", out])
    assert rc == 0
    assert os.path.isfile(out)

    with open(out, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 2  # a.py, b.py each in their own group (group-size 1); png excluded
    all_text = " ".join(g["text"] for g in lines)
    assert "a.py" in all_text and "b.py" in all_text
    assert "asset.png" not in all_text
    for g in lines:
        assert g["cwd"] == os.path.abspath(repo)
        assert "checks" not in g


# --- REVIEW_DIMENSIONS registry: well-formedness ---------------------------------------------

_REQUIRED_DIM_KEYS = {"key", "title", "rubric", "applies_to", "behavioral"}


def test_dimension_registry_well_formed():
    assert len(REVIEW_DIMENSIONS) > 0
    seen_keys = set()
    for dim in REVIEW_DIMENSIONS:
        assert _REQUIRED_DIM_KEYS <= set(dim.keys())
        assert isinstance(dim["key"], str) and dim["key"]
        assert dim["key"] not in seen_keys, "duplicate dimension key %r" % (dim["key"],)
        seen_keys.add(dim["key"])
        assert isinstance(dim["title"], str) and dim["title"]
        assert isinstance(dim["rubric"], str) and dim["rubric"]
        assert "{FILE_LIST_CWD}" in dim["rubric"]
        assert "{FILE_LIST}" in dim["rubric"]
        assert isinstance(dim["applies_to"], (set, frozenset))
        assert dim["applies_to"], "dimension %r applies to nothing" % (dim["key"],)
        assert dim["applies_to"] <= {"review", "security"}
        assert isinstance(dim["behavioral"], bool)


def test_dimension_registry_has_expected_keys():
    keys = {d["key"] for d in REVIEW_DIMENSIONS}
    assert keys == {
        "correctness", "security", "runtime_behavior", "deployment_operational",
        "test_hygiene", "false_green_ci", "missing_wiring", "adversarial_input",
        "cross_file_interaction",
    }


def test_dimension_registry_marks_behavioral_dimensions():
    behavioral_keys = {d["key"] for d in REVIEW_DIMENSIONS if d["behavioral"]}
    assert behavioral_keys == {"runtime_behavior", "adversarial_input"}


# --- dimensions_for_kind ----------------------------------------------------------------------

def test_dimensions_for_kind_review_excludes_security_only():
    review_keys = {d["key"] for d in dimensions_for_kind("review")}
    assert "security" not in review_keys  # security-only dimension
    assert "correctness" in review_keys
    assert "test_hygiene" in review_keys
    assert "missing_wiring" in review_keys
    assert "cross_file_interaction" in review_keys
    # shared dimensions still apply to review
    assert "runtime_behavior" in review_keys
    assert "adversarial_input" in review_keys


def test_dimensions_for_kind_security_excludes_review_only():
    security_keys = {d["key"] for d in dimensions_for_kind("security")}
    assert "security" in security_keys
    assert "correctness" not in security_keys  # review-only dimension
    assert "test_hygiene" not in security_keys
    assert "missing_wiring" not in security_keys
    assert "cross_file_interaction" not in security_keys
    # shared dimensions still apply to security
    assert "runtime_behavior" in security_keys
    assert "deployment_operational" in security_keys


def test_dimensions_for_kind_bad_kind_raises():
    with pytest.raises(ValueError):
        dimensions_for_kind("bogus")


def test_dimensions_for_kind_disjoint_partition_of_shared_dims():
    review_keys = {d["key"] for d in dimensions_for_kind("review")}
    security_keys = {d["key"] for d in dimensions_for_kind("security")}
    all_keys = {d["key"] for d in REVIEW_DIMENSIONS}
    assert review_keys | security_keys == all_keys
    # every dimension applies to at least one of review/security (registry well-formedness
    # already checked applies_to is non-empty and a subset of {"review","security"})


# --- build_dimension_goal ----------------------------------------------------------------------

@pytest.mark.parametrize("dim", REVIEW_DIMENSIONS, ids=[d["key"] for d in REVIEW_DIMENSIONS])
def test_build_dimension_goal_shape_and_contract(dim):
    group = ["pkg/mod.py", "pkg/other_mod.py"]
    goal = build_dimension_goal(dim, group, "C:/fakerepo")

    assert set(goal.keys()) == {"text", "cwd"}
    assert goal["cwd"] == "C:/fakerepo"
    assert "checks" not in goal
    for f in group:
        assert f in goal["text"]

    # dimension's own rubric focus text must be present
    assert dim["title"] in goal["text"]

    # the goal must carry the exact FINDINGS delimiters + a DONE instruction, in that order,
    # as the LAST thing appended to the goal (mirrors build_review_goal's own contract)
    assert FINDINGS_BEGIN in goal["text"]
    assert FINDINGS_END in goal["text"]
    assert "DONE" in goal["text"]
    end_idx = goal["text"].rfind(FINDINGS_END)
    done_idx = goal["text"].rfind("DONE")
    assert end_idx != -1 and done_idx != -1 and end_idx < done_idx

    # behavioral dimensions must instruct the agent to actually run and observe
    if dim["behavioral"]:
        assert "run_python" in goal["text"]
        assert "shell_exec" in goal["text"]
    else:
        # non-behavioral dimensions are not required to mention them, but if they do it
        # must not be the ONLY content (no accidental cross-contamination check needed --
        # this branch just documents the asymmetry the parametrized test is checking).
        pass


def test_build_dimension_goal_accepts_key_string_or_dict():
    group = ["a.py"]
    by_dict = build_dimension_goal(REVIEW_DIMENSIONS[0], group, "C:/fakerepo")
    by_key = build_dimension_goal(REVIEW_DIMENSIONS[0]["key"], group, "C:/fakerepo")
    assert by_dict == by_key


def test_build_dimension_goal_unknown_key_raises():
    with pytest.raises(ValueError):
        build_dimension_goal("not_a_real_dimension", ["a.py"], "C:/fakerepo")


def test_build_dimension_goal_tags_dimension_in_findings_example():
    goal = build_dimension_goal("correctness", ["a.py"], "C:/fakerepo")
    assert '"dimension": "correctness"' in goal["text"]


def test_build_dimension_goal_different_dimensions_differ():
    a = build_dimension_goal("correctness", ["a.py"], "C:/fakerepo")
    b = build_dimension_goal("test_hygiene", ["a.py"], "C:/fakerepo")
    assert a["text"] != b["text"]


# --- build_refute_goal (P1b: reuse relay/refuter.py instead of a bespoke verifier) ----------

def _finding(**over):
    base = {"file": "pkg/a.py", "line": 42, "severity": "high",
            "title": "SQL injection via string concat", "detail": "raw input reaches the query"}
    base.update(over)
    return base


def test_build_refute_goal_contains_verdict_instruction_and_claim():
    goal_text = build_refute_goal(_finding(), "review")
    # the refuter's own verdict-output contract must be present verbatim
    assert REFUTER_INSTRUCTION in goal_text
    assert "REFUTED" in goal_text and "UPHELD" in goal_text
    # the finding's claim (title + detail) must appear -- this is what gets attacked
    assert "SQL injection via string concat" in goal_text
    assert "raw input reaches the query" in goal_text
    assert "pkg/a.py" in goal_text
    # must NOT be wrapped in the FINDINGS delimiters -- different contract
    assert FINDINGS_BEGIN not in goal_text
    assert FINDINGS_END not in goal_text


def test_build_refute_goal_kind_review_defaults_to_correctness_lens():
    goal_text = build_refute_goal(_finding(dimension=None), "review")
    assert LENS_PROMPTS["correctness"] in goal_text


def test_build_refute_goal_kind_security_defaults_to_security_lens():
    finding = _finding(title="hardcoded API key")
    goal_text = build_refute_goal(finding, "security")
    assert LENS_PROMPTS["security"] in goal_text


def test_build_refute_goal_dimension_overrides_kind_default():
    # runtime_behavior maps to the "edge" lens regardless of kind="review" (whose own
    # kind-fallback would otherwise be "correctness")
    finding = _finding(dimension="runtime_behavior")
    goal_text = build_refute_goal(finding, "review")
    assert LENS_PROMPTS["edge"] in goal_text
    assert LENS_PROMPTS["correctness"] not in goal_text


def test_build_refute_goal_unknown_dimension_falls_back_to_kind():
    finding = _finding(dimension="not_a_real_dimension")
    goal_text = build_refute_goal(finding, "security")
    assert LENS_PROMPTS["security"] in goal_text


def test_build_refute_goal_explicit_lens_overrides_everything():
    finding = _finding(dimension="runtime_behavior")  # would normally pick "edge"
    goal_text = build_refute_goal(finding, "review", lens="security")
    assert LENS_PROMPTS["security"] in goal_text
    assert LENS_PROMPTS["edge"] not in goal_text


def test_build_refute_goal_returns_plain_string_not_dict():
    goal_text = build_refute_goal(_finding(), "review")
    assert isinstance(goal_text, str)


if __name__ == "__main__":
    import sys
    raise SystemExit(pytest.main([__file__, "-q"] + sys.argv[1:]))
