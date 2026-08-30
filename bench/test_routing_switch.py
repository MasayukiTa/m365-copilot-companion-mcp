"""The switch has to answer the same way when the script is RUN as when it is imported.

Both pro_stage_goals.py and pro_capture.py carried:

    try:
        from relay import broker_client as bc
    except ImportError:
        routed = False

Run as `python bench/<script>.py`, sys.path[0] is bench/ and that import always raises, so both
read "routing is off" every time while the switch was on. Staging made four local clones and
printed "ok"; capture read the empty local directories and printed four skips saying "not a
worktree root", after a worker had edited seven files inside its container. Neither reads as a
switch being ignored.

The subprocess tests are the point: an in-process import cannot reproduce the sys.path the
scripts actually run under, and that is where the bug lived.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "bench")


def _run(code, cwd, env=None):
    e = dict(os.environ)
    e.pop("SWE_BROKER", None)
    # PYTHONPATH too: pytest can leave the repository on it, which would put `relay` within
    # reach no matter what the helper does and make the fail-back test pass for free.
    e.pop("PYTHONPATH", None)
    e.update(env or {})
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=e,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_the_switch_is_reachable_from_the_bench_directory():
    """sys.path[0] = bench/, exactly as `python bench/pro_capture.py` gets it."""
    out = _run("import routing_switch; print(routing_switch.REPO)", cwd=BENCH)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == REPO


def test_it_says_routing_is_off_when_neither_switch_is_set(tmp_path):
    code = ("import routing_switch as R; R.MARKER = r'%s'; print(R.routing_requested())"
            % (tmp_path / "absent"))
    out = _run(code, cwd=BENCH)
    assert out.stdout.strip() == "False", out.stderr


def test_the_environment_variable_is_enough(tmp_path):
    code = ("import routing_switch as R; R.MARKER = r'%s'; print(R.routing_requested())"
            % (tmp_path / "absent"))
    out = _run(code, cwd=BENCH, env={"SWE_BROKER": "on"})
    assert out.stdout.strip() == "True", out.stderr


def test_the_marker_file_is_enough(tmp_path):
    marker = tmp_path / "BROKER_ON"
    marker.write_text("on", encoding="utf-8")
    code = "import routing_switch as R; R.MARKER = r'%s'; print(R.routing_requested())" % marker
    out = _run(code, cwd=BENCH)
    assert out.stdout.strip() == "True", out.stderr


def test_relay_is_importable_from_the_bench_directory():
    """THE ONE THAT WOULD HAVE CAUGHT IT. `from relay import broker_client` raises here unless
    the helper puts the repository on the path first."""
    out = _run("import routing_switch; print(routing_switch.broker() is not None)", cwd=BENCH,
               env={"SWE_BROKER": "on"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True", (
        "the switch was asked for and the helper answered no; that answer sent staging to "
        "clone locally and capture to read an empty directory")


def test_being_unable_to_answer_is_an_error_not_a_no(tmp_path):
    """Routing asked for + relay unreachable must raise, never fall back to this machine."""
    code = ("import routing_switch as R\n"
            "R.REPO = r'%s'\n"                       # a repo with no relay package in it
            "try:\n"
            "    R.broker()\n"
            "    print('FELL BACK')\n"
            "except RuntimeError as e:\n"
            "    print('RAISED')\n" % tmp_path)
    out = _run(code, cwd=BENCH, env={"SWE_BROKER": "on"})
    assert out.stdout.strip() == "RAISED", (out.stdout, out.stderr)
