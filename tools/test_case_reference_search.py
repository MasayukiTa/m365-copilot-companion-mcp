from tools.search_ops import case_reference_search

GOAL = "枝さん_679かすれ調査　これ調べて"


def test_reference_finds_folder_and_request_only(tmp_path):
    case = tmp_path / "枝さん_679かすれ調査"
    case.mkdir()
    (case / "やりたいこと.txt").write_text("設備と材料を調べる", encoding="utf-8")
    (tmp_path / "枝さん_679ワニス調査").mkdir()
    (tmp_path / "795Gプロジェクト.txt").write_text("679", encoding="utf-8")
    result = case_reference_search(GOAL, [tmp_path])
    assert not result["errors"] and not result["truncated"]
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["request"] == str(case / "やりたいこと.txt")
    assert "設備" in result["candidates"][0]["excerpt"]


def test_timeout_is_explicit(tmp_path):
    result = case_reference_search(GOAL, [tmp_path], timeout=0)
    assert "search timeout" in result["errors"]
