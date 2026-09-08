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

import os
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

    # 1b. A done flag is not evidence the venv exists -------------------------
    def test_a_missing_venv_clears_the_checkpoints_that_claim_it_exists(self):
        """state.json travels with a folder copy, a OneDrive sync or a ZIP restore; .venv does
        not, or arrives broken. Skipping on the flag alone let STEP 1 report success on a
        machine with NO interpreter -- after which supervisor.ps1 falls back to bare `python`,
        which on a fresh Windows box is the Store alias, and every later symptom points
        somewhere else. Only the venv-dependent steps are cleared; the other four produce their
        own artefacts and re-running them is not free."""
        state = {"done": {n: True for n in ("ensure_venv", "install_deps", "gen_env",
                                            "check_edge", "dev_tunnel", "gen_connector",
                                            "verify")}}
        bootstrap.save_state(state, self.state_file)

        missing = Path(self.tmp.name) / "no-such-venv" / "python.exe"
        with mock.patch.object(bootstrap, "VENV_PYTHON", missing):
            rec = RecordingSteps(["ensure_venv", "install_deps", "gen_env", "check_edge",
                                  "dev_tunnel", "gen_connector", "verify"])
            rc = bootstrap.run_all(steps=rec.steps, state_file=self.state_file)

        self.assertEqual(rc, 0)
        self.assertEqual(rec.calls, ["ensure_venv", "install_deps", "verify"],
                         "the venv-dependent steps did not re-run")

    def test_an_existing_venv_leaves_the_build_checkpoints_alone(self):
        """The clearing must be caused by the venv being absent, not by running at all. `verify`
        is exempt: it is a CHECK and always runs (see the test below)."""
        state = {"done": {n: True for n in ("ensure_venv", "install_deps", "verify")}}
        bootstrap.save_state(state, self.state_file)

        present = Path(self.tmp.name) / "python.exe"
        present.write_text("", encoding="utf-8")
        with mock.patch.object(bootstrap, "VENV_PYTHON", present):
            rec = RecordingSteps(["ensure_venv", "install_deps", "verify"])
            rc = bootstrap.run_all(steps=rec.steps, state_file=self.state_file)

        self.assertEqual(rc, 0)
        self.assertEqual(rec.calls, ["verify"],
                         "a build step re-ran, or the check did not")

    # 1c. A check that is skipped is not a check --------------------------------
    def test_verify_always_runs_even_when_the_checkpoint_says_it_is_done(self):
        """The .venv-missing guard does not fire on the path that actually happens: setup.bat
        creates the venv with uv BEFORE bootstrap runs, so a machine carrying a copied
        state.json arrives with a fresh EMPTY venv and flags saying the dependencies are
        installed. step_verify imports main.py in a subprocess and counts the tools, so it is
        exactly the step that catches that -- and it was the one being skipped."""
        state = {"done": {n: True for n in ("ensure_venv", "install_deps", "verify")}}
        bootstrap.save_state(state, self.state_file)

        present = Path(self.tmp.name) / "python.exe"
        present.write_text("", encoding="utf-8")
        with mock.patch.object(bootstrap, "VENV_PYTHON", present):
            rec = RecordingSteps(["ensure_venv", "install_deps", "verify"])
            bootstrap.run_all(steps=rec.steps, state_file=self.state_file)

        self.assertIn("verify", rec.calls)
        self.assertEqual(bootstrap.ALWAYS_REVALIDATE, ("verify",))

    def test_a_failed_verify_clears_what_the_next_run_has_to_rebuild(self):
        """Otherwise "re-run to retry this step" retries only verify, forever: the steps that
        produce what it verifies stay marked done and are skipped straight back to the same
        failure."""
        state = {"done": {n: True for n in ("ensure_venv", "install_deps", "verify")}}
        bootstrap.save_state(state, self.state_file)

        present = Path(self.tmp.name) / "python.exe"
        present.write_text("", encoding="utf-8")
        with mock.patch.object(bootstrap, "VENV_PYTHON", present):
            rec = RecordingSteps(
                ["ensure_venv", "install_deps", "verify"],
                raisers={"verify": bootstrap.StepError("no module named fastmcp")},
            )
            rc = bootstrap.run_all(steps=rec.steps, state_file=self.state_file)

        self.assertEqual(rc, 1)
        after = bootstrap.load_state(self.state_file)
        self.assertFalse(bootstrap.is_done(after, "install_deps"),
                         "install_deps still marked done after verification failed")
        self.assertFalse(bootstrap.is_done(after, "ensure_venv"))

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

    # 6b. Resume banner appears only when some (not all, not none) steps done --
    def test_resume_banner_shows_when_resuming_and_not_when_fresh(self):
        # Fresh run (no state yet): the RESUMING banner must NOT appear.
        rec_fresh = RecordingSteps(["a", "b", "c"])
        with mock.patch.object(bootstrap, "log") as mlog:
            bootstrap.run_all(steps=rec_fresh.steps, state_file=self.state_file)
        fresh_out = "\n".join(str(c.args[0]) for c in mlog.call_args_list if c.args)
        self.assertNotIn("RESUMING", fresh_out,
                         "RESUMING banner appeared on a fresh (0-done) run")

        # Now simulate an interrupted install: 'a' is already done, resume.
        bootstrap.reset_state(state_file=self.state_file)
        state = {"done": {"a": True}}
        bootstrap.save_state(state, self.state_file)
        rec_resume = RecordingSteps(["a", "b", "c"])
        with mock.patch.object(bootstrap, "log") as mlog:
            bootstrap.run_all(steps=rec_resume.steps, state_file=self.state_file)
        resume_out = "\n".join(str(c.args[0]) for c in mlog.call_args_list if c.args)
        self.assertIn("RESUMING", resume_out,
                      "RESUMING banner missing when a step was already done")
        # Banner must report the correct progress and the next pending step name.
        self.assertIn("1/3", resume_out)
        self.assertIn("continuing from 'b'", resume_out)
        self.assertEqual(rec_resume.calls, ["b", "c"])  # 'a' skipped

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

    def test_provision_targets_renamed_tunnel_from_env(self):
        # FIX 3: if the user renamed the tunnel (MCP_TUNNEL_NAME in .env) and
        # MCP_TUNNEL_URL is absent, provisioning must target the RENAMED tunnel --
        # not the default 'm365-copilot-companion' (which _write_tunnel_to_env would
        # then persist, flipping the renamed setup back to default).
        seen = {"tunnel": None}

        def _capture(dt, tunnel):
            seen["tunnel"] = tunnel

        def _env(key):
            if key == "MCP_TUNNEL_NAME":
                return "custom-name"
            return None  # MCP_TUNNEL_URL absent -> provisioning proceeds

        with mock.patch.object(bootstrap, "find_executable", return_value="devtunnel"), \
             mock.patch.object(bootstrap, "_devtunnel_logged_in", return_value=True), \
             mock.patch.object(bootstrap, "_read_env_value", side_effect=_env), \
             mock.patch.object(bootstrap, "_provision_dev_tunnel", _capture):
            bootstrap.step_dev_tunnel()
        self.assertEqual(seen["tunnel"], "custom-name",
                         "provisioning did not target the renamed MCP_TUNNEL_NAME")


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


@unittest.skipUnless(os.name == "nt", "gen_env protects the generated secrets with DPAPI, "
                                      "which exists only on Windows")
class GenEnvBackfillsMissingSecretsTests(unittest.TestCase):
    """An existing .env that has LOST its required secrets must be repaired by gen_env, not
    left for step_verify to fail on forever. gen_env stays append-only: a secret that is
    already present (even blank/placeholder) is the user's value and is never overwritten.

    SKIPPED OFF WINDOWS, AND RUN ON THE WINDOWS JOB INSTEAD. gen_env calls
    tools.secret_store.protect_secret, which raises "DPAPI protection is only available on
    Windows" -- so on the ubuntu runner these were not a failing assertion but a capability
    that cannot exist there, and they were red for that reason alone. A skip on its own would
    have retired the coverage silently, so ci.yml's windows-install-smoke job now runs this
    file: skipping here is only honest because it runs somewhere."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_root = bootstrap.ROOT
        bootstrap.ROOT = self.root

    def tearDown(self):
        bootstrap.ROOT = self._orig_root
        self.tmp.cleanup()

    def test_missing_api_key_and_unlock_are_generated(self):
        env = self.root / ".env"
        env.write_text("SOME_OTHER=1\n", encoding="utf-8")
        bootstrap.step_gen_env()
        text = env.read_text(encoding="utf-8-sig")
        # Both secrets now present and non-placeholder.
        api = bootstrap._read_env_value("MCP_API_KEY")
        self.assertTrue(api and not api.startswith("replace"),
                        "MCP_API_KEY was not generated into an existing .env")
        prot = bootstrap._read_env_value(bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR)
        self.assertTrue(prot, "protected unlock password was not generated")
        # The pre-existing line is preserved.
        self.assertIn("SOME_OTHER=1", text)

    def test_existing_secrets_are_not_overwritten(self):
        env = self.root / ".env"
        env.write_text(
            "MCP_API_KEY=keepme\n"
            "MCP_UNLOCK_PASSWORD=keepme_too\n",
            encoding="utf-8",
        )
        bootstrap.step_gen_env()
        text = env.read_text(encoding="utf-8-sig")
        # The user's values survive verbatim, and no duplicate key is appended.
        self.assertEqual(bootstrap._read_env_value("MCP_API_KEY"), "keepme")
        self.assertEqual(bootstrap._read_env_value("MCP_UNLOCK_PASSWORD"), "keepme_too")
        self.assertEqual(text.count("MCP_API_KEY="), 1)
        self.assertEqual(text.count("MCP_UNLOCK_PASSWORD="), 1)
        # The protected form must NOT be added when a plain unlock password already exists.
        self.assertNotIn(bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR + "=", text)

    def test_only_api_key_missing_generates_only_api_key(self):
        env = self.root / ".env"
        # Unlock present (protected form); API key absent.
        env.write_text(
            bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR + "=abc123\n",
            encoding="utf-8",
        )
        bootstrap.step_gen_env()
        api = bootstrap._read_env_value("MCP_API_KEY")
        self.assertTrue(api and not api.startswith("replace"))
        # The existing protected unlock value is untouched, and no plain unlock line is minted.
        self.assertEqual(bootstrap._read_env_value(bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR), "abc123")

    def test_repaired_env_then_passes_verify(self):
        # End to end: a .env missing both secrets is repaired by gen_env so that a subsequent
        # verify no longer fails on the .env-key check (the dead end this fix removes). We stub
        # the tool-count import so the test does not need the whole server importable.
        env = self.root / ".env"
        env.write_text("SOME_OTHER=1\n", encoding="utf-8")
        bootstrap.step_gen_env()
        with mock.patch.object(bootstrap, "_count_tools_via_subprocess", return_value=42):
            bootstrap.step_verify()  # must not raise StepError on the key check


@unittest.skipUnless(os.name == "nt", "gen_env protects the generated secrets with DPAPI, "
                                      "which exists only on Windows")
class GenEnvFreshStillWritesSecretsTests(unittest.TestCase):
    """Guard the original path: when NO .env exists, gen_env still writes a fresh one carrying
    both secrets. The backfill branch must not have cannibalised the create branch.

    Windows-only for the same DPAPI reason as the class above."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_root = bootstrap.ROOT
        bootstrap.ROOT = self.root
        # No .env.example in the temp root -> gen_env uses its built-in minimal template.

    def tearDown(self):
        bootstrap.ROOT = self._orig_root
        self.tmp.cleanup()

    def test_fresh_env_has_both_secrets(self):
        bootstrap.step_gen_env()
        env = self.root / ".env"
        self.assertTrue(env.exists())
        api = bootstrap._read_env_value("MCP_API_KEY")
        self.assertTrue(api and not api.startswith("replace"))
        self.assertTrue(bootstrap._read_env_value(bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR))


if __name__ == "__main__":
    unittest.main(verbosity=2)
