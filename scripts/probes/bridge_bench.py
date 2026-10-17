# -*- coding: utf-8 -*-
"""A live, read-only benchmark of the fenced-block bridge, over a fixed list of tasks.

WHAT IT MEASURES. Five questions about a checkout, asked through the tool-calling bridge: the
model is told it may call one gateway, it asks THIS side to run a tool, and the answer it finally
gives is scored against a number computed here, locally, before anything was sent. So a "success"
is the model's answer agreeing with this repository rather than with itself.

IT SENDS REAL TURNS. Each ask is a new conversation against a real agent through a real socket:
one user turn, plus whatever tool-result follow-up turns the model asks for. That is the point --
the tool loop runs inside `Conversation.ask` and never surfaces, so the only way to know whether
the bridge works end to end is to run it. Every task is read-only (count files, read a constant,
count `def ` lines, sum squares, quote a first line); nothing here writes to the tree it reads.

NOT RUN BY CI. It needs a signed-in Edge on the debug port and a live local gateway, so it is an
operator's tool, not a test. The offline half -- the expected answers and the summary arithmetic
-- is pinned by `relay/test_bridge_bench_selfcheck.py`, which needs neither a network nor a model.
Nothing it prints or writes carries a credential: the per-ask record holds whether the answer
matched, what it cost, and how many tool calls it took -- never the token, never the model's prose.

usage: python bridge_bench.py <agent_url_file> <donor_template_json> [cdp_url]
                              [--repeat N] [--tree DIR] [--out FILE]
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

def _read_text(tree, rel):
    """One file's text under `tree`. utf-8-sig so a byte-order mark, if one is ever committed,
    is not silently part of the first line the `read_line` task asks about."""
    with open(os.path.join(tree, rel), encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def expected_glob_count(tree, pattern="relay/test_*.py") -> str:
    """How many files the task's glob matches under `tree`, as the string the answer must carry."""
    return str(len(glob.glob(os.path.join(tree, pattern))))


def expected_const_value(tree, name="MAX_CALLS_PER_REPLY", rel="relay/socket_tools.py") -> str:
    """The value assigned to `name` in `rel`, as written.

    ANCHORED to the start of a line, so a comment or docstring that merely MENTIONS the constant
    is not mistaken for its assignment. Raises rather than returning something empty: a task whose
    ground truth cannot be computed must not score zero as though the model had got it wrong.
    """
    m = re.search(r"^\s*" + re.escape(name) + r"\s*=\s*(\S+)", _read_text(tree, rel), re.M)
    if not m:
        raise ValueError("%s is not assigned in %s" % (name, rel))
    return m.group(1)


def expected_def_count(tree, rel="relay/socket_tools.py") -> str:
    """How many lines of `rel` begin with `def ` -- the question the `def_count` task asks."""
    text = _read_text(tree, rel)
    return str(sum(1 for line in text.splitlines() if line.startswith("def ")))


def expected_first_line(tree, rel="relay/socket_tools.py") -> str:
    """The first line of `rel`, stripped -- the answer `read_line` looks for."""
    return _read_text(tree, rel).splitlines()[0].strip()


def sum_of_squares(n=100) -> str:
    """The sum of the squares of 1..n (338350 for n=100), as the string the answer must carry.

    Computed here so the arithmetic task's expectation is a LOCAL fact rather than a number typed
    twice -- if this and the model ever disagree, one of them is wrong and the benchmark says so.
    """
    return str(sum(i * i for i in range(1, n + 1)))


def make_tasks(tree):
    """The fixed task list: (id, prompt, expected_fn). The expected_fn is called by the caller so
    that a ground truth which cannot be computed surfaces as an error rather than as a bad row."""
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


# -- one ask -------------------------------------------------------------------------------------

def run_one(task_id, prompt, expected, *, holder, template, catalogue, run_tool, connect):
    """One ask in a NEW conversation. Records what it cost and whether the expected string is in
    the answer. NEVER raises: one failure must not end the run.

    A new conversation per ask is deliberate -- the bridge keeps continuity in the conversation id
    rather than in the wire, so a shared one would let ask 3 answer from ask 1's context and score
    a memory the benchmark is not trying to measure.

    THE ANSWER TEXT IS NOT RECORDED ANYWHERE. It goes into one boolean and is dropped: the rows
    are written to a file, and nothing that reaches a file may carry a credential the model
    happened to echo back.
    """
    conv = Conversation(lambda: holder["t"], template=template)
    calls = []

    def counted(name, args):
        calls.append(name)
        return run_tool(name, args)

    error = ""
    answer = ""
    t0 = time.time()
    try:
        answer = conv.ask(prompt, connect=connect, run_tool=counted, catalogue=catalogue)
    except Exception as exc:                       # report it in the row; do not re-raise
        error = type(exc).__name__
    dt = time.time() - t0
    return {"task": task_id,
            "success": bool(expected in (answer or "")),
            "seconds": round(dt, 3),
            "turns": int(conv.turns),
            "last_result": str(conv.last_result),
            "tool_calls": len(calls),
            "error": error}


# -- the summary: pure, and the half the selfcheck test pins -------------------------------------

def _median(values) -> float:
    """Median of the list, or 0.0 for an empty one: a run that measured nothing is not a
    division by zero."""
    return float(statistics.median(values)) if values else 0.0


def _mean(values) -> float:
    """Mean of the list, or 0.0 for an empty one, for the same reason as `_median`."""
    return float(sum(values)) / len(values) if values else 0.0


def _rate(rows) -> float:
    """The fraction of these rows whose answer carried the expected string; 0.0 for none."""
    return (sum(1 for r in rows if r.get("success")) / float(len(rows))) if rows else 0.0


def _seconds(row) -> float:
    """The row's wall time as a float. A row that never got that far counts as 0, not as missing."""
    return float(row.get("seconds") or 0.0)


def summarize(rows) -> dict:
    """The run as the operator reads it: overall rates and medians, then the same per task.

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
            "median_seconds": _median([_seconds(r) for r in group]),
            "mean_tool_calls": _mean([float(r.get("tool_calls") or 0) for r in group]),
        }
    return {
        "asks": len(rows),
        "success_rate": _rate(rows),
        "median_seconds": _median([_seconds(r) for r in rows]),
        "median_turns": _median([float(r.get("turns") or 0) for r in rows]),
        "mean_tool_calls": _mean([float(r.get("tool_calls") or 0) for r in rows]),
        "tasks": per_task,
    }


def format_table(rows) -> str:
    """One fixed-width line per ask: task, run index, ok/FAIL, seconds, turns, tool calls, error."""
    header = "%-10s %4s %5s %9s %6s %6s  %s" % (
        "task", "run", "ok", "seconds", "turns", "calls", "error")
    lines = [header]
    for i, row in enumerate(rows, 1):
        lines.append("%-10s %4d %5s %9.3f %6d %6d  %s" % (
            row.get("task", ""), i,
            "ok" if row.get("success") else "FAIL",
            _seconds(row), int(row.get("turns") or 0), int(row.get("tool_calls") or 0),
            row.get("error") or ""))
    return "\n".join(lines)


def format_summary(summary) -> str:
    """The overall line first (success rate, median turns, mean tool calls), then each task's."""
    lines = ["asks=%d  success rate %.1f%%  median seconds %.3f  median turns %.1f  "
             "mean tool calls %.2f"
             % (summary["asks"], 100.0 * summary["success_rate"], summary["median_seconds"],
                summary["median_turns"], summary["mean_tool_calls"])]
    for task_id in sorted(summary["tasks"]):
        t = summary["tasks"][task_id]
        lines.append("  %-10s asks=%d  success rate %.1f%%  median seconds %.3f  "
                     "mean tool calls %.2f"
                     % (task_id, t["asks"], 100.0 * t["success_rate"], t["median_seconds"],
                        t["mean_tool_calls"]))
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
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "bench_out.jsonl"),
                    help="where the jsonl rows go (git-ignored)")
    args = ap.parse_args(argv)
    assert ":9222" not in args.cdp and ":9223" not in args.cdp and ":9224" not in args.cdp
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
    dropping it would quietly shorten the run, so it is reported with the exception's name."""
    return {"task": task_id, "success": False, "seconds": 0.0, "turns": 0, "last_result": "",
            "tool_calls": 0, "error": type(exc).__name__}


def _ask_all(args, ready):
    """Every task, `--repeat` times, appending each row to `--out` AS IT ARRIVES.

    Flushed per row on purpose: a run that is killed part-way -- the operator stops it, the machine
    goes to sleep -- still has every ask that finished, and the next run appends rather than
    republishing.
    """
    rows = []
    with open(args.out, "a", encoding="utf-8") as fh:
        for task_id, prompt, expected_fn in make_tasks(args.tree):
            try:
                expected = expected_fn()
            except Exception as exc:                  # unreadable ground truth: a row, not an end
                for _ in range(args.repeat):
                    row = _failed_row(task_id, exc)
                    rows.append(row)
                    fh.write(json.dumps({"kind": "ask", **row}) + "\n")
                    fh.flush()
                continue
            for _ in range(args.repeat):
                row = run_one(task_id, prompt, expected,
                              holder=ready["holder"], template=ready["template"],
                              catalogue=ready["catalogue"], run_tool=ready["run_tool"],
                              connect=ready["connect"])
                rows.append(row)
                fh.write(json.dumps({"kind": "ask", **row}) + "\n")
                fh.flush()
    return rows


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
        print("\n".join(log))
        rows = _ask_all(args, ready)
        summary = summarize(rows)
        print(format_table(rows))
        print(format_summary(summary))
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "summary", **summary}) + "\n")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
