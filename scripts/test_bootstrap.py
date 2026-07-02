#!/usr/bin/env python3
# =============================================================================
# Unit tests for the resumable state machine in bootstrap.py.
#
# These tests use MOCKED step functions only -- they never install Python, run
# pip/winget, touch the network, or change anything on the system. They prove:
#   1. A clean run executes every step in order and exits 0.
#   2. An ActionNeeded pause stops at the right step, exits 2, and records the
#      completed-so-far steps; re-running RESUMES and SKIPS completed steps.
#   3. A StepError pause exits 1 and is likewise resumable.
#   4. --reset clears the saved state.
#   5. --status reflects done/pending and does not mutate state.
#   6. run_only runs exactly one step and marks it done.
#
# Run:  python scripts/test_bootstrap.py     (no pytest dependency required)
# ASCII / ENGLISH ONLY.
# =============================================================================
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Make 'bootstrap' importable regardless of the caller's working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: E402


def _completed(returncode=0, stdout="", stderr=""):
    """Build a fake subprocess.CompletedProcess for patching subprocess.run."""
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class RecordingSteps:
    """Builds a list of (name, fn) steps whose fns just append their name to a
    shared 'calls' list. Selected steps can be made to raise ActionNeeded /
    StepError to simulate a pause."""

    def __init__(self, names, raisers=None):
        self.calls = []
        self.raisers = raisers or {}  # name -> exception instance to raise
        self.steps = [(n, self._make(n)) for n in names]

    def _make(self, name):
        def fn():
            self.calls.append(name)
            if name in self.raisers:
                raise self.raisers[name]
        return fn


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    # 1. Clean full run -------------------------------------------------------
    def test_clean_run_executes_all_steps_in_order(self):
        rec = RecordingSteps(["a", "b", "c"])
        rc = bootstrap.run_all(steps=rec.steps, state_file=self.state_file)
        self.assertEqual(rc, 0)
        self.assertEqual(rec.calls, ["a", "b", "c"])
        state = bootstrap.load_state(self.state_file)
        for n in ("a", "b", "c"):
            self.assertTrue(bootstrap.is_done(state, n))

    # 2. ActionNeeded pause then resume --------------------------------------
    def test_action_needed_pauses_and_resume_skips_completed(self):
        # First run: 'b' raises ActionNeeded -> a done, b/c pending, rc=2.
        rec1 = RecordingSteps(
            ["a", "b", "c"],
            raisers={"b": bootstrap.ActionNeeded("sign in to Microsoft")},
        )
        rc1 = bootstrap.run_all(steps=rec1.steps, state_file=self.state_file)
        self.assertEqual(rc1, 2)
        self.assertEqual(rec1.calls, ["a", "b"])  # stopped at b, never reached c
        state = bootstrap.load_state(self.state_file)
        self.assertTrue(bootstrap.is_done(state, "a"))
        self.assertFalse(bootstrap.is_done(state, "b"))
        self.assertFalse(bootstrap.is_done(state, "c"))

        # Second run (resume): user "fixed" the issue, so 'b' no longer raises.
        # 'a' must be SKIPPED; only b and c run.
        rec2 = RecordingSteps(["a", "b", "c"])
        rc2 = bootstrap.run_all(steps=rec2.steps, state_file=self.state_file)
        self.assertEqual(rc2, 0)
        self.assertEqual(rec2.calls, ["b", "c"])  # 'a' skipped on resume
        state = bootstrap.load_state(self.state_file)
        for n in ("a", "b", "c"):
            self.assertTrue(bootstrap.is_done(state, n))

    # 3. StepError pause then resume -----------------------------------------
    def test_step_error_pauses_with_rc1_and_is_resumable(self):
        rec1 = RecordingSteps(
            ["a", "b", "c"],
            raisers={"a": bootstrap.StepError("pip failed")},
        )
        rc1 = bootstrap.run_all(steps=rec1.steps, state_file=self.state_file)
        self.assertEqual(rc1, 1)
        self.assertEqual(rec1.calls, ["a"])
        state = bootstrap.load_state(self.state_file)
        self.assertFalse(bootstrap.is_done(state, "a"))

        rec2 = RecordingSteps(["a", "b", "c"])
        rc2 = bootstrap.run_all(steps=rec2.steps, state_file=self.state_file)
        self.assertEqual(rc2, 0)
        self.assertEqual(rec2.calls, ["a", "b", "c"])  # retried a, then b, c

    # 4. --reset clears state -------------------------------------------------
    def test_reset_clears_state(self):
        rec = RecordingSteps(["a", "b"])
        bootstrap.run_all(steps=rec.steps, state_file=self.state_file)
        self.assertTrue(self.state_file.exists())
        rc = bootstrap.reset_state(state_file=self.state_file)
        self.assertEqual(rc, 0)
        self.assertFalse(self.state_file.exists())
        # A fresh load yields an empty done-map (nothing marked).
        state = bootstrap.load_state(self.state_file)
        self.assertEqual(state.get("done", {}), {})

    # 5. --status is read-only ------------------------------------------------
    def test_status_does_not_mutate_state(self):
        rec = RecordingSteps(
            ["a", "b", "c"],
            raisers={"b": bootstrap.ActionNeeded("x")},
        )
        bootstrap.run_all(steps=rec.steps, state_file=self.state_file)
        before = self.state_file.read_bytes()
        rc = bootstrap.print_status(steps=rec.steps, state_file=self.state_file)
        self.assertEqual(rc, 0)
        after = self.state_file.read_bytes()
        self.assertEqual(before, after)  # status changed nothing

    # 6. run_only runs exactly one step --------------------------------------
    def test_run_only_runs_single_step(self):
        rec = RecordingSteps(["a", "b", "c"])
        rc = bootstrap.run_only("b", steps=rec.steps, state_file=self.state_file)
        self.assertEqual(rc, 0)
        self.assertEqual(rec.calls, ["b"])
        state = bootstrap.load_state(self.state_file)
        self.assertTrue(bootstrap.is_done(state, "b"))
        self.assertFalse(bootstrap.is_done(state, "a"))

    def test_run_only_unknown_step_errors(self):
        rec = RecordingSteps(["a", "b"])
        rc = bootstrap.run_only("zzz", steps=rec.steps, state_file=self.state_file)
        self.assertEqual(rc, 1)
        self.assertEqual(rec.calls, [])

    # 7. Corrupt state file does not wedge the machine -----------------------
    def test_corrupt_state_file_starts_clean(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text("{ this is not json", encoding="utf-8")
        state = bootstrap.load_state(self.state_file)
        self.assertEqual(state.get("done", {}), {})


class DevTunnelNeverBlocksTests(unittest.TestCase):
    """FIX 1: step_dev_tunnel must NEVER raise ActionNeeded -- the real install
    and interactive sign-in happen later at quickstart STEP 4. On a clean PC
    (devtunnel CLI absent) it must WARN and return normally so the bootstrap
    keeps going instead of walling the novice."""

    def test_missing_devtunnel_returns_done_not_action_needed(self):
        # find_executable -> None (CLI not on PATH), and force the winget-Links
        # fallback path to a location that does not exist.
        with mock.patch.object(bootstrap, "find_executable", return_value=None), \
             mock.patch.dict(bootstrap.os.environ, {"LOCALAPPDATA": ""}, clear=False):
            # Must NOT raise (ActionNeeded, StepError, or anything else).
            try:
                bootstrap.step_dev_tunnel()
            except bootstrap.ActionNeeded as e:
                self.fail("step_dev_tunnel raised ActionNeeded (must not wall): %s" % e)
            except Exception as e:  # noqa: BLE001
                self.fail("step_dev_tunnel raised unexpectedly: %r" % e)

    def test_present_but_not_signed_in_returns_done_not_action_needed(self):
        # devtunnel present, but not signed in -> still must not wall.
        with mock.patch.object(bootstrap, "find_executable", return_value="devtunnel"), \
             mock.patch.object(bootstrap, "_devtunnel_logged_in", return_value=False):
            try:
                bootstrap.step_dev_tunnel()
            except bootstrap.ActionNeeded as e:
                self.fail("not-signed-in raised ActionNeeded (must not wall): %s" % e)

    def test_provision_failure_does_not_wall(self):
        # Signed in, no existing URL, but provisioning blows up -> WARN + return.
        with mock.patch.object(bootstrap, "find_executable", return_value="devtunnel"), \
             mock.patch.object(bootstrap, "_devtunnel_logged_in", return_value=True), \
             mock.patch.object(bootstrap, "_read_env_value", return_value=None), \
             mock.patch.object(bootstrap, "_provision_dev_tunnel",
                               side_effect=RuntimeError("boom")):
            try:
                bootstrap.step_dev_tunnel()
            except Exception as e:  # noqa: BLE001
                self.fail("provision failure was not swallowed: %r" % e)

    def test_short_circuits_when_url_already_present(self):
        # Signed in and .env already has MCP_TUNNEL_URL -> must NOT re-host.
        called = {"provision": False}

        def _should_not_run(*a, **k):
            called["provision"] = True

        with mock.patch.object(bootstrap, "find_executable", return_value="devtunnel"), \
             mock.patch.object(bootstrap, "_devtunnel_logged_in", return_value=True), \
             mock.patch.object(bootstrap, "_read_env_value",
                               return_value="https://x-8000.jpe1.devtunnels.ms/"), \
             mock.patch.object(bootstrap, "_provision_dev_tunnel", _should_not_run):
            bootstrap.step_dev_tunnel()
        self.assertFalse(called["provision"],
                         "provisioning ran even though MCP_TUNNEL_URL was already set")


class WriteTunnelPreservesUrlTests(unittest.TestCase):
    """FIX 1: a transient failure (url=None) must NEVER blank an existing
    non-empty MCP_TUNNEL_URL already recorded in .env."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_root = bootstrap.ROOT
        bootstrap.ROOT = self.root  # _write_tunnel_to_env writes ROOT/.env

    def tearDown(self):
        bootstrap.ROOT = self._orig_root
        self.tmp.cleanup()

    def test_none_url_preserves_existing_url(self):
        env = self.root / ".env"
        env.write_text(
            "MCP_API_KEY=abc\r\n"
            "MCP_TUNNEL_NAME=old-name\r\n"
            "MCP_TUNNEL_URL=https://keep-me-8000.jpe1.devtunnels.ms/\r\n",
            encoding="utf-8", newline="",
        )
        # url=None (a hosting hiccup) must keep the prior URL.
        bootstrap._write_tunnel_to_env("m365-copilot-companion", None)
        text = env.read_text(encoding="utf-8-sig")
        self.assertIn("MCP_TUNNEL_URL=https://keep-me-8000.jpe1.devtunnels.ms/", text)
        self.assertIn("MCP_API_KEY=abc", text)  # other keys preserved

    def test_new_url_overwrites_old(self):
        env = self.root / ".env"
        env.write_text(
            "MCP_TUNNEL_URL=https://stale-8000.jpe1.devtunnels.ms/\r\n",
            encoding="utf-8", newline="",
        )
        bootstrap._write_tunnel_to_env("m365-copilot-companion",
                                       "https://fresh-8000.jpe1.devtunnels.ms/")
        text = env.read_text(encoding="utf-8-sig")
        self.assertIn("MCP_TUNNEL_URL=https://fresh-8000.jpe1.devtunnels.ms/", text)
        self.assertNotIn("stale", text)


class EnsureVenvInvalidatesDepsTests(unittest.TestCase):
    """FIX 2: when ensure_venv finds a venv whose python fails a real probe, it
    must recreate the venv AND clear the install_deps done-flag so deps get
    reinstalled (the observed 'venv recreated but deps skipped' bug)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_file = self.root / "state.json"
        self._orig_root = bootstrap.ROOT
        self._orig_vpy = bootstrap.VENV_PYTHON
        bootstrap.ROOT = self.root
        # Point VENV_PYTHON at a file we can create/remove inside the temp root.
        self.vpy = self.root / ".venv" / "Scripts" / "python.exe"
        bootstrap.VENV_PYTHON = self.vpy

    def tearDown(self):
        bootstrap.ROOT = self._orig_root
        bootstrap.VENV_PYTHON = self._orig_vpy
        self.tmp.cleanup()

    def test_broken_venv_recreated_and_install_deps_flag_cleared(self):
        # Pretend a venv exists on disk but is broken (probe fails), and that
        # install_deps was previously marked done.
        self.vpy.parent.mkdir(parents=True, exist_ok=True)
        self.vpy.write_text("not a real python", encoding="utf-8")
        state = {"done": {"install_deps": True, "ensure_venv": False}}

        # Probe (pip --version) fails; recreation ('python -m venv') "succeeds"
        # by creating the python.exe file back.
        def fake_run(cmd, *a, **k):
            return _completed(returncode=1)  # probe fails

        def fake_call(cmd, *a, **k):
            # Simulate 'python -m venv .venv' rebuilding the interpreter file.
            self.vpy.parent.mkdir(parents=True, exist_ok=True)
            self.vpy.write_text("rebuilt", encoding="utf-8")
            return 0

        with mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(bootstrap.subprocess, "call", side_effect=fake_call):
            bootstrap.step_ensure_venv(state=state, state_file=self.state_file)

        # install_deps checkpoint must have been cleared so deps reinstall.
        self.assertFalse(bootstrap.is_done(state, "install_deps"),
                         "install_deps done-flag was NOT cleared on venv recreate")

    def test_healthy_venv_leaves_install_deps_flag_untouched(self):
        self.vpy.parent.mkdir(parents=True, exist_ok=True)
        self.vpy.write_text("real python", encoding="utf-8")
        state = {"done": {"install_deps": True}}

        with mock.patch.object(bootstrap.subprocess, "run",
                               return_value=_completed(returncode=0)):
            bootstrap.step_ensure_venv(state=state, state_file=self.state_file)

        # Healthy probe -> nothing recreated, install_deps stays done.
        self.assertTrue(bootstrap.is_done(state, "install_deps"))


class InstallDepsSentinelTests(unittest.TestCase):
    """FIX 2: install_deps must not be considered done just because pip exits 0.
    If the sentinel import (import fastmcp, httpx) fails in the venv, it must
    raise StepError with a readable message."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_root = bootstrap.ROOT
        self._orig_vpy = bootstrap.VENV_PYTHON
        bootstrap.ROOT = self.root
        self.vpy = self.root / ".venv" / "Scripts" / "python.exe"
        bootstrap.VENV_PYTHON = self.vpy
        self.vpy.parent.mkdir(parents=True, exist_ok=True)
        self.vpy.write_text("python", encoding="utf-8")
        (self.root / "requirements.txt").write_text("fastmcp\n", encoding="utf-8")

    def tearDown(self):
        bootstrap.ROOT = self._orig_root
        bootstrap.VENV_PYTHON = self._orig_vpy
        self.tmp.cleanup()

    def test_sentinel_import_failure_raises_step_error(self):
        # pip upgrade + install both "succeed" (call -> 0); the sentinel import
        # (run) FAILS -> StepError.
        def fake_run(cmd, *a, **k):
            return _completed(returncode=1, stderr="ModuleNotFoundError: fastmcp")

        with mock.patch.object(bootstrap.subprocess, "call", return_value=0), \
             mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(bootstrap.StepError) as ctx:
                bootstrap.step_install_deps()
        # Message should mention import + the retry path (novice-readable).
        msg = str(ctx.exception)
        self.assertIn("import", msg.lower())
        self.assertIn("quickstart.bat", msg)

    def test_sentinel_import_success_completes(self):
        # pip succeeds AND the import probe succeeds -> no raise.
        with mock.patch.object(bootstrap.subprocess, "call", return_value=0), \
             mock.patch.object(bootstrap.subprocess, "run",
                               return_value=_completed(returncode=0)):
            bootstrap.step_install_deps()  # must not raise


class LoadDotenvOverrideTests(unittest.TestCase):
    """FIX 4c: verify loads .env with OVERRIDE semantics -- a stale pre-set
    environment variable must NOT shadow the .env value."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_dotenv_overrides_preset_env(self):
        self.env.write_text("MCP_TEST_KEY=from_dotenv\n", encoding="utf-8")
        with mock.patch.dict(bootstrap.os.environ,
                             {"MCP_TEST_KEY": "stale_preset"}, clear=False):
            bootstrap._load_dotenv_into_env(self.env)
            self.assertEqual(bootstrap.os.environ["MCP_TEST_KEY"], "from_dotenv")


if __name__ == "__main__":
    unittest.main(verbosity=2)
