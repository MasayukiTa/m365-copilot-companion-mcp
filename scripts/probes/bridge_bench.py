# -*- coding: utf-8 -*-
"""A live, read-only benchmark of the fenced-block bridge, over a fixed list of tasks.

WHAT IT MEASURES. Questions about a checkout, asked through the tool-calling bridge: the model is
told it may call one gateway, it asks THIS side to run a tool, and the answer it finally gives is
scored against a number computed here, locally, before anything was sent. So a "success" is the
model's answer agreeing with this repository rather than with itself. The five SHORT tasks are one
lookup each; the three LONG ones (`--group long`) need several tool calls and are scored on every
part of a several-part answer.

IT SENDS REAL TURNS, UP TO `--parallel` AT ONCE. Each ask is a new conversation against a real
agent through a real socket: one user turn, plus whatever tool-result follow-up turns the model
asks for. That is the point -- the tool loop runs inside `Conversation.ask` and never surfaces, so
the only way to know whether the bridge works end to end is to run it. Every task is read-only
(count files, read a constant, count `def ` lines, sum squares, quote a first line); nothing here
writes to the tree it reads.

NOT RUN BY CI. It needs a signed-in Edge on the debug port and a live local gateway, so it is an
operator's tool, not a test. The offline half -- the expected answers and the summary arithmetic
-- is pinned by `relay/test_bridge_bench_selfcheck.py`, which needs neither a network nor a model.

A FAILED ASK HAS TO BE READABLE. The first live `--group long` run could not be told apart one
failure from another, because a row kept only the exception's CLASS. So a failed ask now also
records `error_msg` (the message, truncated) and `answer_head` (the first 300 characters of the
answer, if there was one), and the table prints both under the failing row. Nothing that reaches a
file carries a credential: both strings go through `redact`, which replaces the live token and any
key-shaped value from the environment before they are written.

AN ASK THAT CANNOT BE SCORED IS NOT A MODEL FAILURE. Each task's expected answer is a pure
function of the tree, and each one RAISES -- naming its task -- when its precondition fails (the
file is missing, the constant is not assigned, the glob matches fewer than `MIN_MATCHES` files).
The harness turns that into a `harness_error` row: it is excluded from the success-rate denominator
and listed separately, because a broken ground truth scored as a model failure is a published
number about the benchmark's own bug.

usage: python bridge_bench.py <agent_url_file> <donor_template_json> [cdp_url]
                              [--repeat N] [--tree DIR] [--out FILE]
                              [--group short|long|all] [--parallel N]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from relay import profile_token as PT          # noqa: E402
from relay import template_seed as TS          # noqa: E402
from relay import temp_home as TH              # noqa: E402
from relay.chathub import Conversation         # noqa: E402  (stdlib-only at import)

#: This checkout is a sibling worktree of the primary clone (`<name>-wt-<suffix>`), and the bench
#: reads the PRIMARY one. The name is DERIVED from this file's own path, never written down:
#: scripts/check_no_identifying_names.py fails CI on a home-shaped path or an id-shaped token in
#: any tracked file, and the absolute path a reader would expect carries both.
_WORKTREE_SUFFIX = re.compile(r"-wt-[^-]+$")


def default_tree():
    """The checkout the tasks read when `--tree` is not given: `BRIDGE_BENCH_TREE`, else the
    primary clone beside this worktree (the same basename without its `-wt-...` suffix), else
    this checkout itself."""
    env = os.environ.get("BRIDGE_BENCH_TREE", "").strip()
    if env:
        return env
    primary = _WORKTREE_SUFFIX.sub("", os.path.basename(ROOT))
    beside = os.path.join(os.path.dirname(ROOT), primary)
    if primary != os.path.basename(ROOT) and os.path.isdir(beside):
        return beside
    return ROOT


# -- expected answers: computed here, before the model is asked ---------------------------------
#
# These five are the whole ground truth of the benchmark. They are pure functions of the tree and
# the filesystem, so the selfcheck test can pin them without a network, and a bug in one of them
# would otherwise be reported as a bug in the model.

#: The smallest number of files a glob-based task may match. A prompt that says "add up the line
#: counts of ALL files matching <glob>" over a glob matching one or two files is a single lookup
#: wearing a multi-file prompt, so a pattern that finds fewer than this is a broken GROUND TRUTH:
#: every expected_fn built on one raises -- naming its task -- instead of scoring a model against
#: it. The default patterns match 266 and 8 files in the primary checkout, so they clear this.
MIN_MATCHES = 3


def _task_msg(task_id, text) -> str:
    """The message form every precondition failure uses. It NAMES THE TASK, because the operator's
    next question about a harness error is "which task was it?"."""
    return ("%s: %s" % (task_id, text)) if task_id else text


def _read_text(tree, rel, task_id="") -> str:
    """One file's text under `tree`. utf-8-sig so a byte-order mark, if one is ever committed,
    is not silently part of the first line the `read_line` task asks about.

    A file that is not there raises, NAMING THE TASK: a task whose ground truth cannot be read must
    not score zero as though the model had got it wrong, and the message has to say which task."""
    try:
        with open(os.path.join(tree, rel), encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        raise ValueError(_task_msg(task_id, "cannot read %s under the tree: %s" % (rel, exc)))


def _glob(tree, pattern) -> list:
    """Every path `pattern` matches under `tree`, sorted.

    The pattern is split on "/" and rejoined with the platform's separator, so a Windows tree
    matches the same files a POSIX one would."""
    return sorted(glob.glob(os.path.join(tree, *pattern.split("/"))))


def _require_matches(task_id, pattern, matches) -> list:
    """The glob precondition, checked before anything is scored: a pattern must find MIN_MATCHES
    files, or the task is not the multi-file question its prompt claims to ask."""
    if len(matches) < MIN_MATCHES:
        raise ValueError(_task_msg(
            task_id, "pattern %s matches %d file(s) under the tree, need at least %d"
                     % (pattern, len(matches), MIN_MATCHES)))
    return matches


def expected_glob_count(tree, pattern="relay/test_*.py", task_id="glob_count") -> str:
    """How many files the task's glob matches under `tree`, as the string the answer must carry.

    Raises, naming the task, unless the glob matches at least MIN_MATCHES files: a glob that finds
    none (or one) is a broken expectation, and scoring a model against it would publish the
    benchmark's own bug as the model's failure."""
    return str(len(_require_matches(task_id, pattern, _glob(tree, pattern))))


def expected_const_value(tree, name="MAX_CALLS_PER_REPLY", rel="relay/socket_tools.py",
                         task_id="const_value") -> str:
    """The value assigned to `name` in `rel`, as written.

    ANCHORED to the start of a line, so a comment or docstring that merely MENTIONS the constant
    is not mistaken for its assignment. Raises rather than returning something empty: a task whose
    ground truth cannot be computed must not score zero as though the model had got it wrong.
    """
    m = re.search(r"^\s*" + re.escape(name) + r"\s*=\s*(\S+)",
                  _read_text(tree, rel, task_id), re.M)
    if not m:
        raise ValueError(_task_msg(task_id, "%s is not assigned in %s" % (name, rel)))
    return m.group(1)


def expected_def_count(tree, rel="relay/socket_tools.py", task_id="def_count") -> str:
    """How many lines of `rel` begin with `def ` -- the question the `def_count` task asks."""
    text = _read_text(tree, rel, task_id)
    return str(sum(1 for line in text.splitlines() if line.startswith("def ")))


def expected_first_line(tree, rel="relay/socket_tools.py", task_id="read_line") -> str:
    """The first line of `rel`, stripped -- the answer `read_line` looks for."""
    return _read_text(tree, rel, task_id).splitlines()[0].strip()


def sum_of_squares(n=100) -> str:
    """The sum of the squares of 1..n (338350 for n=100), as the string the answer must carry.

    Computed here so the arithmetic task's expectation is a LOCAL fact rather than a number typed
    twice -- if this and the model ever disagree, one of them is wrong and the benchmark says so.
    """
    return str(sum(i * i for i in range(1, n + 1)))


# -- the long tasks' expectations -----------------------------------------------------------------
#
# THREE MORE GROUND TRUTHS, EACH A SEVERAL-PART ANSWER. The five above are one lookup each and one
# short string to score; these ask for a count AND a name AND a line count, so the scorer cannot
# accept a lucky half of the reply. All three stay pure functions of `--tree`, which is what keeps
# them checkable in the selfcheck test instead of only in a live run.

def _line_count(tree, rel) -> int:
    """How many lines `rel` (under `tree`) has, read the same way `expected_def_count` reads it."""
    return len(_read_text(tree, rel).splitlines())


def _py_files(tree, subdir="relay") -> list:
    """The .py files DIRECTLY under `subdir`, as tree-relative paths, sorted.

    NOT RECURSIVE, and the tasks say so too: "directly in relay/" is a different question from
    "anywhere under relay/", and a walk that descended would answer the wrong one -- the answer
    would change the day somebody adds a subdirectory rather than the day the code changes.
    """
    pattern = os.path.join(tree, *subdir.split("/"), "*.py")
    return sorted(os.path.relpath(p, tree).replace("\\", "/") for p in glob.glob(pattern))


def files_containing(tree, needle="Conversation", subdir="relay") -> list:
    """(relative path, line count) for every .py file directly under `subdir` carrying `needle`.

    Read through `_read_text`, so a byte-order mark cannot decide whether a file "contains" the
    string -- the same tolerance the live task's tools are expected to have."""
    out = []
    for rel in _py_files(tree, subdir):
        if needle in _read_text(tree, rel):
            out.append((rel, _line_count(tree, rel)))
    return out


def expected_files_with_text(tree, needle="Conversation", subdir="relay",
                             task_id="text_files") -> list:
    """How many files carry `needle`, and the longest of them, as [count, name, lines].

    THE TIE IS BROKEN BY NAME, so the expectation is one value for a given tree rather than
    whichever file the filesystem happened to list first; a tree where two files tie for longest
    would make the model's answer ambiguous, and the checkouts this runs against do not.
    """
    hits = files_containing(tree, needle, subdir)
    if not hits:
        raise ValueError(_task_msg(
            task_id, "no .py file directly under %s contains %r" % (subdir, needle)))
    rel, lines = max(hits, key=lambda item: (item[1], item[0]))
    return [str(len(hits)), os.path.basename(rel), str(lines)]


def expected_largest_py(tree, subdir="relay", count=3, task_id="largest_py") -> list:
    """The `count` .py files with the most lines directly under `subdir`, as a flat
    [name, lines, name, lines, ...] in descending line order (the name decides a tie).

    ALL THREE ARE REQUIRED. A model that names the files but not their line counts, or gives the
    counts for the wrong files, has not answered the question, and a scorer checking only the names
    would call that a success."""
    ranked = sorted(((_line_count(tree, rel), rel) for rel in _py_files(tree, subdir)),
                    key=lambda item: (-item[0], item[1]))
    if len(ranked) < count:
        raise ValueError(_task_msg(
            task_id, "only %d .py file(s) directly under %s, need %d" % (len(ranked), subdir, count)))
    return [part for lines, rel in ranked[:count] for part in (os.path.basename(rel), str(lines))]


def expected_line_sum(tree, pattern="relay/test_socket_*.py", task_id="line_sum") -> list:
    """The total line count of the files the glob matches, as one required fragment.

    ONE NUMBER, BUT NOT ONE LOOKUP: earning it means reading every matched file and adding them,
    which is the reason the task is here at all. The pattern is one the DEFAULT tree really has
    -- `relay/test_tool_*.py` matched nothing there, which is how a live run raised before its
    first ask -- and it must clear `MIN_MATCHES` or this raises, naming the task."""
    matches = _require_matches(task_id, pattern, _glob(tree, pattern))
    return [str(sum(_line_count(tree, os.path.relpath(p, tree)) for p in matches))]


#: What `--group` selects between: the five single-lookup asks, the three that take several tool
#: calls, or both in one pass.
GROUPS = ("short", "long", "all")


def _short_tasks(tree):
    """The original five: one lookup each, each scored on a single short string."""
    return [
        ("glob_count",
         "How many files match relay/test_*.py ? Use the tools.",
         lambda: expected_glob_count(tree)),
        ("const_value",
         "In relay/socket_tools.py what is the value of MAX_CALLS_PER_REPLY? Use the tools.",
         lambda: expected_const_value(tree)),
        ("def_count",
         "How many lines in relay/socket_tools.py start with 'def ' ? Use run_python or grep.",
         lambda: expected_def_count(tree)),
        ("arithmetic",
         "Use run_python to compute the sum of squares of 1..100 and give the number.",
         lambda: sum_of_squares()),
        ("read_line",
         "What is the first line of relay/socket_tools.py ?",
         lambda: expected_first_line(tree)),
    ]


def _long_tasks(tree):
    """The longer three: each needs several tool calls, and each answer has more than one required
    part.

    The point of the group is the TOOL LOOP, not the question: one lookup proves the bridge carries
    a single call, and these prove it carries a run of them -- list, read, count, and only then
    answer. Each is written so the tools are the only way to answer it and the answer is a fact
    about the given tree, so the same local expectation still decides success. The prompts name the
    steps, because a task that can be answered from memory measures the model, not the bridge.
    """
    return [
        ("text_files",
         "In relay/, look only at the .py files directly in that directory, not in any "
         "subdirectory. How many of those files contain the text 'Conversation', and which one of "
         "them has the most lines? Use the tools for each step, then report the count, the file "
         "name, and that file's line count.",
         lambda: expected_files_with_text(tree)),
        ("largest_py",
         "Using the tools, get the line count of every .py file directly in relay/, then report "
         "the 3 files with the most lines, each with its line count.",
         lambda: expected_largest_py(tree)),
        ("line_sum",
         "Using the tools, add up the line counts of all files matching relay/test_socket_*.py and "
         "report the total.",
         lambda: expected_line_sum(tree)),
    ]


def make_tasks(tree, group="short"):
    """The task list for one group. `short` (the default) is the original five, `long` the three
    that take several tool calls, `all` both.

    An unknown group RAISES rather than quietly falling back to `short`: a typo on the command line
    that silently benchmarked the wrong tasks would publish a success rate for a group nobody
    asked for.
    """
    if group == "short":
        return _short_tasks(tree)
    if group == "long":
        return _long_tasks(tree)
    if group == "all":
        return _short_tasks(tree) + _long_tasks(tree)
    raise ValueError("unknown task group %r (expected one of %s)" % (group, ", ".join(GROUPS)))


# -- keeping credentials out of the rows ---------------------------------------------------------

def _key_shaped_values(env):
    """The values in `env` that look like a secret worth hiding: anything assigned to a name with
    KEY, TOKEN, SECRET or PASSWORD in it, kept only when it is long enough to be one (8+ chars) so
    an empty or placeholder value does not blank out unrelated text."""
    marks = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    out = []
    for name, value in (env or {}).items():
        if value and len(value) >= 8 and any(m in name.upper() for m in marks):
            out.append(value)
    return out


def redact(text, *, token="", env=None):
    """`text` with the live token and any key-shaped environment value replaced by a marker.

    The two strings a failed ask records -- the exception message and the first of the answer --
    are the two places the model's own words (or an echoed credential) could reach a file, so both
    go through here before they are written. The token is blanked first, then the longest
    env-values first, so a value that contains another is not half-replaced. An empty or None text
    comes back as an empty string, never None, so a row always has a string where one is expected.
    """
    s = text or ""
    if not s:
        return ""
    secrets = [token] if token else []
    secrets.extend(_key_shaped_values(env if env is not None else os.environ))
    for secret in sorted((x for x in secrets if x), key=len, reverse=True):
        s = s.replace(secret, "[REDACTED]")
    return s


# -- one ask -------------------------------------------------------------------------------------

#: A comma (ASCII ',' or full-width '，' U+FF0C) that sits BETWEEN two digits -- a thousands
#: separator. Only these are stripped; a comma with a non-digit on either side is left alone.
_THOUSANDS_SEP = re.compile(r"(?<=\d)[,\uff0c](?=\d)")


def normalise_for_match(text) -> str:
    """The ANSWER text with thousands-separator commas removed, used ONLY for matching.

    A model writes '10,824 行'; the expected fragment is '10824'. Removing a comma that sits
    between two digits lets the correct answer match without loosening anything else: commas that
    are not between digits are untouched, and the original text is kept for answer_head.
    """
    return _THOUSANDS_SEP.sub("", text or "")


def _part_in(part, text) -> bool:
    """Whether one required fragment appears in the (already normalised) answer text.

    A fragment that is a whole number must match on digit boundaries, so '431' does not match
    inside '3431' and '10824' does not match inside '110824'. Non-numeric fragments keep the
    plain substring test.
    """
    part = str(part)
    if part.isdigit():
        return re.search(r"(?<!\d)" + re.escape(part) + r"(?!\d)", text) is not None
    return part in text


def answer_has(answer, expected) -> bool:
    """True when the answer carries everything the task required.

    `expected` is a string (one required fragment -- what every short task produces) or an iterable
    of fragments (the long tasks, whose answer has several parts). EVERY fragment must appear: a
    model that reports the count but not the file has not answered the question, and a scorer that
    accepted either half would print a success rate about nothing.

    The answer is normalised first (thousands-separator commas removed) so '10,824 行' carries
    '10824'; numeric fragments then match on digit boundaries so '3,431' does not satisfy '431'.
    """
    text = normalise_for_match(answer or "")
    if isinstance(expected, str):
        return _part_in(expected, text)
    return all(_part_in(part, text) for part in expected)


def run_one(task_id, prompt, expected, *, holder, template, catalogue, run_tool, connect,
            redact_env=None):
    """One ask in a NEW conversation. Records what it cost and whether the expected string is in
    the answer. NEVER raises: one failure must not end the run.

    A new conversation per ask is deliberate -- the bridge keeps continuity in the conversation id
    rather than in the wire, so a shared one would let ask 3 answer from ask 1's context and score
    a memory the benchmark is not trying to measure. That also makes an ask safe to run on a worker
    thread of its own: nothing here is shared with another ask but the ready pieces, which are
    read-only.

    THE RECORDED STRINGS ARE REDACTED FIRST. A failed ask keeps `error_msg` (the exception's
    message) and `answer_head` (the first 300 characters of the answer, when there was one), so a
    reader can tell one failure from another; both pass through `redact` before they are returned,
    which blanks the live token and any key-shaped environment value so nothing reaching a file
    carries a credential the model happened to echo back.
    """
    conv = Conversation(lambda: holder["t"], template=template)
    calls = []

    def counted(name, args):
        calls.append(name)
        return run_tool(name, args)

    error = ""
    error_text = ""
    answer = ""
    t0 = time.time()
    try:
        answer = conv.ask(prompt, connect=connect, run_tool=counted, catalogue=catalogue)
    except Exception as exc:                       # report it in the row; do not re-raise
        error = type(exc).__name__
        error_text = str(exc)[:300]
    dt = time.time() - t0
    tok = holder.get("t") if hasattr(holder, "get") else None
    tok = tok if isinstance(tok, str) else ""
    err_msg = redact(error_text, token=tok, env=redact_env) if error else ""
    return {"task": task_id,
            "success": answer_has(answer, expected),
            "harness_error": False,
            "seconds": round(dt, 3),
            "turns": int(conv.turns),
            "last_result": str(conv.last_result),
            "tool_calls": len(calls),
            "error": error,
            "error_msg": err_msg,
            "answer_head": redact((answer or "")[:300], token=tok, env=redact_env)}


# -- the summary: pure, and the half the selfcheck test pins -------------------------------------

def _median(values) -> float:
    """Median of the list, or 0.0 for an empty one: a run that measured nothing is not a
    division by zero."""
    return float(statistics.median(values)) if values else 0.0


def _mean(values) -> float:
    """Mean of the list, or 0.0 for an empty one, for the same reason as `_median`."""
    return float(sum(values)) / len(values) if values else 0.0


def _scored(rows) -> list:
    """The rows the model was actually scored on: every row that is not a harness error. A broken
    ground truth never reached the model, so it does not belong in a rate ABOUT the model."""
    return [r for r in rows if not r.get("harness_error")]


def _rate(rows) -> float:
    """The fraction of the SCORABLE rows whose answer carried the expected string; 0.0 for none.

    Harness-error rows -- a ground truth that could not be computed -- are excluded from both the
    numerator and the denominator: scoring them as model failures would publish the benchmark's own
    bug as a number about the model, and a run that is ALL harness errors reads 0.0 rather than
    raising."""
    scorable = _scored(rows)
    return (sum(1 for r in scorable if r.get("success")) / float(len(scorable))) if scorable else 0.0


def _count_harness(rows) -> int:
    """How many of these rows are harness errors -- a ground truth that could not be computed."""
    return sum(1 for r in rows if r.get("harness_error"))


def _count_zero_calls(rows) -> int:
    """How many SCORABLE asks made no tool call at all. An ask that answered the file question
    without ever reading a file did not use the bridge, so this is the count that says whether the
    bridge was exercised -- a green run of zero-call asks is measuring the model's guesswork."""
    return sum(1 for r in _scored(rows) if not int(r.get("tool_calls") or 0))


def _seconds(row) -> float:
    """The row's wall time as a float. A row that never got that far counts as 0, not as missing."""
    return float(row.get("seconds") or 0.0)


def asks_per_minute(asks, wall_s) -> float:
    """The run's throughput: `asks` over `wall_s`, in asks per minute.

    A run that measured no wall time gives 0.0 rather than raising, the same rule `_rate` follows:
    a killed benchmark that got through setup and no ask must still be summarisable, or the failure
    report is the thing that fails. With `--parallel` the rate exceeds the single-ask rate, which
    is the number this exists to show.
    """
    return (float(asks) * 60.0 / float(wall_s)) if wall_s and float(wall_s) > 0 else 0.0


def summarize(rows, wall_s=0.0) -> dict:
    """The run as the operator reads it: overall rates and medians, then the same per task.

    `wall_s` is the whole run's elapsed time, which the caller measured around the asks rather than
    summing the rows: under `--parallel` the sum of the asks is larger than the wall clock, and the
    point of running them together is that difference.

    An empty run gives zeroes rather than raising -- a killed benchmark that got through the setup
    and no asks must still be summarisable, or the failure report is the thing that fails.
    """
    groups = {}
    for row in rows:
        groups.setdefault(row.get("task", ""), []).append(row)
    per_task = {}
    for task_id, group in groups.items():
        per_task[task_id] = {
            "asks": len(group),
            "success_rate": _rate(group),
            "harness_errors": _count_harness(group),
            "tool_calls_zero": _count_zero_calls(group),
            "median_seconds": _median([_seconds(r) for r in group]),
            "mean_tool_calls": _mean([float(r.get("tool_calls") or 0) for r in group]),
        }
    return {
        "asks": len(rows),
        "success_rate": _rate(rows),
        "harness_errors": _count_harness(rows),
        "tool_calls_zero": _count_zero_calls(rows),
        "wall_s": round(float(wall_s or 0.0), 3),
        "asks_per_minute": round(asks_per_minute(len(rows), wall_s), 3),
        "median_seconds": _median([_seconds(r) for r in rows]),
        "median_turns": _median([float(r.get("turns") or 0) for r in rows]),
        "mean_tool_calls": _mean([float(r.get("tool_calls") or 0) for r in rows]),
        "tasks": per_task,
    }


def format_table(rows) -> str:
    """One fixed-width line per ask: task, run index, ok/FAIL/HARN, seconds, turns, tool calls,
    error. A failing or harness-error row is followed by an indented detail line carrying the
    redacted message and the first of the answer, so one failure can be told from another without
    opening the jsonl. A harness error is marked HARN, because it is the benchmark's fault, not the
    model's."""
    header = "%-10s %4s %5s %9s %6s %6s  %s" % (
        "task", "run", "ok", "seconds", "turns", "calls", "error")
    lines = [header]
    for i, row in enumerate(rows, 1):
        if row.get("harness_error"):
            mark = "HARN"
        elif row.get("success"):
            mark = "ok"
        else:
            mark = "FAIL"
        lines.append("%-10s %4d %5s %9.3f %6d %6d  %s" % (
            row.get("task", ""), i, mark,
            _seconds(row), int(row.get("turns") or 0), int(row.get("tool_calls") or 0),
            row.get("error") or ""))
        if mark != "ok":
            detail = (row.get("error_msg") or "").strip()
            head = (row.get("answer_head") or "").strip()
            if detail:
                lines.append("           msg: " + detail.replace("\n", " "))
            if head:
                lines.append("           answer: " + head.replace("\n", " "))
    return "\n".join(lines)


def format_summary(summary) -> str:
    """The overall line first (success rate, wall time, throughput, medians, and the two counts the
    operator reads next -- harness errors and zero-call asks), then each task's."""
    lines = ["asks=%d  success rate %.1f%%  harness errors %d  zero-call asks %d  wall %.1fs  "
             "asks/min %.2f  median seconds %.3f  median turns %.1f  mean tool calls %.2f"
             % (summary["asks"], 100.0 * summary["success_rate"],
                int(summary.get("harness_errors") or 0), int(summary.get("tool_calls_zero") or 0),
                float(summary.get("wall_s") or 0.0),
                float(summary.get("asks_per_minute") or 0.0), summary["median_seconds"],
                summary["median_turns"], summary["mean_tool_calls"])]
    for task_id in sorted(summary["tasks"]):
        t = summary["tasks"][task_id]
        lines.append("  %-10s asks=%d  success rate %.1f%%  harness errors %d  zero-call asks %d  "
                     "median seconds %.3f  mean tool calls %.2f"
                     % (task_id, t["asks"], 100.0 * t["success_rate"],
                        int(t.get("harness_errors") or 0), int(t.get("tool_calls_zero") or 0),
                        t["median_seconds"], t["mean_tool_calls"]))
    return "\n".join(lines)


# -- running it ----------------------------------------------------------------------------------

def _parse_args(argv=None):
    """The default `--out` is computed HERE rather than as a module constant, and the reason is a
    test: relay/test_live_record_isolation.py treats an UPPERCASE module constant -- or a
    one-line-return helper -- that resolves a gitignored, state-shaped path beside its own module
    as an unclassified shared record, and fails until conftest.py classifies it. A `.jsonl` beside
    the module is exactly that shape, so it is built at call time instead.
    """
    ap = argparse.ArgumentParser(
        description="Live, read-only benchmark of the fenced-block tool bridge over a fixed "
                    "task list.")
    ap.add_argument("agent_url_file", help="file holding the agent's url, one line")
    ap.add_argument("donor_template", help="a captured request template json to seed from")
    ap.add_argument("cdp", nargs="?", default="http://127.0.0.1:9340",
                    help="the bgedge debug port (never the operator's own 9222/9223/9224)")
    ap.add_argument("--repeat", type=int, default=2, help="asks per task (default 2)")
    ap.add_argument("--tree", default=default_tree(), help="the checkout the tasks read")
    ap.add_argument("--group", choices=GROUPS, default="short",
                    help="which task group to run: short (default), long, or all")
    ap.add_argument("--parallel", type=int, default=1,
                    help="how many asks to run at the same time (default 1, one at a time)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "bench_out.jsonl"),
                    help="where the jsonl rows go (git-ignored)")
    args = ap.parse_args(argv)
    assert ":9222" not in args.cdp and ":9223" not in args.cdp and ":9224" not in args.cdp
    args.parallel = max(1, int(args.parallel or 1))   # a zero or negative is one, not none
    return args


def _setup(args, tmp, log):
    """Template and token, ONCE. Returns the pieces every ask is handed.

    The template is derived the way the single-shot probe derives it -- a COPY of a donor template,
    seeded for this agent's url -- and the token comes from the bgedge light page: one navigation,
    no message sent, and the Playwright context is left BEFORE the first ask, because the event
    loop it owns does not survive into `ask`.
    """
    url = open(args.agent_url_file, encoding="ascii", errors="replace").read().strip()
    standin = "https://donor.example.invalid/agent"
    shutil.copy(args.donor_template, PT.template_path(standin, tmp))
    TS.seed(standin, url, tmp)
    template = PT.load_template(url, tmp)
    if template is None:                 # a missing template is "no socket route", not "try anyway"
        raise RuntimeError("no template could be derived for the agent url")
    log.append("template derived, names an agent: %s" % bool(template.gpt_id))

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp)
        ctx = browser.contexts[0]
        token = PT.token_via_light_page(ctx, url, log=lambda m: log.append(m))
    holder = {"t": token}
    log.append("token obtained, life %.0f min" % (PT.token_life_s(token) / 60.0))  # never the token

    from dotenv import load_dotenv
    load_dotenv(os.environ.get("BRIDGE_ENV_FILE", os.path.join(ROOT, ".env")),
                encoding="utf-8-sig")            # the gateway key lives there; never printed
    from relay import tool_bridge as TB
    from relay.socket_route import websocket_connect
    return {"holder": holder,
            "template": template,
            "catalogue": TB.catalogue(),
            "run_tool": TB.make_run_tool("bridge_bench"),
            "connect": websocket_connect}


def _failed_row(task_id, exc):
    """A task whose expected answer cannot be computed is a ROW, not a crash. Counting it as a
    plain failure would blame the model for the benchmark's own unreadable ground truth, and
    dropping it would quietly shorten the run, so it is reported with the exception's name.

    It carries `harness_error=True`, which keeps it OUT of the success-rate denominator -- the
    model never saw this ask, so it is not a model failure -- while still appearing as its own line
    in the table and its own count in the summary. The exception's message (redacted) goes in
    `error_msg` so the operator can read WHICH precondition failed, not only that one did.
    """
    return {"task": task_id, "success": False, "harness_error": True, "seconds": 0.0, "turns": 0,
            "last_result": "", "tool_calls": 0, "error": type(exc).__name__,
            "error_msg": redact(str(exc)[:300]), "answer_head": ""}


def _plan_asks(args):
    """Every ask this run will make, as (task_id, prompt, expected) -- ground truth computed HERE,
    before a thread starts or a byte is sent.

    A task whose expectation cannot be computed still becomes `--repeat` rows carrying the
    exception, exactly as the serial path did: the model is not blamed for the benchmark's own
    unreadable tree, and the run is not quietly shortened."""
    asks = []
    for task_id, prompt, expected_fn in make_tasks(args.tree, group=args.group):
        try:
            expected = expected_fn()
        except Exception as exc:                  # unreadable ground truth: a row, not an end
            for _ in range(args.repeat):
                asks.append((task_id, prompt, exc))
            continue
        for _ in range(args.repeat):
            asks.append((task_id, prompt, expected))
    return asks


def _ask_one(planned, ready):
    """One planned ask, in whatever thread runs it: a fresh Conversation of its own, and no
    exception out of here -- a raised ask must not end a run that other asks are still measuring."""
    task_id, prompt, expected = planned
    if isinstance(expected, Exception):
        return _failed_row(task_id, expected)
    try:
        return run_one(task_id, prompt, expected,
                       holder=ready["holder"], template=ready["template"],
                       catalogue=ready["catalogue"], run_tool=ready["run_tool"],
                       connect=ready["connect"])
    except Exception as exc:                       # run_one should never get here; be sure anyway
        return _failed_row(task_id, exc)


def _ask_all(args, ready, on_row):
    """Every planned ask, up to `--parallel` at a time, handing each row to `on_row` AS IT ARRIVES.

    THREADS, AND NOT ASYNCIO, BECAUSE OF WHERE THE EVENT LOOP IS. `relay.socket_route.
    websocket_connect` starts an asyncio loop of its own per connection, so it must be called from
    a thread that owns no loop Playwright's sync API is driving. The token is taken before any
    worker starts (`_setup`), and every ask after that is exactly such a thread.

    Returns `(rows, wall_s)`, the rows in completion order. The wall clock spans the asks, so
    `--parallel 4` that finishes four asks in the time one took reports that rather than the sum of
    its parts; `on_row` is called on this thread, one row at a time, so the writer needs no lock.
    """
    planned = _plan_asks(args)
    rows = []
    t0 = time.time()
    if args.parallel <= 1:
        for item in planned:
            row = _ask_one(item, ready)
            rows.append(row)
            on_row(row)
    else:
        with ThreadPoolExecutor(max_workers=args.parallel,
                                thread_name_prefix="bridge_bench") as pool:
            futures = [pool.submit(_ask_one, item, ready) for item in planned]
            for fut in as_completed(futures):
                row = fut.result()
                rows.append(row)
                on_row(row)
    return rows, time.time() - t0


def main(argv=None) -> int:
    """Arguments, one setup, then every ask. Never aborts on a failed ask -- the run IS the
    measurement, and a benchmark that stops at the first refusal measures one refusal."""
    args = _parse_args(argv)
    # The swept home, never a bare %TEMP% directory: relay/temp_home.py is the one place this
    # repository's scratch is allowed to live, and its age sweep covers it.
    tmp = tempfile.mkdtemp(prefix="bridge_bench_", dir=TH.temp_home())
    try:
        log = []
        try:
            ready = _setup(args, tmp, log)
        except Exception as exc:                  # no template, no token: say it and stop
            print("FAILED: %s: %s" % (type(exc).__name__, str(exc)[:300]))
            return 1
        log.append("group=%s parallel=%d" % (args.group, args.parallel))
        print("\n".join(log))
        # Rows are appended and flushed AS THEY ARRIVE: a run that is killed part-way -- the
        # operator stops it, the machine goes to sleep -- still has every ask that finished, and
        # the next run appends rather than republishing. The callback runs on this one thread, so
        # `--parallel` needs no lock around the file.
        with open(args.out, "a", encoding="utf-8") as fh:
            def on_row(row):
                fh.write(json.dumps({"kind": "ask", **row}) + "\n")
                fh.flush()

            rows, wall_s = _ask_all(args, ready, on_row)
        summary = summarize(rows, wall_s)
        print(format_table(rows))
        print(format_summary(summary))
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "summary", **summary}) + "\n")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
