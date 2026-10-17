"""The benchmark's own arithmetic, pinned where no network, no model and no token is needed.

WHY THIS EXISTS AT ALL. `scripts/probes/bridge_bench.py` scores a live model against a string it
computes locally. Nothing in that live run can tell the difference between a model that got the
question wrong and a benchmark whose EXPECTED answer is wrong -- both show up as a failure, and
the second one is a number about the bench's own bug published as a number about the model. So the
ground truth is checked here instead: against a synthetic tree whose answers a reader can count,
against the spec's own literal 338350, and against a summary computed on hand-built rows whose
medians are the ones written down beside them.

NO NETWORK. The probe module IS imported (by path -- it lives under scripts/probes, outside every
importable package), which loads relay.chathub; that import is stdlib-only, so this stays hermetic.
The live half of the bench -- the token, the browser, the gateway -- is never touched here.
"""
import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CACHED = {}


def _bench():
    """The probe module, loaded by path and cached -- `import bridge_bench` cannot work, because
    scripts/probes is not on sys.path and is not a package."""
    if "m" not in _CACHED:
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        path = os.path.join(REPO, "scripts", "probes", "bridge_bench.py")
        spec = importlib.util.spec_from_file_location("bridge_bench", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CACHED["m"] = mod
    return _CACHED["m"]


#: The synthetic file every expectation is checked against. Two `def ` lines, an assignment of 8,
#: and a comment that MENTIONS the constant -- the anchored regex has to skip the comment, or a
#: benchmark would score the model against the value in its own prose.
SOCKET_TOOLS = '''"""Tool calling over a plain socket, where the model asks and THIS side executes.
"""
# MAX_CALLS_PER_REPLY is the cap this module applies; this comment is not the assignment.
MAX_CALLS_PER_REPLY = 8


def step(reply, run_tool):
    return None, []


def parse_calls(reply):
    return []
'''


def _tree(tmp_path):
    """A checkout with known answers under it -- counted by hand in the constants above, so a
    test failure names the expectation rather than the machine the test happened to run on."""
    relay = tmp_path / "relay"
    relay.mkdir()
    (relay / "socket_tools.py").write_text(SOCKET_TOOLS, encoding="utf-8")
    (relay / "test_a.py").write_text("# only here to be counted\n", encoding="utf-8")
    (relay / "test_b.py").write_text("# likewise\n", encoding="utf-8")
    return str(tmp_path)


# -- the expected answers ------------------------------------------------------------------------

def test_expected_glob_count_counts_the_files_the_glob_matches(tmp_path):
    """Pins that glob_count's expectation is the COUNT of matches under the given tree, not a
    number typed in beside the prompt -- a hardcoded answer would make the task unfalsifiable."""
    assert _bench().expected_glob_count(_tree(tmp_path)) == "2"


def test_expected_const_value_reads_the_assignment_and_ignores_the_comment(tmp_path):
    """THE ANCHOR, PINNED BY SHAPE. The synthetic file's comment names the constant first; an
    unanchored search would find the comment's words, or the wrong value, and report it as the
    model's error."""
    assert _bench().expected_const_value(_tree(tmp_path)) == "8"


def test_expected_def_count_counts_only_lines_that_start_with_def(tmp_path):
    """`def_count` asks about lines STARTING with `def `, so a line that merely mentions one must
    not be counted -- the synthetic file has exactly two, both at column 0."""
    assert _bench().expected_def_count(_tree(tmp_path)) == "2"


def test_expected_first_line_is_the_first_line_stripped(tmp_path):
    """The read_line answer is the file's first line, stripped. It must be read as text with the
    same tolerance the live task gets, or the expectation describes a different file."""
    assert _bench().expected_first_line(_tree(tmp_path)) == (
        '"""Tool calling over a plain socket, where the model asks and THIS side executes.')


def test_sum_of_squares_pins_the_number_the_spec_names(tmp_path):
    """The arithmetic expectation, written as the spec's own literal rather than recomputed the
    same way twice: if the helper and the spec ever disagree, one of them is wrong and this says
    so before a live run reports the model as the one that failed."""
    assert _bench().sum_of_squares() == "338350"


def test_expected_const_value_refuses_a_name_that_is_not_there(tmp_path):
    """A task whose ground truth cannot be computed must not silently score 0 -- that would blame
    the model for the benchmark's own failure to read the tree. The helper raises instead."""
    with pytest.raises(ValueError):
        _bench().expected_const_value(_tree(tmp_path), name="NO_SUCH_CONSTANT")


# -- the task list -------------------------------------------------------------------------------

def test_make_tasks_is_the_spec_five_in_the_spec_order(tmp_path):
    """The ids, their order and their prompts ARE the benchmark: five tasks in this order, each
    with an expectation that can actually be computed. A rename or a reorder silently changes what
    a published success rate is a rate OF, so it is pinned here as data."""
    tasks = _bench().make_tasks(_tree(tmp_path))
    assert [t[0] for t in tasks] == [
        "glob_count", "const_value", "def_count", "arithmetic", "read_line"]
    for task_id, prompt, expected_fn in tasks:
        assert prompt.strip(), task_id
        assert str(expected_fn()).strip(), task_id


# -- the summary ---------------------------------------------------------------------------------

def _row(task, success, seconds, turns, tool_calls, error=""):
    """One ask as `run_one` returns it, so the summary is tested against the shape it really gets."""
    return {"task": task, "success": success, "seconds": seconds, "turns": turns,
            "last_result": "Success" if success else "declined", "tool_calls": tool_calls,
            "error": error}


def test_summarize_reports_overall_rates_and_medians():
    """The overall numbers the operator reads first. An odd number of asks, so the median is a
    measured value rather than an average of two -- and one task's failure, so the rate is not 1."""
    rows = [_row("glob_count", True, 1.0, 1, 0),
            _row("glob_count", True, 9.0, 3, 4),
            _row("read_line", False, 2.0, 2, 2, error="TimeoutError")]
    s = _bench().summarize(rows)
    assert s["asks"] == 3
    assert s["success_rate"] == pytest.approx(2.0 / 3.0)
    assert s["median_seconds"] == pytest.approx(2.0)
    assert s["median_turns"] == pytest.approx(2.0)
    assert s["mean_tool_calls"] == pytest.approx(2.0)


def test_summarize_medians_an_even_number_of_asks_and_groups_by_task():
    """THE EVEN CASE, and the per-task split. With two asks of one task the median seconds is the
    MIDPOINT of the two (2.0 here, not 1.0 or 3.0), and the per-task rate is the task's own rather
    than the run's -- a summary that pooled them would hide a task the model cannot do at all."""
    rows = [_row("arithmetic", True, 1.0, 1, 1),
            _row("arithmetic", False, 3.0, 2, 3)]
    s = _bench().summarize(rows)
    assert s["median_seconds"] == pytest.approx(2.0)
    assert s["median_turns"] == pytest.approx(1.5)
    assert s["mean_tool_calls"] == pytest.approx(2.0)
    per = s["tasks"]["arithmetic"]
    assert per["asks"] == 2
    assert per["success_rate"] == pytest.approx(0.5)
    assert per["median_seconds"] == pytest.approx(2.0)
    assert per["mean_tool_calls"] == pytest.approx(2.0)


def test_summarize_of_nothing_is_zeroes_rather_than_an_error():
    """A run killed after setup and before the first ask must still be summarisable -- otherwise
    the failure report is the thing that fails, and the operator learns nothing about why."""
    s = _bench().summarize([])
    assert s["asks"] == 0
    assert s["success_rate"] == 0.0
    assert s["median_seconds"] == 0.0
    assert s["median_turns"] == 0.0
    assert s["mean_tool_calls"] == 0.0
    assert s["tasks"] == {}


def test_format_summary_names_every_task_and_the_words_the_operator_asked_for():
    """The printed summary is the deliverable an operator reads; it must name each task and say
    'success rate' and 'median turns' in words, not only as keys in the JSON."""
    rows = [_row("glob_count", True, 1.0, 1, 0),
            _row("read_line", False, 3.0, 2, 1, error="TimeoutError")]
    text = _bench().format_summary(_bench().summarize(rows))
    assert "success rate" in text
    assert "median turns" in text
    for task_id in ("glob_count", "read_line"):
        assert task_id in text


def test_format_table_has_one_line_per_ask_and_marks_the_failure():
    """One line per ask, with the failing one marked: the table is how a single bad ask in an
    otherwise green run is found, so a row that is not printed is a failure nobody sees."""
    rows = [_row("glob_count", True, 1.0, 1, 0),
            _row("read_line", False, 3.0, 2, 1, error="TimeoutError")]
    lines = _bench().format_table(rows).splitlines()
    assert len(lines) == 3                       # a header and the two asks
    assert "glob_count" in lines[1] and "ok" in lines[1]
    assert "FAIL" in lines[2] and "TimeoutError" in lines[2]
