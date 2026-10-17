"""The benchmark's own arithmetic, pinned where no network, no model and no token is needed.

WHY THIS EXISTS AT ALL. `scripts/probes/bridge_bench.py` scores a live model against a string it
computes locally. Nothing in that live run can tell the difference between a model that got the
question wrong and a benchmark whose EXPECTED answer is wrong -- both show up as a failure, and
the second one is a number about the bench's own bug published as a number about the model. So the
ground truth is checked here instead: against a synthetic tree whose answers a reader can count,
against the spec's own literal 338350, and against a summary computed on hand-built rows whose
medians are the ones written down beside them.

THE LONG TASKS ARE CHECKED HERE TOO. The three of them answer with several parts (a count, a file
name, a line count), so the same argument applies twice over: a wrong expectation would look like a
model that cannot count, and a scorer that accepted only one part would call half an answer a
success. Both the expectations and the all-parts scorer are pinned below.

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
    (relay / "test_c.py").write_text("# a third, so the glob clears MIN_MATCHES\n", encoding="utf-8")
    return str(tmp_path)


# -- the expected answers ------------------------------------------------------------------------

def test_expected_glob_count_counts_the_files_the_glob_matches(tmp_path):
    """Pins that glob_count's expectation is the COUNT of matches under the given tree, not a
    number typed in beside the prompt -- a hardcoded answer would make the task unfalsifiable. The
    tree has three `test_*.py`, which is also the fewest the glob is allowed to match."""
    assert _bench().expected_glob_count(_tree(tmp_path)) == "3"


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

def _row(task, success, seconds, turns, tool_calls, error="", *, harness_error=False,
         error_msg="", answer_head=""):
    """One ask as `run_one` returns it, so the summary is tested against the shape it really gets.

    `harness_error` marks a row the model never saw (the ground truth could not be computed); it is
    excluded from the success rate and counted on its own. `error_msg` and `answer_head` are the
    two redacted strings a failed row now carries so one failure reads differently from another.
    """
    return {"task": task, "success": success, "harness_error": harness_error, "seconds": seconds,
            "turns": turns, "last_result": "Success" if success else "declined",
            "tool_calls": tool_calls, "error": error, "error_msg": error_msg,
            "answer_head": answer_head}


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


# -- the wall clock: one run, several asks in flight -------------------------------------------------

def test_asks_per_minute_is_asks_over_the_wall_clock():
    """THE NUMBER `--parallel` EXISTS TO SHOW. Two asks in a 6-second wall is 20 a minute, whether
    they ran one after the other or side by side -- the throughput is the run's, not a row's."""
    assert _bench().asks_per_minute(2, 6.0) == pytest.approx(20.0)


def test_asks_per_minute_of_no_wall_time_is_zero_not_a_division():
    """A run with no usable wall clock (killed before any ask) reads 0.0, not ZeroDivisionError --
    the failure report must not be the thing that fails."""
    assert _bench().asks_per_minute(4, 0.0) == 0.0
    assert _bench().asks_per_minute(0, 0.0) == 0.0


def test_summarize_reports_the_wall_clock_and_the_throughput():
    """The summary gains the run's own elapsed time and asks-per-minute, alongside the existing
    success rate -- the three numbers the operator reads first."""
    rows = [_row("glob_count", True, 1.0, 1, 0),
            _row("glob_count", True, 3.0, 2, 2)]
    s = _bench().summarize(rows, wall_s=12.0)
    assert s["asks"] == 2
    assert s["success_rate"] == pytest.approx(1.0)
    assert s["wall_s"] == pytest.approx(12.0)
    assert s["asks_per_minute"] == pytest.approx(10.0)     # 2 asks in 12 s


def test_summarize_without_a_wall_clock_still_works():
    """`wall_s` defaults to 0, so every existing caller -- and a hand-built summary -- keeps
    working: the new keys are present as zeroes rather than absent."""
    s = _bench().summarize([_row("read_line", True, 1.0, 1, 1)])
    assert s["wall_s"] == 0.0
    assert s["asks_per_minute"] == 0.0


def test_format_summary_names_the_wall_clock_and_the_throughput():
    """The printed summary is the deliverable, so the two new numbers have to appear in WORDS there
    and not only as JSON keys."""
    text = _bench().format_summary(
        _bench().summarize([_row("glob_count", True, 1.0, 1, 0)], wall_s=30.0))
    assert "asks/min" in text
    assert "wall" in text


# -- the long tasks' ground truth -----------------------------------------------------------------

def _long_tree(tmp_path):
    """A checkout whose long-task answers are counted by hand as the constants in each test, plus a
    file in a SUBDIRECTORY that every one of the three must ignore -- each asks about `relay/`
    directly, and a walk that descended would answer a different question."""
    relay = tmp_path / "relay"
    relay.mkdir()
    (relay / "aa.py").write_text("top\nConversation\n", encoding="utf-8")       # 2 lines, carries it
    (relay / "bb.py").write_text("Conversation\n" * 5, encoding="utf-8")        # 5 lines, carries it
    (relay / "cc.py").write_text("nothing to see here\n", encoding="utf-8")     # 1 line, does not
    (relay / "test_tool_a.py").write_text("a\nb\nc\n", encoding="utf-8")        # 3 lines
    (relay / "test_tool_b.py").write_text("only\n", encoding="utf-8")           # 1 line
    (relay / "test_socket_a.py").write_text("x\ny\n", encoding="utf-8")         # 2 lines, line_sum
    (relay / "test_socket_b.py").write_text("x\ny\n", encoding="utf-8")         # 2 lines, line_sum
    (relay / "test_socket_c.py").write_text("only\n", encoding="utf-8")         # 1 line, line_sum
    sub = relay / "sub"
    sub.mkdir()
    (sub / "dd.py").write_text("Conversation\n" * 9, encoding="utf-8")          # 9 lines, out of scope
    return str(tmp_path)


def test_expected_files_with_text_counts_the_hits_and_names_the_longest(tmp_path):
    """The first long task's answer: how many files carry the text, and which is longest. Its name
    and its line count are facts about the tree, computed before the model is asked."""
    assert _bench().expected_files_with_text(_long_tree(tmp_path)) == ["2", "bb.py", "5"]


def test_expected_files_with_text_ignores_a_subdirectory(tmp_path):
    """THE NON-RECURSION, PINNED. `relay/sub/dd.py` carries the text and is the longest file in the
    synthetic tree; a recursive walk would hand the model a question the prompt did not ask and
    score it against an answer nobody could give."""
    got = _bench().expected_files_with_text(_long_tree(tmp_path))
    assert got[0] == "2" and got[2] == "5" and "dd.py" not in got


def test_expected_largest_py_is_the_top_three_with_their_line_counts(tmp_path):
    """The second long task: three names AND three counts, in descending line order. A scorer that
    checked only the names would accept a model that never measured anything."""
    assert _bench().expected_largest_py(_long_tree(tmp_path)) == [
        "bb.py", "5", "test_tool_a.py", "3", "aa.py", "2"]


def test_expected_largest_py_refuses_a_tree_that_is_too_small(tmp_path):
    """Asking for more files than exist is an expectation that cannot be computed, so it raises and
    becomes a labelled row -- never a zero scored as though the model had failed."""
    with pytest.raises(ValueError):
        _bench().expected_largest_py(_long_tree(tmp_path), count=99)


def test_expected_line_sum_adds_every_matched_file(tmp_path):
    """The third long task: 2 + 2 + 1 lines across the three matched `test_socket_*.py` files. One
    number, but it takes a look at every file the glob matches -- which is why the task is here, and
    three of them is also the fewest the glob is allowed to match."""
    assert _bench().expected_line_sum(_long_tree(tmp_path)) == ["5"]


def test_expected_line_sum_refuses_a_glob_that_matches_nothing(tmp_path):
    """A tree with nothing to add must raise rather than report a total of 0: 0 is also what a
    model that summed nothing would say, and the two must not look alike."""
    with pytest.raises(ValueError):
        _bench().expected_line_sum(_long_tree(tmp_path), pattern="relay/test_nope_*.py")


# -- the all-parts scorer --------------------------------------------------------------------------

def test_answer_has_a_string_still_requires_that_substring():
    """The short tasks keep their scoring: one required fragment, present or not."""
    assert _bench().answer_has("the value is 8", "8")
    assert not _bench().answer_has("the value is 9", "8")


def test_answer_has_a_list_requires_every_fragment():
    """THE LONG TASKS' SHAPE. A count AND a file name AND a line count: a model that reports the
    count alone has not answered the question, and scoring it as a success would publish a rate
    that means nothing."""
    required = ["16", "relay_fleet.py", "10851"]
    assert _bench().answer_has("16 files; the longest is relay_fleet.py with 10851 lines", required)
    assert not _bench().answer_has("16 files contain it", required)
    assert not _bench().answer_has("relay_fleet.py has 10851 lines", required)


def test_answer_has_no_answer_at_all_is_not_a_match():
    """An ask that never returned (`answer` is None) fails every requirement rather than raising --
    a failed ask is a row, not the end of the run."""
    assert not _bench().answer_has(None, "8")
    assert not _bench().answer_has(None, ["8"])


# -- the groups ------------------------------------------------------------------------------------

def test_make_tasks_defaults_to_the_five_and_group_long_is_the_three(tmp_path):
    """`--group` picks the list, and the default is unchanged: a run with no flag benchmarks the
    same five tasks it always did, so an old success rate and a new one are rates of the same
    thing."""
    tree = _tree(tmp_path)
    short = [t[0] for t in _bench().make_tasks(tree)]
    long_ = [t[0] for t in _bench().make_tasks(tree, group="long")]
    assert short == ["glob_count", "const_value", "def_count", "arithmetic", "read_line"]
    assert long_ == ["text_files", "largest_py", "line_sum"]
    assert [t[0] for t in _bench().make_tasks(tree, group="all")] == short + long_


def test_every_long_task_has_a_prompt_and_a_computable_expectation(tmp_path):
    """Each long task is a prompt plus a ground truth the tree can actually produce, checked on the
    synthetic tree the tests above count by hand."""
    for task_id, prompt, expected_fn in _bench().make_tasks(_long_tree(tmp_path), group="long"):
        assert prompt.strip(), task_id
        assert expected_fn(), task_id


def test_make_tasks_refuses_a_group_it_does_not_have(tmp_path):
    """A typo must not silently benchmark the default group: that would publish a success rate for
    a set of tasks nobody asked for."""
    with pytest.raises(ValueError):
        _bench().make_tasks(_tree(tmp_path), group="medium")


# -- harness errors, zero-call asks, and the redacted failure strings -----------------------------

def test_summarize_excludes_harness_errors_from_the_success_rate():
    """A harness-error row (ground truth that could not be computed) must leave the rate ABOUT the
    model alone: two scorable asks, one of them failed, is 50% -- the harness row is neither
    numerator nor denominator, and is counted on its own instead."""
    rows = [_row("glob_count", True, 1.0, 1, 2),
            _row("glob_count", False, 2.0, 1, 2, error="TimeoutError"),
            _row("line_sum", False, 0.0, 0, 0, error="ValueError",
                 harness_error=True, error_msg="line_sum: pattern matches 0 file(s)")]
    s = _bench().summarize(rows)
    assert s["asks"] == 3
    assert s["harness_errors"] == 1
    assert s["success_rate"] == pytest.approx(0.5)      # 1 of the 2 scorable, not 1 of 3
    assert s["tasks"]["line_sum"]["harness_errors"] == 1
    assert s["tasks"]["line_sum"]["success_rate"] == 0.0


def test_summarize_counts_asks_that_made_no_tool_call():
    """THE BRIDGE-FAILURE CLASS. An ask that answered the file question without ever calling a tool
    answered from memory, not over the bridge; the summary carries that count both overall and per
    task. A harness-error row (no model ask at all) is not counted among them."""
    rows = [_row("read_line", True, 1.0, 1, 0),        # answered with no tool call
            _row("read_line", True, 1.0, 2, 3),        # used the bridge
            _row("glob_count", False, 2.0, 1, 0, error="TimeoutError"),   # zero-call failure
            _row("line_sum", False, 0.0, 0, 0, harness_error=True)]       # not a model ask
    s = _bench().summarize(rows)
    assert s["tool_calls_zero"] == 2
    assert s["tasks"]["read_line"]["tool_calls_zero"] == 1
    assert s["tasks"]["glob_count"]["tool_calls_zero"] == 1
    assert s["tasks"]["line_sum"]["tool_calls_zero"] == 0


def test_format_summary_names_harness_errors_and_zero_call_asks():
    """The two counts the operator reads next have to appear in WORDS in the printed deliverable,
    not only as JSON keys."""
    rows = [_row("read_line", True, 1.0, 1, 0),
            _row("line_sum", False, 0.0, 0, 0, harness_error=True)]
    text = _bench().format_summary(_bench().summarize(rows))
    assert "harness errors" in text
    assert "zero-call asks" in text


def test_failed_row_is_a_harness_error_naming_the_task():
    """`_failed_row` turns an uncomputable expectation into a row that is marked `harness_error`,
    keeps the exception's class and its message, and carries no answer -- so it is listed apart
    from a model failure and the operator can read which precondition failed."""
    row = _bench()._failed_row("line_sum", ValueError("line_sum: pattern matches 0 file(s)"))
    assert row["harness_error"] is True
    assert row["success"] is False
    assert row["error"] == "ValueError"
    assert "line_sum" in row["error_msg"]
    assert row["answer_head"] == ""
    assert row["tool_calls"] == 0


def test_redact_blanks_the_token_and_key_shaped_env_values():
    """The two strings a failed ask records pass through `redact` first: the live token and any
    long KEY/TOKEN/SECRET/PASSWORD env value are replaced before anything reaches a file, and a
    short placeholder value is left alone so it cannot blank unrelated text."""
    env = {"GATEWAY_API_KEY": "super-secret-value-here", "SHELL": "/bin/sh", "SHORT_TOKEN": "abc"}
    out = _bench().redact("tok=live-token-xyz key=super-secret-value-here keep=abc",
                          token="live-token-xyz", env=env)
    assert "live-token-xyz" not in out
    assert "super-secret-value-here" not in out
    assert "keep=abc" in out            # the short value is not treated as a secret
    assert out.count("[REDACTED]") == 2


def test_redact_of_nothing_is_an_empty_string_not_none():
    """An empty or None text comes back as "", never None, so a row always has a string where one
    is expected."""
    assert _bench().redact(None) == ""
    assert _bench().redact("") == ""


def test_format_table_prints_the_redacted_message_and_answer_under_a_failure():
    """A failed ask's detail lines carry the recorded message and the first of the answer, so one
    failure reads differently from another without opening the jsonl."""
    rows = [_row("text_files", False, 2.0, 2, 1, error="ValueError",
                 error_msg="text_files: no .py file contains it",
                 answer_head="I could not find any such file.")]
    text = _bench().format_table(rows)
    assert "text_files: no .py file contains it" in text
    assert "I could not find any such file." in text


def test_format_table_marks_a_harness_error_as_harn():
    """A harness-error row is marked HARN, not FAIL: it is the benchmark's fault, not the model's,
    and the mark has to say so."""
    rows = [_row("line_sum", False, 0.0, 0, 0, error="ValueError",
                 harness_error=True, error_msg="line_sum: pattern matches 0 file(s)")]
    lines = _bench().format_table(rows).splitlines()
    assert "HARN" in lines[1]
    assert "line_sum: pattern matches 0 file(s)" in text_of(lines)


def text_of(lines):
    """The joined table body, for a membership check that does not care which line a string is on."""
    return "\n".join(lines)


# -- harness errors and zero-call asks: the two counts the operator reads next --------------------

def test_summarize_excludes_a_harness_error_from_the_success_rate():
    """A harness error is a benchmark bug, not a model failure: it is kept OUT of both halves of the
    success rate, so one broken ground truth cannot pull a true 100% down to 50%. It is still
    counted on its own, and still an ask."""
    rows = [_row("glob_count", True, 1.0, 1, 2),
            _row("line_sum", False, 0.0, 0, 0, error="ValueError",
                 harness_error=True, error_msg="line_sum: pattern matches 0 file(s)")]
    s = _bench().summarize(rows)
    assert s["asks"] == 2
    assert s["success_rate"] == pytest.approx(1.0)       # the one scorable ask succeeded
    assert s["harness_errors"] == 1
    per = s["tasks"]["line_sum"]
    assert per["harness_errors"] == 1
    assert per["success_rate"] == 0.0                    # no scorable ask of this task


def test_summarize_of_only_harness_errors_is_zero_not_a_division():
    """A run that could compute no ground truth at all reads 0.0 rather than raising: the whole
    scorable set is empty, and an empty set is 0.0 by the same rule the empty run follows."""
    rows = [_row("line_sum", False, 0.0, 0, 0, error="ValueError", harness_error=True),
            _row("glob_count", False, 0.0, 0, 0, error="ValueError", harness_error=True)]
    s = _bench().summarize(rows)
    assert s["success_rate"] == 0.0
    assert s["harness_errors"] == 2


def test_summarize_counts_zero_call_asks_among_the_scorable():
    """An ask that answered a file question without ever calling a tool did not exercise the bridge,
    so it is counted: a green run of zero-call asks is measuring the model's guesswork, not the
    socket. A harness-error row -- which never ran -- is not a zero-call ask."""
    rows = [_row("glob_count", True, 1.0, 1, 0),                       # scored, no tool call
            _row("glob_count", True, 2.0, 1, 3),                       # scored, used the bridge
            _row("line_sum", False, 0.0, 0, 0, harness_error=True)]    # never ran: not counted
    s = _bench().summarize(rows)
    assert s["tool_calls_zero"] == 1
    assert s["tasks"]["glob_count"]["tool_calls_zero"] == 1


def test_format_summary_names_harness_errors_and_zero_call_asks():
    """The two counts are part of the printed deliverable, in words, so an operator reading the
    summary -- not the JSON -- sees how many asks never ran and how many never touched the bridge."""
    rows = [_row("glob_count", True, 1.0, 1, 0),
            _row("line_sum", False, 0.0, 0, 0, error="ValueError", harness_error=True)]
    text = _bench().format_summary(_bench().summarize(rows))
    assert "harness errors" in text
    assert "zero-call asks" in text


def test_format_table_marks_a_harness_error_and_prints_its_message():
    """A harness-error row is marked HARN -- not FAIL -- because it is the benchmark's fault, and the
    message that says WHICH precondition failed is printed under it so the operator need not open
    the jsonl to tell one harness error from another."""
    rows = [_row("line_sum", False, 0.0, 0, 0, error="ValueError", harness_error=True,
                 error_msg="line_sum: pattern relay/test_socket_*.py matches 0 file(s)")]
    text = _bench().format_table(rows)
    assert "HARN" in text
    assert "FAIL" not in text
    assert "matches 0 file(s)" in text


def test_format_table_prints_the_answer_head_under_a_model_failure():
    """A model failure that did return something prints the first of that answer under its row, so a
    wrong-but-present answer reads differently from an ask that never answered at all."""
    rows = [_row("read_line", False, 3.0, 2, 1, error="",
                 answer_head="I think the first line is something else")]
    lines = _bench().format_table(rows).splitlines()
    assert any("answer:" in ln and "something else" in ln for ln in lines)
    assert any("FAIL" in ln for ln in lines)


def test_make_tasks_long_includes_line_sum_over_the_socket_glob(tmp_path):
    """THE PROMPT AND THE EXPECTATION NAME THE SAME GLOB. `line_sum`'s prompt asks about
    `relay/test_socket_*.py`, and its expectation sums exactly those files; a prompt that named a
    different glob than the scorer would blame the model for answering the question it was asked."""
    tasks = {t[0]: t for t in _bench().make_tasks(_long_tree(tmp_path), group="long")}
    _id, prompt, expected_fn = tasks["line_sum"]
    assert "relay/test_socket_*.py" in prompt
    assert expected_fn() == ["5"]


# -- the harness-error accounting, the zero-call count, and redaction ------------------------------

def _hrow(task, exc_name="ValueError", msg="boom"):
    """A harness-error row as `_failed_row` returns it: the ground truth could not be computed, so
    it is NOT a model failure and must stay out of the scored denominator."""
    return {"task": task, "success": False, "harness_error": True, "seconds": 0.0, "turns": 0,
            "last_result": "", "tool_calls": 0, "error": exc_name, "error_msg": msg,
            "answer_head": ""}


def test_failed_row_is_a_harness_error_carrying_the_exception_name(tmp_path):
    """`_failed_row` turns an uncomputable ground truth into a row flagged `harness_error` and
    named by the exception -- a crash here would end the run, and a plain failure would blame the
    model for the bench's own unreadable tree."""
    row = _bench()._failed_row("line_sum", ValueError("no match"))
    assert row["harness_error"] is True
    assert row["success"] is False
    assert row["error"] == "ValueError"
    assert "no match" in row["error_msg"]


def test_rate_excludes_harness_errors_from_the_denominator():
    """THE WHOLE POINT OF (2). One scored pass and one harness error read as 100%, not 50%: the
    broken ground truth never reached the model, so it is not a model failure."""
    rows = [_row("glob_count", True, 1.0, 1, 2), _hrow("line_sum")]
    s = _bench().summarize(rows)
    assert s["success_rate"] == pytest.approx(1.0)
    assert s["harness_errors"] == 1
    assert s["asks"] == 2                         # still counted as an ask that happened


def test_rate_of_all_harness_errors_is_zero_not_a_division():
    """A run that is nothing but harness errors has no scorable asks; the rate reads 0.0 rather
    than raising ZeroDivisionError."""
    s = _bench().summarize([_hrow("line_sum"), _hrow("largest_py")])
    assert s["success_rate"] == 0.0
    assert s["harness_errors"] == 2


def test_tool_calls_zero_counts_only_scorable_asks_with_no_call():
    """(4). The count of asks that answered without ever calling a tool -- the bridge was not
    exercised -- over the SCORABLE asks only, so a harness error (0 calls by construction) is not
    counted among them."""
    rows = [_row("glob_count", True, 1.0, 1, 0),   # answered with no tool call
            _row("glob_count", True, 1.0, 1, 3),   # used the bridge
            _hrow("line_sum")]                      # harness error, not a zero-call model ask
    s = _bench().summarize(rows)
    assert s["tool_calls_zero"] == 1


def test_format_table_marks_a_harness_error_HARN_and_prints_its_message():
    """A harness error is marked HARN, not FAIL, and its redacted message appears on the detail
    line so the operator reads WHICH precondition failed."""
    rows = [_hrow("line_sum", exc_name="ValueError", msg="glob matched nothing")]
    text = _bench().format_table(rows)
    assert "HARN" in text
    assert "glob matched nothing" in text


def test_format_table_prints_the_redacted_answer_head_under_a_failure():
    """A scored failure (not harness) keeps a detail line too, carrying the first of the answer so
    one wrong answer can be told from another without opening the jsonl."""
    row = _row("glob_count", False, 1.0, 1, 2, error="")
    row["answer_head"] = "the answer was seventeen"
    row["error_msg"] = ""
    text = _bench().format_table([row])
    assert "FAIL" in text
    assert "seventeen" in text


def test_format_summary_names_harness_errors_and_zero_call_asks():
    """The printed deliverable surfaces (2) and (4) in WORDS, not only as JSON keys."""
    rows = [_row("glob_count", True, 1.0, 1, 0), _hrow("line_sum")]
    text = _bench().format_summary(_bench().summarize(rows))
    assert "harness errors" in text
    assert "zero-call asks" in text


def test_redact_blanks_the_token_and_a_key_shaped_env_value():
    """The two recorded strings pass through `redact` first: the live token and a KEY/TOKEN/SECRET/
    PASSWORD-named env value (8+ chars) are both replaced, so neither reaches the row."""
    secret_env = {"MCP_UNLOCK_PASSWORD": "hunter2hunter2"}
    out = _bench().redact("tok=ABCDEFGH pw=hunter2hunter2 done", token="ABCDEFGH", env=secret_env)
    assert "ABCDEFGH" not in out
    assert "hunter2hunter2" not in out
    assert "[REDACTED]" in out


def test_redact_of_none_is_an_empty_string_not_none():
    """A row always has a string where a message is expected; `redact(None)` is "", never None."""
    assert _bench().redact(None, token="", env={}) == ""
