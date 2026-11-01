# -*- coding: utf-8 -*-
"""A goal that is a FULL PATH to the case (not the short reference shape) must still resolve.

Job bfa6b22fac93 ran one turn and finished "judge: none". Its goal was exactly a Downloads
full path to the request folder followed by 「これ調べて」. The reference regex forbids path
separators, so case_reference_search returned None, the request file was never read, and the
goal classified as non-analysis. These tests pin the full-path shape to the same resolved
candidate the short reference produces.
"""
from tools.search_ops import case_reference_search

# The EXACT shape of the real goal: an absolute path whose final component is the case folder,
# then a full-width space, then the words. (job bfa6b22fac93)
CASE_NAME = "枝さん_679かすれ調査"


def _full_path_goal(tmp_path):
    return str(tmp_path / CASE_NAME) + "\u3000これ調べて"


def test_full_path_goal_resolves_the_request_file(tmp_path):
    case = tmp_path / CASE_NAME
    case.mkdir()
    (case / "やりたいこと.txt").write_text(
        "ADAMSデータと実データを参照してかすれの原因を調べる", encoding="utf-8")
    result = case_reference_search(_full_path_goal(tmp_path), [tmp_path])
    assert result is not None
    assert not result["errors"] and not result["truncated"]
    assert len(result["candidates"]) == 1
    cand = result["candidates"][0]
    assert cand["kind"] == "directory"
    assert cand["request"] == str(case / "やりたいこと.txt")
    assert "ADAMS" in cand["excerpt"]
    assert cand["case_id"] == "679"


def test_full_path_goal_resolves_even_without_configured_roots(tmp_path):
    # The real goal carries an absolute path, so resolution must not depend on the path being
    # under one of the configured M365_CASE_ROOTS -- the pasted path IS the location.
    case = tmp_path / CASE_NAME
    case.mkdir()
    (case / "やりたいこと.txt").write_text("ADAMS", encoding="utf-8")
    # roots=[] -> the walk would find nothing; only the full-path resolver can succeed.
    result = case_reference_search(_full_path_goal(tmp_path), [])
    assert result is not None
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["request"] == str(case / "やりたいこと.txt")


def test_full_path_directly_to_the_request_file_resolves_its_folder(tmp_path):
    # If the user pastes the request .txt itself, the case is its parent folder.
    case = tmp_path / CASE_NAME
    case.mkdir()
    req = case / "やりたいこと.txt"
    req.write_text("ADAMS", encoding="utf-8")
    goal = str(req) + " これ調べて"
    result = case_reference_search(goal, [])
    assert result is not None
    assert len(result["candidates"]) == 1
    cand = result["candidates"][0]
    assert cand["kind"] == "directory"
    assert cand["request"] == str(req)


def test_a_bare_nonexistent_path_is_not_resolved(tmp_path):
    # A full path whose final component has the case shape but does not exist must not be
    # invented as a candidate; it falls through (returns None here, no candidates).
    goal = str(tmp_path / CASE_NAME) + " これ調べて"
    result = case_reference_search(goal, [tmp_path])
    # Nothing on disk: the resolver declines, the walk finds nothing -> empty candidates.
    assert result is None or result["candidates"] == []


def test_an_arbitrary_file_path_without_case_shape_is_ignored(tmp_path):
    # A plain path whose name lacks the <title>_<NN><topic> shape must not be swept in.
    other = tmp_path / "notes.txt"
    other.write_text("x", encoding="utf-8")
    goal = str(other) + " これ調べて"
    result = case_reference_search(goal, [])
    assert result is None
