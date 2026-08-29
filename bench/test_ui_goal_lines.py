"""Goals that survive the cockpit's newline split.

The cockpit splits its input on newlines: one line, one goal. A SWE problem statement is a
thousand characters across many lines, so pasted in raw it becomes dozens of fragments, each
dispatched as its own task -- a run that looks like it started while measuring something else.
"""
import io
import json

from bench.ui_goal_lines import from_goals_file, to_ui_lines, write_ui_file


def test_a_multiline_goal_becomes_exactly_one_line():
    """THE PROPERTY THE WHOLE FILE EXISTS FOR."""
    g = "一行目\n二行目\n\n四行目"
    lines = to_ui_lines([g])
    assert len(lines) == 1 and "\n" not in lines[0]


def test_the_line_round_trips_to_the_original_text():
    g = "問題文\nwith \"quotes\" and \backslashes\ and \ttabs"
    obj = json.loads(to_ui_lines([g])[0])
    assert obj["text"] == g


def test_a_line_is_a_json_object_so_the_cockpit_passes_it_through():
    """GoalsToJsonl passes a line through unchanged when it parses as a JSON object. A bare
    string would be wrapped a second time and the newlines would already have done their
    damage before it got there."""
    line = to_ui_lines(["x"])[0]
    assert line.startswith("{") and isinstance(json.loads(line), dict)


def test_extra_keys_survive():
    """The Continue flow needs keys beyond text; a staged goal may carry cwd or a campaign id,
    and dropping those silently would change what the run does."""
    obj = json.loads(to_ui_lines([{"text": "t", "cwd": "C:/x", "n": 3}])[0])
    assert obj["cwd"] == "C:/x" and obj["n"] == 3


def test_a_staged_goals_file_converts_one_line_per_goal(tmp_path):
    p = tmp_path / "goals.jsonl"
    p.write_text(json.dumps({"text": "multi\nline"}, ensure_ascii=False) + "\n"
                 + json.dumps({"text": "second"}, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    lines = from_goals_file(str(p))
    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == "multi\nline"


def test_a_plain_text_line_is_still_accepted(tmp_path):
    """Not every goals file is JSONL; a plain line must not be lost."""
    p = tmp_path / "g.txt"
    p.write_text("just some text\n", encoding="utf-8")
    assert json.loads(from_goals_file(str(p))[0])["text"] == "just some text"


def test_writing_the_ui_file_produces_one_physical_line_per_goal(tmp_path):
    src = tmp_path / "goals.jsonl"
    src.write_text(json.dumps({"text": "a\nb\nc"}, ensure_ascii=False) + "\n", encoding="utf-8")
    dst = tmp_path / "ui.txt"
    n = write_ui_file(str(src), str(dst))
    body = io.open(str(dst), encoding="utf-8").read()
    assert n == 1
    assert body.count("\n") == 1
