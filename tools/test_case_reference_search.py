from tools.search_ops import case_reference_search

GOAL = "sampleuser_679group_a_study　study this"


def test_reference_finds_folder_and_request_only(tmp_path):
    case = tmp_path / "sampleuser_679group_a_study"
    case.mkdir()
    (case / "やりたいこと.txt").write_text("この条件を調べる", encoding="utf-8")
    (tmp_path / "sampleuser_679group_b_study").mkdir()
    (tmp_path / "otherproject_p1.txt").write_text("679", encoding="utf-8")
    result = case_reference_search(GOAL, [tmp_path])
    assert not result["errors"] and not result["truncated"]
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["request"] == str(case / "やりたいこと.txt")
    assert "条件" in result["candidates"][0]["excerpt"]


def test_timeout_is_explicit(tmp_path):
    result = case_reference_search(GOAL, [tmp_path], timeout=0)
    assert "search timeout" in result["errors"]
