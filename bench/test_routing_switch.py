"""The switch has to answer the same way when the script is RUN as when it is imported.

Both pro_stage_goals.py and pro_capture.py carried:

    try:
        from bench.remote import broker_client as bc
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


_FAKE_PING_OK = (
    "import subprocess\n"
    "class _P:\n"
    "    stdout = '{\"ok\": true, \"pong\": true}'\n"
    "    stderr = ''\n"
    "    returncode = 0\n"
    "subprocess.run = lambda *a, **k: _P()\n"
)

_FAKE_PING_DOWN = (
    "import subprocess\n"
    "class _P:\n"
    "    stdout = ''\n"
    "    stderr = 'ssh: connect to host swe-broker port 22: Connection refused'\n"
    "    returncode = 255\n"
    "subprocess.run = lambda *a, **k: _P()\n"
)


def test_relay_is_importable_from_the_bench_directory():
    """THE ONE THAT WOULD HAVE CAUGHT IT. `from bench.remote import broker_client` raises here unless
    the helper puts the repository on the path first. The broker's `subprocess.run` is faked so
    this exercises the import/path wiring, not a real network call."""
    out = _run(_FAKE_PING_OK +
               "import routing_switch; print(routing_switch.broker() is not None)", cwd=BENCH,
               env={"SWE_BROKER": "on"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True", (
        "the switch was asked for and the helper answered no; that answer sent staging to "
        "clone locally and capture to read an empty directory")


def test_broker_pings_before_handing_back_the_client():
    """THE GAP THE BURNDOWN NAMED: `broker()` used to establish only that the module imports,
    never that the broker answers. A routed run against a down host must fail here, once, with
    a reason -- not proceed and fail one `create` at a time across every instance."""
    out = _run(_FAKE_PING_OK +
               "import routing_switch; print(routing_switch.broker() is not None)", cwd=BENCH,
               env={"SWE_BROKER": "on"})
    assert out.stdout.strip() == "True", (out.stdout, out.stderr)

    out = _run(
        _FAKE_PING_DOWN +
        "import routing_switch as R\n"
        "try:\n"
        "    R.broker()\n"
        "    print('FELL BACK')\n"
        "except RuntimeError as e:\n"
        "    print('RAISED')\n",
        cwd=BENCH, env={"SWE_BROKER": "on"})
    assert out.stdout.strip() == "RAISED", (
        "the broker never answered a ping and routing was asked for; this must raise, never "
        "fall back to running the instance on this machine", out.stdout, out.stderr)


def test_the_ping_is_cached_for_the_process_not_repeated_per_call():
    """`broker()` is called once per instance in a run of forty; re-pinging every call would
    turn that into forty extra SSH round trips for a fact that does not change mid-run."""
    out = _run(
        "import subprocess\n"
        "calls = []\n"
        "class _P:\n"
        "    stdout = '{\"ok\": true, \"pong\": true}'\n"
        "    stderr = ''\n"
        "    returncode = 0\n"
        "def _fake_run(*a, **k):\n"
        "    calls.append(1)\n"
        "    return _P()\n"
        "subprocess.run = _fake_run\n"
        "import routing_switch as R\n"
        "R.broker(); R.broker(); R.broker()\n"
        "print(len(calls))\n",
        cwd=BENCH, env={"SWE_BROKER": "on"})
    assert out.stdout.strip() == "1", (
        "broker() pinged more than once per process", out.stdout, out.stderr)


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
