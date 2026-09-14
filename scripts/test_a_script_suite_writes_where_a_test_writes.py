# -*- coding: utf-8 -*-
"""Twenty suites ran outside pytest, so none of the live-record isolation applied to them.

`relay/test_live_record_isolation.py` walks relay/, tools/ and bridge/ for constants naming a
directory where the operator's records accumulate, and requires every one to be redirected for
tests or classified as deliberately not. conftest's autouse fixture does the redirecting.

`scripts/run_script_style_tests.py` runs twenty files that pytest collects nothing from -- as
plain scripts, in their own interpreter. conftest never runs for them. The guard and the table
and the classification were all in place and none of it reached the suites that cover the relay
loop, the planner, the watchdog, transient retry and the unlock injection path.

MEASURED 2026-09-13: a single preflight appended 67 rows to the operator's real
`.fleet/mechanisms.jsonl` from that phase -- per-goal fan-out judgements with an empty run_id,
timestamped inside it. The exposure is as old as the runner; it became visible that day only
because the call site writing those rows had been raising NameError into a bare except until an
hour earlier (relay/test_a_swallowed_record_is_no_record.py).

The suites now run through `scripts/run_isolated.py`, which applies
`conftest.LIVE_RECORD_REDIRECTS` -- the same table, not a second copy of it -- before exec.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

RUNNER = os.path.join(REPO, "scripts", "run_isolated.py")
LIVE = os.path.join(REPO, ".fleet", "mechanisms.jsonl")


def _probe(tmp_path, body):
    p = tmp_path / "probe.py"
    p.write_text(body, encoding="utf-8")
    return subprocess.run([sys.executable, RUNNER, str(p)], cwd=REPO,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


# ── the defect ────────────────────────────────────────────────────────────────────────────

def test_a_record_written_by_a_script_does_not_land_in_the_live_file(tmp_path):
    """THE DEFECT. Run the write that was actually observed, and see where it goes."""
    before = os.path.getsize(LIVE) if os.path.exists(LIVE) else None

    r = _probe(tmp_path, "\n".join([
        "import sys",
        "sys.path.insert(0, r'%s')" % REPO,
        "from relay import mechanism_telemetry as MT",
        "MT.record('fanout', run_id='probe', configured=True)",
        "print('LOG=' + MT.LOG)",
    ]))
    assert r.returncode == 0, r.stderr

    after = os.path.getsize(LIVE) if os.path.exists(LIVE) else None
    assert before == after, "オペレータの実ログにスクリプトが書いた"

    log = [l for l in r.stdout.splitlines() if l.startswith("LOG=")][0][4:]
    assert "live_records" in log, log
    assert io.open(log, encoding="utf-8").read().strip(), "リダイレクト先にも書けていない"


def test_the_runner_sends_its_suites_through_it():
    """The isolation is worth nothing if the runner still execs the scripts directly."""
    src = io.open(os.path.join(REPO, "scripts", "run_script_style_tests.py"),
                  encoding="utf-8").read()
    assert "run_isolated.py" in src, "スクリプトを直接起動に戻っている"


def test_it_redirects_more_than_nothing(tmp_path):
    """A runner that silently redirected zero constants would pass the first test by writing
    nothing at all, and would protect nothing in a suite that does write."""
    sys.path.insert(0, REPO)
    from scripts.run_isolated import apply_redirects

    moved = apply_redirects(tmp_path / "base")
    assert moved >= 10, moved


# ── it must not change what the runner reads ──────────────────────────────────────────────

def test_the_exit_code_is_the_suite_s_own():
    """The runner decides pass/fail from the child's exit code. A wrapper that swallowed it
    would report every suite green -- which is the failure this whole phase exists to end."""
    import tempfile

    d = tempfile.mkdtemp()
    p = os.path.join(d, "fails.py")
    io.open(p, "w", encoding="utf-8", newline="\n").write(
        "import sys\nprint('=== 1/2 checks passed ===')\nsys.exit(3)\n")
    r = subprocess.run([sys.executable, RUNNER, p], cwd=REPO, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert "1/2 checks passed" in r.stdout, "出力も素通しでなければ集計が読めない"


def test_a_suite_still_sees_its_own_argv(tmp_path):
    """sys.argv[0] is the suite, not the wrapper: a suite that prints its own name, or reads
    argv at all, must not see run_isolated's."""
    r = _probe(tmp_path, "import sys\nprint('ARGV0=' + sys.argv[0])\n")
    assert r.returncode == 0, r.stderr
    assert "probe.py" in [l for l in r.stdout.splitlines() if l.startswith("ARGV0=")][0]


def test_it_refuses_with_no_script():
    r = subprocess.run([sys.executable, RUNNER], cwd=REPO, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 2
    assert "usage" in r.stderr


def test_the_wrapper_costs_less_than_a_suite_is_given(tmp_path):
    """THE PRICE OF THE ISOLATION, PINNED. The wrapper imports every module in the redirect
    table so it can move their constants before the suite runs. That cost is paid once per
    suite; at 2.4s x 20 it measured +11s on a real preflight, which is worth the isolation. It
    is not worth an unbounded amount, and a fixed cost that creeps up shows first as suites
    timing out for no reason anybody can see -- which is exactly how it showed the first time
    (scripts/test_run_script_style_tests.py's 2s).

    MEASURED AGAINST THE FLOOR, NOT AGAINST A NUMBER, since 2026-09-14. The budget was
    `cost < 6.0` against the wall clock of one subprocess, and what that number mostly contained
    was `import fastmcp` -- reached from tools.file_ops through tools.security, 2.59s of a 2.69s
    import of tools.trace_ops, and paid by ANY isolated run whether or not the wrapper exists.
    So the budget drifted with the library (the 1.76s this docstring used to quote had become
    2.69s without anything in this repository changing), and inside a full suite, where the same
    wall clock also contains contention from seven thousand other tests, it read 10.6s and
    failed -- while passing on its own minutes later. A budget that fails on load gets raised
    until it means nothing, which is the same death the paragraph above is trying to prevent,
    reached from the other side.

    What it now measures is the wrapper's OWN cost: the same subprocess, minus the floor that an
    isolated run cannot avoid, both timed in the same seconds. Measured 0.42s, 0.72s and 2.23s
    across three pairs on a loaded machine -- the minimum, 0.42s, is the one nearest the truth,
    because contention can only add.

    AND ONE REAL 2.7s WAS FOUND AND REMOVED while diagnosing this: tools/rebuild_history.py
    imported `bridge.copilot_bridge` at module scope for one helper. conftest lists that module
    in ONLY_IF_ALREADY_IMPORTED precisely because "the bridge takes ~4s", and this walked in
    through the back door -- the wrapper skipped importing the bridge and then imported
    rebuild_history, which imported it anyway. Deferring that import to the call site took
    `import tools.rebuild_history` from 2.73s to 0.03s."""
    import time

    p = tmp_path / "nothing.py"
    p.write_text("print('=== 1/1 checks passed ===')\n", encoding="utf-8")

    def _time(argv):
        t = time.time()
        r = subprocess.run(argv, cwd=REPO, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
        return time.time() - t, r

    # THE FLOOR ANY ISOLATED RUN PAYS ANYWAY. tools.file_ops -> tools.security -> fastmcp, and
    # `import fastmcp` alone is 2.59s of it. Measured on this machine the floor is 3.3s and the
    # whole wrapper 3.7s, so an absolute budget of 6.0s was measuring the LIBRARY, not the
    # wrapper -- which is why it drifted from the 2.4s its docstring recorded, and why it
    # reached 10.6s inside a full suite and failed there while passing alone.
    floor = [sys.executable, "-c", "import sys; sys.path.insert(0, '.'); import tools.file_ops"]

    # THE MINIMUM OF THREE, not the mean. Both numbers are wall clock on a machine that may be
    # running seven thousand other tests; contention only ever makes a sample larger, so the
    # smallest is the one closest to the fixed cost this is about. Taken in the same seconds as
    # each other, so whatever load there is applies to both.
    costs, floors = [], []
    for _ in range(3):
        fc, _fr = _time(floor)
        wc, r = _time([sys.executable, RUNNER, str(p)])
        assert r.returncode == 0, r.stderr
        floors.append(fc)
        costs.append(wc)

    overhead = min(costs) - min(floors)
    assert overhead < 2.0, (
        "ラッパー自身の固定コストが %.1fs まで伸びている (wrapper %.1fs / floor %.1fs)。"
        "計測値は 0.4-0.7s。2.7s 増えるのは redirect 表のモジュールが bridge や "
        "それに並ぶ重い依存を import し始めたとき"
        % (overhead, min(costs), min(floors)))
