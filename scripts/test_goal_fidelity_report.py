"""goal_fidelity_report: extraction, drift score, false-denial detection, file loading."""
import gzip
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import goal_fidelity_report as R  # noqa: E402

GOAL = ("Plan a hike. ただし「Alpha Hut」から入山すること。10/3 14:00ごろ出発し、"
        "絶対に上市駅へ戻ること。")


def _write(path, goal, replies, gz=False):
    lines = [json.dumps({"meta": True, "goal": goal})]
    for i, t in enumerate(replies, 1):
        lines.append(json.dumps({"turn": i, "role": "user", "text": "u"}))
        lines.append(json.dumps({"turn": i, "role": "assistant", "text": t}, ensure_ascii=False))
    data = "\n".join(lines) + "\n"
    if gz:
        with gzip.open(str(path), "wt", encoding="utf-8") as fh:
            fh.write(data)
    else:
        path.write_text(data, encoding="utf-8")


def test_extract_tokens_finds_quotes_times_dates_and_cue_runs():
    toks = R.extract_tokens(GOAL)
    assert "Alpha Hut" in toks
    assert "14:00" in toks
    assert "10/3" in toks
    assert "上市駅" in toks


def test_extraction_is_bounded_and_deterministic():
    goal = " ".join("%d:%02d" % (h, m) for h in range(1, 12) for m in (0, 15, 30, 45))
    assert len(R.extract_tokens(goal)) == R.MAX_TOKENS
    assert R.extract_tokens(GOAL) == R.extract_tokens(GOAL)


def test_drift_counts_later_turns_with_no_token_after_an_earlier_hit():
    r = R.retention(["14:00"], ["plan 14:00", "still 14:00", "generic", "generic"])
    assert r["later_turns"] == 3 and r["drifted_turns"] == 2
    assert r["drift_score"] == round(2 / 3, 4)


def test_no_drift_when_the_tokens_were_never_echoed():
    r = R.retention(["14:00"], ["a", "b", "c"])
    assert r["drifted_turns"] == 0 and r["drift_score"] == 0.0


def test_full_width_digits_and_line_breaks_do_not_hide_a_token():
    r = R.retention(["14:00"], ["１４：００ に\n出発"])
    assert r["per_turn"][0] == ["14:00"]


def test_a_denial_of_something_the_goal_says_is_false():
    d = R.denials(GOAL, ["この具体行程 14:00 はユーザー原文に存在しません。"])
    assert (d["false"], d["true"], d["unresolved"]) == (1, 0, 0)


def test_a_denial_of_something_the_goal_lacks_is_true():
    d = R.denials(GOAL, ["「Beta Lodge」は原文に存在しません。"])
    assert (d["false"], d["true"]) == (0, 1)


def test_a_denial_with_no_checkable_item_is_unresolved():
    d = R.denials(GOAL, ["その条件は指示にありません。"])
    assert d["unresolved"] == 1 and d["false"] == 0


def test_a_must_token_names_the_item():
    d = R.denials(GOAL, ["上市駅の指定は記載がありません。"], must=["上市駅"])
    assert d["false"] == 1


def test_non_denial_sentences_are_ignored():
    d = R.denials(GOAL, ["14:00 に出発します。"])
    assert d == {"false": 0, "true": 0, "unresolved": 0, "examples": []}


def test_analyse_reads_jsonl_and_gz_by_run_id_and_totals(tmp_path):
    _write(tmp_path / "rX_a0_w0.jsonl", GOAL, ["14:00 出発", "generic", "generic"])
    _write(tmp_path / "rX_a0_w1.jsonl.gz", GOAL, ["14:00", "14:00 は原文に存在しません"], gz=True)
    _write(tmp_path / "rY_a0_w0.jsonl", GOAL, ["only one"])
    res = R.analyse(["rX_a0", "rY_a0"], str(tmp_path))
    x, y = res["runs"]
    assert x["transcripts"] == 2 and x["assistant_turns"] == 5
    assert x["later_turns"] == 3 and x["drifted_turns"] == 2
    assert x["false_denials"] == 1
    assert y["later_turns"] == 0
    assert res["total"]["transcripts"] == 3
    assert "TOTAL" in R.format_table(res)


def test_the_reader_never_writes(tmp_path):
    p = tmp_path / "rZ_a0_w0.jsonl"
    _write(p, GOAL, ["a"])
    before = p.read_bytes()
    R.analyse(["rZ_a0"], str(tmp_path))
    assert p.read_bytes() == before


def test_cli_prints_a_table_and_json(tmp_path, capsys):
    _write(tmp_path / "rQ_a0_w0.jsonl", GOAL, ["14:00", "x"])
    out = tmp_path / "o.json"
    assert R.main(["rQ_a0", "--dir", str(tmp_path), "--json", str(out)]) == 0
    assert "rQ_a0" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["total"]["transcripts"] == 1
