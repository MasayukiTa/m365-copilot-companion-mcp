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
        # gen_env is revalidated every run (its flag proves nothing about the .env on disk); the
        # other artefact steps (check_edge, dev_tunnel, gen_connector) stay skipped.
        self.assertEqual(rec.calls, ["ensure_venv", "install_deps", "gen_env", "verify"],
                         "the venv-dependent steps did not re-run")

    def test_an_existing_venv_leaves_the_build_checkpoints_alone(self):
        """The clearing must be caused by the venv being absent, not by running at all. `verify`
        is exempt: it is a CHECK and always runs (see the test below)."""
        state = {"done": {n: True for n in ("ensure_venv", "install_deps", "verify")}}
        bootstrap.save_state(state, self.state_file)

        present = Path(self.tmp.name) / "python.exe"
        present.write_text("", encoding="utf-8")
        # A WORKING venv is the premise, so it is stated: since 2026-09-24 (D20) an existing
        # venv that cannot run is cleared too, and an empty file cannot run. The requirements
        # hash is stated for the same reason (D5): a stale hash clears install_deps by design.
        state["install_deps_requirements_sha256"] = bootstrap.requirements_hash()
        bootstrap.save_state(state, self.state_file)
        with mock.patch.object(bootstrap, "VENV_PYTHON", present), \
                mock.patch.object(bootstrap, "_venv_usable", return_value=True):
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
        self.assertEqual(bootstrap.ALWAYS_REVALIDATE, ("gen_env", "verify"))

    def test_gen_env_runs_even_when_its_checkpoint_says_it_is_done(self):
        """A .env carried from another PC arrives WITH a state.json saying gen_env is done, so the
        step that can repair it was skipped and verify failed on the first run; it only ran on
        the second. It must run on the first."""
        state = {"done": {n: True for n in ("ensure_venv", "install_deps", "gen_env", "verify")}}
        bootstrap.save_state(state, self.state_file)

        present = Path(self.tmp.name) / "python.exe"
        present.write_text("", encoding="utf-8")
        with mock.patch.object(bootstrap, "VENV_PYTHON", present):
            rec = RecordingSteps(["ensure_venv", "install_deps", "gen_env", "verify"])
            bootstrap.run_all(steps=rec.steps, state_file=self.state_file)

        self.assertIn("gen_env", rec.calls)

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

    # A TEMPORARY ROOT, ALWAYS (added 2026-09-24). These tests patch _read_env_value and left
    # ROOT pointing at the checkout; once step_dev_tunnel learned to set aside a carried .env's
    # tunnel keys (D7), a run of this class REWROTE THE CHECKOUT'S REAL .env -- measured, on the
    # owner's machine, and put back by hand. Nothing in this class may reach a real .env again.
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_root = bootstrap.ROOT
        bootstrap.ROOT = Path(self._tmp.name)

    def tearDown(self):
        bootstrap.ROOT = self._orig_root
        self._tmp.cleanup()

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

    def _run_with_env(self, values):
        """Run step_dev_tunnel signed in, with .env answering from `values`. Returns whether
        provisioning ran."""
        called = {"provision": False}

        def _mark(*a, **k):
            called["provision"] = True

        with mock.patch.object(bootstrap, "find_executable", return_value="devtunnel"), \
             mock.patch.object(bootstrap, "_devtunnel_logged_in", return_value=True), \
             mock.patch.object(bootstrap, "_read_env_value", values.get), \
             mock.patch.object(bootstrap, "_this_host", return_value="pc-new"), \
             mock.patch.object(bootstrap, "_provision_dev_tunnel", _mark):
            bootstrap.step_dev_tunnel()
        return called["provision"]

    URL = "https://x-8000.jpe1.devtunnels.ms/"

    def test_short_circuits_when_this_machine_minted_the_url(self):
        # Signed in, and the URL carries this machine's stamp -> must NOT re-host (~30s).
        ran = self._run_with_env({"MCP_TUNNEL_URL": self.URL, "MCP_TUNNEL_HOST": "pc-new"})
        self.assertFalse(ran, "re-hosted a tunnel this machine had already provisioned")

    def test_provisions_when_the_url_came_from_another_machine(self):
        """THE NEW-PC FAILURE (reported 2026-09-08).

        This test used to assert the opposite -- that a non-empty MCP_TUNNEL_URL was enough to
        skip -- and that is exactly the bug it locked in. Setting up a new PC starts by
        carrying .env across, so the URL is present and belongs to the OLD machine; setup
        skipped provisioning, and the address pasted into Copilot Studio pointed at a tunnel
        this machine does not host. A devtunnel URL is reachable only while a machine hosts
        that tunnel, so it is a fact about a machine, and .env travels between machines.
        """
        ran = self._run_with_env({"MCP_TUNNEL_URL": self.URL, "MCP_TUNNEL_HOST": "pc-old"})
        self.assertTrue(ran, "trusted a URL minted on a different machine")

    def test_provisions_when_no_machine_is_recorded(self):
        """A .env written before the stamp existed. Provenance unknown, so do not trust it:
        re-hosting the same tunnel name yields the same URL, so being wrong costs ~30s once,
        and the re-host writes the stamp that settles it from then on."""
        ran = self._run_with_env({"MCP_TUNNEL_URL": self.URL})
        self.assertTrue(ran, "trusted a URL of unknown provenance")

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
            "MCP_API_KEY_PROTECTED=dpapi:opaque\r\n"
            "MCP_TUNNEL_NAME=old-name\r\n"
            "MCP_TUNNEL_URL=https://keep-me-8000.jpe1.devtunnels.ms/\r\n",
            encoding="utf-8", newline="",
        )
        # url=None (a hosting hiccup) must keep the prior URL.
        bootstrap._write_tunnel_to_env("m365-copilot-companion", None)
        text = env.read_text(encoding="utf-8-sig")
        self.assertIn("MCP_TUNNEL_URL=https://keep-me-8000.jpe1.devtunnels.ms/", text)
        self.assertIn("MCP_API_KEY_PROTECTED=dpapi:opaque", text)  # unrelated key preserved

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
        self._orig_sf = bootstrap.STATE_FILE
        bootstrap.STATE_FILE = self.root / ".setup" / "state.json"   # the install lock lives beside it
        bootstrap.ROOT = self.root
        # Point VENV_PYTHON at a file we can create/remove inside the temp root.
        self.vpy = self.root / ".venv" / "Scripts" / "python.exe"
        bootstrap.VENV_PYTHON = self.vpy

    def tearDown(self):
        bootstrap.ROOT = self._orig_root
        bootstrap.VENV_PYTHON = self._orig_vpy
        bootstrap.STATE_FILE = self._orig_sf
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
        self._orig_sf = bootstrap.STATE_FILE
        bootstrap.STATE_FILE = self.root / ".setup" / "state.json"   # the install lock lives beside it
        bootstrap.ROOT = self.root
        self.vpy = self.root / ".venv" / "Scripts" / "python.exe"
        bootstrap.VENV_PYTHON = self.vpy
        self.vpy.parent.mkdir(parents=True, exist_ok=True)
        self.vpy.write_text("python", encoding="utf-8")
        (self.root / "requirements.txt").write_text("fastmcp\n", encoding="utf-8")

    def tearDown(self):
        bootstrap.ROOT = self._orig_root
        bootstrap.VENV_PYTHON = self._orig_vpy
        bootstrap.STATE_FILE = self._orig_sf
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
        # pip succeeds AND the verify import of main.py prints a tool count -> no raise.
        with mock.patch.object(bootstrap.subprocess, "call", return_value=0), \
             mock.patch.object(bootstrap.subprocess, "run",
                               return_value=_completed(returncode=0, stdout="42")):
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
        api = bootstrap._read_env_value("MCP_API_KEY_PROTECTED")
        self.assertTrue(api and api.startswith("dpapi:"),
                        "protected MCP_API_KEY was not generated into an existing .env")
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
        # The user's values survive semantically, but plaintext storage is migrated away.
        from tools.secret_store import unprotect_secret
        api_blob = bootstrap._read_env_value("MCP_API_KEY_PROTECTED")
        unlock_blob = bootstrap._read_env_value(bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR)
        self.assertEqual(unprotect_secret(api_blob), "keepme")
        self.assertEqual(unprotect_secret(unlock_blob), "keepme_too")
        self.assertNotIn("MCP_API_KEY=", text)
        self.assertNotIn("MCP_UNLOCK_PASSWORD=", text)

    def test_only_api_key_missing_generates_only_api_key(self):
        env = self.root / ".env"
        # Unlock present (a value this account can open); API key absent.
        good_unlock = bootstrap.protect_secret("keep-this-unlock")
        env.write_text(
            bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR + "=" + good_unlock + "\n",
            encoding="utf-8",
        )
        bootstrap.step_gen_env()
        api = bootstrap._read_env_value("MCP_API_KEY_PROTECTED")
        self.assertTrue(api and api.startswith("dpapi:"))
        # The existing protected unlock value is untouched, and no plain unlock line is minted.
        self.assertEqual(bootstrap._read_env_value(bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR), good_unlock)

    def test_one_run_repairs_a_carried_over_env_with_done_flags(self):
        """The screen that was reported: .env and state.json both came from another PC, every
        step is flagged done, the protected secrets cannot be opened here. ONE run of the real
        gen_env + verify must end at rc 0 -- not fail first and mend on the second run."""
        state_file = self.root / ".setup" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        orig_sf = bootstrap.STATE_FILE
        bootstrap.STATE_FILE = state_file
        try:
            (self.root / ".env").write_text(
                "MCP_API_KEY_PROTECTED=dpapi:foreign-blob-a\n"
                + bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR + "=dpapi:foreign-blob-b\n",
                encoding="utf-8")
            bootstrap.save_state({"done": {n: True for n in ("ensure_venv", "install_deps",
                                                              "gen_env", "verify")}}, state_file)
            steps = [("ensure_venv", lambda: None), ("install_deps", lambda: None),
                     ("gen_env", bootstrap.step_gen_env), ("verify", bootstrap.step_verify)]
            present = self.root / "python.exe"
            present.write_text("", encoding="utf-8")
            with mock.patch.object(bootstrap, "VENV_PYTHON", present), \
                    mock.patch.object(bootstrap, "_count_tools_via_subprocess", return_value=8), \
                    mock.patch.object(bootstrap, "deps_are_stale", return_value=False):
                rc = bootstrap.run_all(steps=steps, state_file=state_file)
        finally:
            bootstrap.STATE_FILE = orig_sf
        self.assertEqual(rc, 0, "the first run did not repair the carried-over .env")
        self.assertTrue(bootstrap._secret_is_usable(bootstrap._read_env_value("MCP_API_KEY_PROTECTED")))

    def test_secrets_this_machine_cannot_use_are_replaced_not_kept(self):
        # An .env carried from another PC/account: the protected values are present but cannot be
        # opened here (foreign blob), blank, or a template placeholder. They are machine-made and
        # never typed, so this machine makes new ones -- with the old file kept aside -- instead of
        # leaving verify failing on every run.
        for bad_api, bad_unlock in (("dpapi:foreign-blob-c", "dpapi:foreign-blob"),
                                    ("", "replace-me"),
                                    ("dpapi:generated-by-setup", "")):
            with self.subTest(api=bad_api, unlock=bad_unlock):
                env = self.root / ".env"
                env.write_text("SOME_OTHER=1\nMCP_API_KEY_PROTECTED=" + bad_api + "\n"
                               + bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR + "=" + bad_unlock + "\n",
                               encoding="utf-8")
                bootstrap.step_gen_env()
                api = bootstrap._read_env_value("MCP_API_KEY_PROTECTED")
                unlock = bootstrap._read_env_value(bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR)
                self.assertTrue(bootstrap._secret_is_usable(api), "API key was not re-made")
                self.assertTrue(bootstrap._secret_is_usable(unlock), "unlock password was not re-made")
                self.assertNotEqual(api, bad_api)
                # Copilot Studio still holds the old key: the marker lets quickstart.bat refuse to
                # skip STEP 5 and start_all keep saying so. It holds no secret.
                marker = self.root / ".setup" / "bearer_remade"
                self.assertTrue(marker.exists(), "no marker that the key was re-made")
                self.assertNotIn(api, marker.read_text(encoding="ascii"))
                marker.unlink()
                self.assertIn("SOME_OTHER=1", env.read_text(encoding="utf-8-sig"))
                backup = self.root / ".env.before-secret-repair"
                self.assertTrue(backup.exists(), "the old .env was not kept aside")
                self.assertIn(bad_api, backup.read_text(encoding="utf-8-sig"))
                backup.unlink()
                with mock.patch.object(bootstrap, "_count_tools_via_subprocess", return_value=1):
                    bootstrap.step_verify()   # the repaired file passes the check that failed


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
        api = bootstrap._read_env_value("MCP_API_KEY_PROTECTED")
        self.assertTrue(api and api.startswith("dpapi:"))
        self.assertTrue(bootstrap._read_env_value(bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR))


class GenEnvUnexpectedFailureTests(unittest.TestCase):
    """B1: an UNCAUGHT exception from gen_env must become a resumable StepError that says to
    re-run quickstart.bat. Every write in that step goes through tools.secret_store's DPAPI
    protect_secret and scripts/env_file's temp-file-plus-rename, so a locked or read-only .env --
    or a DPAPI refusal -- raised straight out of run_all as a traceback, and the operator got a
    stack trace with no instruction while the one thing they may run is quickstart.bat."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_root = bootstrap.ROOT
        bootstrap.ROOT = self.root

    def tearDown(self):
        bootstrap.ROOT = self._orig_root
        self.tmp.cleanup()

    def test_an_oserror_from_protecting_a_secret_becomes_a_step_error(self):
        # No .env yet, so this is the create path; it protects the minted secrets with DPAPI
        # first thing, which is where OSError comes from on a real machine.
        with mock.patch.object(bootstrap, "protect_secret",
                               side_effect=OSError("DPAPI refused")):
            with self.assertRaises(bootstrap.StepError) as ctx:
                bootstrap.step_gen_env()
        msg = str(ctx.exception)
        self.assertIn("quickstart.bat", msg)
        # The TYPE and the message survive: they are what says which file or handle is at fault.
        self.assertIn("OSError", msg)
        self.assertIn("DPAPI refused", msg)

    def test_a_locked_env_becomes_a_step_error_not_a_traceback(self):
        # A DIRECTORY where .env should be: opening it for reading raises OSError on Windows and
        # on POSIX alike, which is the shape of "another program is holding .env".
        (self.root / ".env").mkdir()
        with self.assertRaises(bootstrap.StepError) as ctx:
            bootstrap.step_gen_env()
        self.assertIn("quickstart.bat", str(ctx.exception))

    def test_the_guard_does_not_reword_a_step_error_or_an_action_needed(self):
        # ActionNeeded and StepError already carry a readable instruction; the guard exists to
        # explain the exceptions that carry none, not to replace the ones that do.
        @bootstrap._step_errors_are_actionable("Testing")
        def step(exc):
            raise exc

        with self.assertRaises(bootstrap.StepError) as ctx:
            step(bootstrap.StepError("its own message"))
        self.assertEqual(str(ctx.exception), "its own message")
        with self.assertRaises(bootstrap.ActionNeeded):
            step(bootstrap.ActionNeeded("sign in to Microsoft"))


class VerifyMarksTheVenvForRebuildTests(unittest.TestCase):
    """B2: when main.py will not import, the .venv may be half-broken and a plain re-run repeats
    the failure -- its python still runs, so ensure_venv calls it healthy and skips it, and
    install_deps finds requirements.txt satisfied and does nothing. The venv is moved aside to
    .venv.broken-<timestamp> and the ensure_venv/install_deps checkpoints are cleared, so the
    NEXT run of quickstart.bat rebuilds it -- and the error message says exactly that."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_root = bootstrap.ROOT
        self._orig_vpy = bootstrap.VENV_PYTHON
        self._orig_sf = bootstrap.STATE_FILE
        bootstrap.ROOT = self.root
        self.venv = self.root / ".venv"
        self.vpy = self.venv / "Scripts" / "python.exe"
        bootstrap.VENV_PYTHON = self.vpy
        self.vpy.parent.mkdir(parents=True, exist_ok=True)
        self.vpy.write_text("python", encoding="utf-8")
        (self.root / ".env").write_text(
            "MCP_API_KEY_PROTECTED=dpapi:test-api\n"
            + bootstrap.UNLOCK_PASSWORD_PROTECTED_VAR + "=dpapi:test-unlock\n",
            encoding="utf-8")
        self.state_file = self.root / ".setup" / "state.json"
        bootstrap.STATE_FILE = self.state_file
        self.state = None

    def tearDown(self):
        bootstrap.ROOT = self._orig_root
        bootstrap.VENV_PYTHON = self._orig_vpy
        bootstrap.STATE_FILE = self._orig_sf
        self.tmp.cleanup()

    def _build_done(self):
        """The state a previously successful run leaves behind."""
        self.state = {"done": {n: True for n in ("ensure_venv", "install_deps", "gen_env",
                                                "verify")}}
        bootstrap.save_state(self.state, self.state_file)
        return self.state

    def _verify(self, count):
        """step_verify with the .env key check and the child import both answered for us; the
        DPAPI usability check is not what these tests are about."""
        with mock.patch.object(bootstrap, "_secret_is_usable", return_value=True), \
                mock.patch.object(bootstrap, "_load_dotenv_into_env", lambda _p: None), \
                mock.patch.object(bootstrap, "_count_tools_via_subprocess", return_value=count):
            bootstrap.step_verify(state=self.state, state_file=self.state_file)

    def _aside_dirs(self):
        return sorted(p.name for p in self.root.glob(".venv.broken-*"))

    def test_an_import_failure_moves_the_venv_aside_and_clears_the_checkpoints(self):
        self._build_done()
        with self.assertRaises(bootstrap.StepError) as ctx:
            self._verify(None)
        msg = str(ctx.exception)

        self.assertFalse(self.venv.exists(), "the unusable .venv is still in place")
        self.assertFalse(self.vpy.exists())
        aside = self._aside_dirs()
        self.assertEqual(len(aside), 1, "the venv was not moved aside exactly once")
        self.assertTrue(aside[0].startswith(".venv.broken-"), aside[0])
        # RENAMED, NOT DELETED: what was wrong with it is still on disk to look at.
        self.assertTrue((self.root / aside[0] / "Scripts" / "python.exe").exists())

        # THE MESSAGE SAYS EXACTLY THAT THE NEXT RUN REBUILDS IT.
        self.assertIn("quickstart.bat", msg)
        self.assertIn("rebuilds .venv from scratch", msg)
        self.assertIn(aside[0], msg)

        # THE CHECKPOINTS ARE CLEARED IN THE SAVED STATE, not merely in memory.
        saved = bootstrap.load_state(self.state_file)
        self.assertFalse(bootstrap.is_done(saved, "ensure_venv"))
        self.assertFalse(bootstrap.is_done(saved, "install_deps"))

        # AND THE PROBE THAT USED TO SKIP IT NO LONGER CALLS IT HEALTHY (the loop this removes).
        self.assertFalse(bootstrap._venv_is_healthy())

    def test_only_the_projects_own_venv_is_touched(self):
        self._build_done()
        keep = self.root / "keep-me"
        keep.mkdir()
        (keep / "file.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(bootstrap.StepError):
            self._verify(None)
        self.assertTrue((keep / "file.txt").exists(), "a directory outside .venv was moved")
        self.assertEqual(len(self._aside_dirs()), 1)

    def test_no_directory_named_venv_is_ever_taken(self):
        # The one thing this must never do is take something that is not the project's .venv.
        self.venv.rename(self.root / "moved-away-by-the-test")
        elsewhere = self.root / "not-a-venv"
        elsewhere.mkdir()
        (elsewhere / "python.exe").write_text("python", encoding="utf-8")
        bootstrap.VENV_PYTHON = elsewhere / "python.exe"
        self.assertIsNone(bootstrap._venv_dir_for_rebuild())

    def test_a_verify_timeout_keeps_the_venv(self):
        """D26, unchanged: running out of time says nothing about the install, so the packages
        and the venv are KEPT. Only a real import FAILURE marks the environment for a rebuild."""
        self._build_done()
        with self.assertRaises(bootstrap.VerifyTimedOut):
            self._verify(bootstrap._IMPORT_TIMED_OUT)
        self.assertTrue(self.venv.exists())
        self.assertEqual(self._aside_dirs(), [])
        saved = bootstrap.load_state(self.state_file)
        self.assertTrue(bootstrap.is_done(saved, "install_deps"))

    def test_a_healthy_import_leaves_the_venv_and_the_checkpoints_alone(self):
        self._build_done()
        self._verify(8)
        self.assertTrue(self.venv.exists())
        self.assertEqual(self._aside_dirs(), [])
        saved = bootstrap.load_state(self.state_file)
        self.assertTrue(bootstrap.is_done(saved, "ensure_venv"))
        self.assertTrue(bootstrap.is_done(saved, "install_deps"))

    def test_a_venv_that_can_neither_be_moved_nor_removed_still_says_what_to_do(self):
        # A handle open inside the tree refuses both the rename and the delete. The checkpoints
        # are cleared all the same, and the message names the situation instead of pretending.
        self._build_done()
        with mock.patch.object(bootstrap, "_VENV_RETIRE_ATTEMPTS", 1), \
                mock.patch.object(bootstrap.Path, "rename",
                                  side_effect=PermissionError("in use")), \
                mock.patch.object(bootstrap.shutil, "rmtree",
                                  side_effect=OSError("in use")):
            with self.assertRaises(bootstrap.StepError) as ctx:
                self._verify(None)
        msg = str(ctx.exception)
        self.assertIn("could not be moved aside or removed", msg)
        self.assertIn("quickstart.bat", msg)
        self.assertTrue(self.venv.exists(), "the venv was removed after the move failed")
        saved = bootstrap.load_state(self.state_file)
        self.assertFalse(bootstrap.is_done(saved, "ensure_venv"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
