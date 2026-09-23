# -*- coding: utf-8 -*-
"""bootstrap.py fixes from the new-PC install-path review (2026-09-24), each driven, not read.

  D6  an existing .env gets every key .env.example defines and it lacks; nothing it has is
      changed; a key the user commented out stays commented.
  D5  install_deps records the requirements.txt it satisfied; a different file clears it.
  D8  a venv on a too-old Python is not "usable"; bootstrap itself refuses a too-old Python.
  D20 an existing venv that cannot run clears the checkpoints that claim it works.
  D26 a verify import that only TIMED OUT does not clear install_deps; the import code survives
      an apostrophe in the install path (D23).
  D1  freshly minted secrets are framed, repeated at the end of the run, and name the command
      that shows them again -- and never reach the transcript.
  D10 a pip failure on a proxied network talks about the proxy.
  D28 .env writes go through the atomic writer.

The steps that call DPAPI (gen_env's secret minting) are Windows-only and skip elsewhere; the
Windows CI job runs this file.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import bootstrap as B  # noqa: E402
import env_file  # noqa: E402
from tools import childproc  # noqa: E402

needs_dpapi = pytest.mark.skipif(os.name != "nt", reason="gen_env mints with DPAPI; Windows only")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake repo root holding the REAL .env.example, with the transcript redirected."""
    monkeypatch.setattr(B, "ROOT", tmp_path)
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "bootstrap.log")
    (tmp_path / ".env.example").write_bytes((REPO / ".env.example").read_bytes())
    monkeypatch.setattr(B, "_MINTED_THIS_RUN", [])
    return tmp_path


def _active(text):
    return env_file.active_keys(text)


# ---- D6 ------------------------------------------------------------------------------------

@needs_dpapi
def test_an_env_made_before_bootstrap_gets_every_template_key(repo):
    """The D6 scenario exactly: configure_env.ps1 created .env holding only the agent URL."""
    env = repo / ".env"
    env.write_text("MCP_IMPL_AGENT_URL=https://example.invalid/agent\r\n", encoding="utf-8")
    B.step_gen_env()
    text = env.read_text(encoding="utf-8")
    want = _active((REPO / ".env.example").read_text(encoding="utf-8-sig"))
    want.discard("MCP_UNLOCK_PASSWORD")          # stored in its protected form instead
    have = _active(text)
    assert want <= have, "template keys still missing: %s" % sorted(want - have)
    for k in ("MCP_ALLOWED_BASE", "MCP_TOOL_MAP", "MCP_TOOL_MAP_MAX", "MCP_UNLOCK_TTL_DAYS"):
        assert k in have, k
    assert "MCP_ALLOWED_BASE=~" in text.splitlines()
    assert B.UNLOCK_PASSWORD_PROTECTED_VAR in have
    assert "MCP_IMPL_AGENT_URL=https://example.invalid/agent" in text.splitlines()
    # Nothing but the backfill and the two secrets was added.
    assert have - want - {"MCP_IMPL_AGENT_URL", B.UNLOCK_PASSWORD_PROTECTED_VAR} == set()


def test_present_values_are_never_overwritten_and_commented_keys_stay_commented():
    current = ("MCP_ALLOWED_BASE=*\n"
               "# MCP_TOOL_MAP=1\n"
               "MCP_TOOL_MAP_MAX=20\n"
               "MCP_API_KEY=keep\n")
    example = (REPO / ".env.example").read_text(encoding="utf-8-sig")
    lines, left = B.missing_template_lines(current, example)
    keys = [l.split("=", 1)[0] for l in lines]
    assert "MCP_ALLOWED_BASE" not in keys, "a present value would be overwritten"
    assert "MCP_TOOL_MAP_MAX" not in keys
    assert "MCP_TOOL_MAP" not in keys and left == ["MCP_TOOL_MAP"], \
        "a key the user commented out was re-enabled"
    assert "MCP_API_KEY" not in keys and "MCP_UNLOCK_PASSWORD" not in keys, \
        "a template PLACEHOLDER secret would be copied in"
    assert "MCP_UNLOCK_TTL_DAYS=30" in lines
    assert len(keys) == len(set(keys))


def test_without_a_template_the_fallback_still_scopes_the_file_tools():
    lines, _ = B.missing_template_lines("", None)
    assert "MCP_ALLOWED_BASE=~" in lines and "MCP_TOOL_MAP=1" in lines


@needs_dpapi
def test_the_backfill_is_written_atomically(repo):
    env = repo / ".env"
    env.write_text("X=1\n", encoding="utf-8")
    seen = []
    real = env_file.atomic_write_text

    def spy(path, text, *a, **k):
        seen.append(Path(path).name)
        return real(path, text, *a, **k)

    with mock.patch.object(B.env_file, "atomic_write_text", spy):
        B.step_gen_env()
    assert seen == [".env"]


# ---- D5 ------------------------------------------------------------------------------------

def test_a_changed_requirements_file_clears_install_deps(tmp_path, monkeypatch):
    req = tmp_path / "requirements.txt"
    req.write_text("fastmcp>=3\n", encoding="utf-8")
    monkeypatch.setattr(B, "REQUIREMENTS", req)
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    state_file = tmp_path / "state.json"
    state = {"done": {"install_deps": True}, B.DEPS_HASH_KEY: B.requirements_hash(req)}
    B.save_state(state, state_file)

    calls = []
    steps = [("install_deps", lambda: calls.append("install_deps"))]
    # Unchanged -> skipped.
    with mock.patch.object(B, "_venv_usable", return_value=True), \
            mock.patch.object(B, "VENV_PYTHON", req):          # "exists"
        assert B.run_all(steps=steps, state_file=state_file) == 0
    assert calls == []
    # Changed -> runs again.
    req.write_text("fastmcp>=3\nnewdep>=1\n", encoding="utf-8")
    with mock.patch.object(B, "_venv_usable", return_value=True), \
            mock.patch.object(B, "VENV_PYTHON", req):
        assert B.run_all(steps=steps, state_file=state_file) == 0
    assert calls == ["install_deps"]


def test_line_endings_alone_do_not_count_as_a_change(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"x\ny\n")
    b.write_bytes(b"x\r\ny\r\n")
    assert B.requirements_hash(a) == B.requirements_hash(b)


def test_a_state_without_a_hash_reinstalls_once():
    assert B.deps_are_stale({"done": {"install_deps": True}})
    assert not B.deps_are_stale({"done": {}})


def test_a_successful_install_records_the_hash(tmp_path, monkeypatch):
    req = tmp_path / "requirements.txt"
    req.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(B, "REQUIREMENTS", req)
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    ok = subprocess.CompletedProcess([], 0, "42", "")      # the verify import's tool count
    state = {"done": {}}
    with mock.patch.object(B.subprocess, "call", return_value=0), \
            mock.patch("tools.childproc.run", return_value=ok):
        B.step_install_deps(state=state, state_file=tmp_path / "state.json")
    assert state[B.DEPS_HASH_KEY] == B.requirements_hash(req)


def test_a_failed_install_records_nothing(tmp_path, monkeypatch):
    req = tmp_path / "requirements.txt"
    req.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(B, "REQUIREMENTS", req)
    monkeypatch.setattr(B, "_broken_distributions", lambda py: [])
    state = {"done": {}}
    with mock.patch.object(B.subprocess, "call", return_value=1):
        with pytest.raises(B.StepError):
            B.step_install_deps(state=state, state_file=tmp_path / "state.json")
    assert B.DEPS_HASH_KEY not in state


def test_check_deps_cli(tmp_path, monkeypatch):
    req = tmp_path / "requirements.txt"
    req.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(B, "REQUIREMENTS", req)
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    sf = tmp_path / "state.json"
    monkeypatch.setattr(B, "STATE_FILE", sf)
    monkeypatch.setattr(B, "load_state", lambda state_file=sf: B.json.loads(sf.read_text()))
    B.save_state({"done": {"install_deps": True}, B.DEPS_HASH_KEY: B.requirements_hash(req)}, sf)
    assert B.main(["--check-deps"]) == 0
    req.write_text("x\ny\n", encoding="utf-8")
    assert B.main(["--check-deps"]) == 3


# ---- D5b: the daily path brings the venv up to date itself ---------------------------------
# --check-deps used to compare the stamp only, so a state.json written before the stamp existed
# said "run setup.bat" on every start forever. It now asks whether the venv SATISFIES
# requirements.txt, records the stamp when it does, and --sync-deps installs when it does not.

#: Requirement lines this very interpreter satisfies (pytest is running it; pytest needs
#: packaging), so the REAL probe runs against real installed metadata, offline.
_SATISFIED_HERE = "pytest\npackaging\n"


def _no_pip(*a, **k):
    raise AssertionError("pip was run for a venv that already satisfies requirements.txt: %r" % (a,))


@pytest.fixture
def deps_repo(tmp_path, monkeypatch):
    req = tmp_path / "requirements.txt"
    monkeypatch.setattr(B, "REQUIREMENTS", req)
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "bootstrap.log")
    monkeypatch.setattr(B, "STATE_FILE", tmp_path / ".setup" / "state.json")
    monkeypatch.setattr(B, "DEPS_LOG_DIR", tmp_path / ".setup" / "logs")
    # The real probe, in-process: the interpreter running pytest IS the environment under test.
    monkeypatch.setattr(B, "_running_in_project_venv", lambda: True)
    # The RECORD integrity check has tests of its own below; here nothing is broken.
    monkeypatch.setattr(B, "_broken_distributions", lambda py: [])
    return tmp_path


def test_the_probe_reads_real_metadata(deps_repo):
    req = deps_repo / "requirements.txt"
    req.write_text(_SATISFIED_HERE + "# a comment\n\n", encoding="utf-8")
    assert B.unsatisfied_requirements(req) == []
    req.write_text("pytest>=999\nno-such-package-xyz\n", encoding="utf-8")
    got = B.unsatisfied_requirements(req)
    assert any(p.startswith("no-such-package-xyz is not installed") for p in got), got
    assert any("pytest>=999 is required" in p for p in got), got
    # Anything it cannot judge leads to the install, never to a stamp.
    req.write_text("--index-url https://example.invalid/simple\npytest\n", encoding="utf-8")
    assert B.unsatisfied_requirements(req)


def test_no_stamp_but_satisfied_is_recorded_without_an_install(deps_repo, capsys):
    """(a) The owner's machine: install_deps done in July, no stamp, venv fine -> rc 0, the stamp
    is written, pip never runs, and the next check takes the fast path."""
    (deps_repo / "requirements.txt").write_text(_SATISFIED_HERE, encoding="utf-8")
    sf = B.STATE_FILE
    B.save_state({"done": {"install_deps": True, "ensure_venv": True}}, sf)
    with mock.patch.object(B.subprocess, "call", _no_pip):
        assert B.main(["--check-deps"]) == 0
    st = B.load_state(sf)
    assert st[B.DEPS_HASH_KEY] == B.requirements_hash()
    assert st["done"] == {"install_deps": True, "ensure_venv": True}, "anything but the stamp changed"
    assert "setup.bat" not in capsys.readouterr().out
    # The stamp now matches, so the probe is not even asked.
    with mock.patch.object(B, "venv_unsatisfied", side_effect=AssertionError("probed again")):
        assert B.main(["--check-deps"]) == 0
    # --sync-deps on the same no-stamp state: recorded, no pip.
    B.save_state({"done": {"install_deps": True}}, sf)
    with mock.patch.object(B.subprocess, "call", _no_pip):
        assert B.sync_deps() == 0
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("deps: recorded")
    assert B.load_state(sf)[B.DEPS_HASH_KEY] == B.requirements_hash()


def test_check_deps_names_what_is_missing_and_never_setup_bat(deps_repo, capsys):
    (deps_repo / "requirements.txt").write_text("no-such-package-xyz>=1\n", encoding="utf-8")
    B.save_state({"done": {"install_deps": True}}, B.STATE_FILE)
    assert B.main(["--check-deps"]) == 3
    out = capsys.readouterr().out
    assert "no-such-package-xyz is not installed" in out
    assert "setup.bat" not in out
    assert B.DEPS_HASH_KEY not in B.load_state(B.STATE_FILE), "stamped a venv that does not satisfy"


def _ok_import(*a, **k):
    return subprocess.CompletedProcess([], 0, "42", "")     # main.py imported; 42 tools


def test_unsatisfied_runs_the_install_step_and_records_it(deps_repo, capsys):
    """(b) A genuinely missing package: bootstrap's own install_deps step runs (pip -r, output
    to .setup/logs, no pip self-upgrade on the daily path), then the stamp and the flag."""
    (deps_repo / "requirements.txt").write_text("no-such-package-xyz>=1\n", encoding="utf-8")
    B.save_state({"done": {"install_deps": True}}, B.STATE_FILE)
    calls = []

    def fake_pip(cmd, **kw):
        calls.append((list(cmd), kw))
        kw["stdout"].write("Successfully installed no-such-package-xyz-1.0\n")
        return 0

    with mock.patch.object(B.subprocess, "call", fake_pip), \
            mock.patch("tools.childproc.run", _ok_import):
        assert B.sync_deps() == 0
    assert len(calls) == 1, "the daily path ran more than the one install: %r" % calls
    cmd, kw = calls[0]
    assert cmd[-2:] == ["-r", str(deps_repo / "requirements.txt")] and "--upgrade" not in cmd
    log_file = Path(kw["stdout"].name)
    assert log_file.parent == deps_repo / ".setup" / "logs"
    assert "Successfully installed" in log_file.read_text(encoding="utf-8")
    st = B.load_state(B.STATE_FILE)
    assert st[B.DEPS_HASH_KEY] == B.requirements_hash() and B.is_done(st, "install_deps")
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("deps: installed") and str(log_file) in last


def test_a_failed_install_says_what_failed_and_to_rerun_start_all(deps_repo, capsys):
    """(c) bootstrap's half: pip's own last ERROR line, the log path, start_all.bat -- and no
    setup.bat anywhere. Nothing is recorded."""
    (deps_repo / "requirements.txt").write_text("no-such-package-xyz>=1\n", encoding="utf-8")
    B.save_state({"done": {"install_deps": True}}, B.STATE_FILE)

    def failing_pip(cmd, **kw):
        kw["stdout"].write("Collecting no-such-package-xyz>=1\n"
                           "ERROR: Could not find a version that satisfies the requirement "
                           "no-such-package-xyz>=1 (from versions: none)\n"
                           "ERROR: No matching distribution found for no-such-package-xyz>=1\n")
        return 1

    with mock.patch.object(B.subprocess, "call", failing_pip):
        assert B.sync_deps() == 1
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("deps: failed: ")
    assert "No matching distribution found for no-such-package-xyz>=1" in last
    assert str(deps_repo / ".setup" / "logs") in last and "deps_install_" in last
    assert "start_all.bat" in last and "setup.bat" not in last
    assert B.DEPS_HASH_KEY not in B.load_state(B.STATE_FILE)


# ---- the fastmcp-slim breakage (2026-09-24) ------------------------------------------------
# fastmcp 2.14.7 -> 3.4.7 moved the fastmcp/ package into a new distribution, fastmcp-slim. pip
# installed fastmcp-slim, then uninstalled fastmcp 2.14.7 by ITS RECORD -- deleting
# fastmcp/server/server.py & co. that fastmcp-slim had just written. pip exited 0 and
# `import fastmcp` still worked; the server did not.

def _fake_dist(site: Path, name: str, version: str, files: dict, record_extra=()):
    """A minimal installed distribution: <name>-<version>.dist-info with METADATA and RECORD,
    plus the files in `files` (relpath -> text) actually written. `record_extra` are RECORD
    entries whose files are NOT written -- what the uninstall of the old owner deleted."""
    di = site / ("%s-%s.dist-info" % (name.replace("-", "_"), version))
    di.mkdir(parents=True)
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, version),
                                 encoding="utf-8")
    for rel, text in files.items():
        p = site / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    rows = list(files) + list(record_extra) + [di.name + "/METADATA", di.name + "/RECORD"]
    (di / "RECORD").write_text("".join("%s,,\n" % r for r in rows), encoding="utf-8")


def test_the_record_check_finds_exactly_the_distribution_that_lost_files(tmp_path):
    site = tmp_path / "site-packages"
    _fake_dist(site, "fastmcp-slim", "3.4.7",
               {"fastmcp/__init__.py": "", "fastmcp/utilities/x.py": ""},
               record_extra=["fastmcp/server/server.py", "fastmcp/server/dependencies.py"])
    _fake_dist(site, "intact-pkg", "1.0", {"intact_pkg/__init__.py": ""},
               record_extra=["intact_pkg/__pycache__/__init__.cpython-310.pyc"])  # caches may go
    got = B.distributions_missing_files([str(site)])
    assert [spec for spec, _ in got] == ["fastmcp-slim==3.4.7"], got
    assert got[0][1].startswith("fastmcp/server/"), got


def _install_with(monkeypatch, tmp_path, broken_seq, import_result=(42, "")):
    """Run the REAL install step with pip, the RECORD check and the main.py import stubbed.
    broken_seq: what _broken_distributions returns on each call (before, after, after repair)."""
    req = tmp_path / "requirements.txt"
    req.write_text("fastmcp>=3.4.7\n", encoding="utf-8")
    monkeypatch.setattr(B, "REQUIREMENTS", req)
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "bootstrap.log")
    seq = list(broken_seq)
    monkeypatch.setattr(B, "_broken_distributions", lambda py: seq.pop(0) if seq else [])
    imports = []
    monkeypatch.setattr(B, "_import_main_in_venv",
                        lambda for_install=False: (imports.append(for_install), import_result)[1])
    calls = []

    def fake_pip(cmd, **kw):
        calls.append(list(cmd))
        return 0

    state = {"done": {}}
    with mock.patch.object(B.subprocess, "call", fake_pip):
        try:
            B.step_install_deps(state=state, state_file=tmp_path / ".setup" / "state.json",
                                upgrade_pip=False)
            err = None
        except B.StepError as e:
            err = e
    return calls, imports, state, err


_OLD_GAP = ("antlr4-python3-runtime==4.7.2", "MANIFEST.in")          # harmless, pre-existing
_SLIM = ("fastmcp-slim==3.4.7", "fastmcp/server/server.py")


def test_a_package_the_upgrade_broke_is_reinstalled_and_then_verified(monkeypatch, tmp_path):
    calls, imports, state, err = _install_with(
        monkeypatch, tmp_path, [[_OLD_GAP], [_OLD_GAP, _SLIM], [_OLD_GAP]])
    assert err is None, err
    assert len(calls) == 2, calls
    assert calls[0][-2:] == ["-r", str(tmp_path / "requirements.txt")]
    repair = calls[1]
    assert "--force-reinstall" in repair and "--no-deps" in repair
    assert repair[-1] == "fastmcp-slim==3.4.7", "not exactly the broken distribution, at its version"
    assert "antlr4-python3-runtime==4.7.2" not in repair, "reinstalled an old gap this install never touched"
    assert imports == [True], "the result was not verified by importing main.py"
    assert state[B.DEPS_HASH_KEY]


def test_a_package_the_repair_cannot_fix_fails_by_name(monkeypatch, tmp_path):
    calls, imports, state, err = _install_with(
        monkeypatch, tmp_path, [[], [_SLIM], [_SLIM]])
    assert isinstance(err, B.StepError)
    assert "fastmcp-slim==3.4.7" in str(err) and "fastmcp/server/server.py" in str(err)
    assert B.DEPS_HASH_KEY not in state


def test_the_install_is_verified_by_importing_main_not_fastmcp(monkeypatch, tmp_path):
    """The exact production symptom: the packages import, main.py does not."""
    calls, imports, state, err = _install_with(
        monkeypatch, tmp_path, [[], [], []],
        import_result=(None, "ImportError: cannot import name 'FastMCP' from 'fastmcp' (unknown location)"))
    assert isinstance(err, B.StepError)
    assert "cannot import name 'FastMCP'" in str(err) and "main.py" in str(err)
    assert B.DEPS_HASH_KEY not in state


def test_the_install_verify_import_has_a_key_even_before_gen_env(monkeypatch, tmp_path):
    """install_deps runs before gen_env on a first install, and main.py reads MCP_API_KEY at
    import: the child gets a placeholder, and nothing outside the child changes."""
    monkeypatch.setattr(B, "ROOT", tmp_path)                   # no .env here
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, "7\n", "")

    with mock.patch("tools.childproc.run", fake_run):
        assert B._import_main_in_venv(for_install=True) == (7, "")
    assert seen.get("MCP_API_KEY") == "install-verify-placeholder"
    assert "MCP_API_KEY" not in os.environ


def test_a_proxy_password_never_reaches_the_failure_text(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:s3cret@proxy.example:8080")
    msg = B._pip_failure_message(B.RERUN_START_ALL, "ERROR: ProxyError http://alice:s3cret@proxy.example:8080")
    assert "s3cret" not in msg and "proxy.example:8080" in msg and "start_all.bat" in msg


#: A process that runs the REAL bootstrap.main with pip and the probe stubbed: "installing"
#: takes PIP_SLEEP seconds and makes the probe report satisfied afterwards, exactly what a real
#: install does to the next reader. Every pip run is logged with its start and end time.
_SYNC_DRIVER = r'''
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(os.environ["DEPS_ROOT"])
sys.path.insert(0, os.environ["DEPS_REPO"])
sys.path.insert(0, os.path.join(os.environ["DEPS_REPO"], "scripts"))
import bootstrap as B
B.STATE_DIR = root / ".setup"
B.STATE_FILE = root / ".setup" / "state.json"
B.DEPS_LOG_DIR = root / ".setup" / "logs"
B.TRANSCRIPT = root / ".setup" / "bootstrap.log"
B.REQUIREMENTS = root / "requirements.txt"
flag = root / "installed.flag"
B.venv_unsatisfied = lambda req=None: [] if flag.exists() else ["no-such-package-xyz is not installed"]
def fake_pip(cmd, **kw):
    t0 = time.time()
    time.sleep(float(os.environ.get("PIP_SLEEP", "0")))
    flag.touch()
    with open(root / "pip_runs.jsonl", "a") as fh:
        fh.write(json.dumps([os.getpid(), t0, time.time()]) + "\n")
    return 0
B.subprocess.call = fake_pip
B._broken_distributions = lambda py: []
import tools.childproc
tools.childproc.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "42", "")
print("started %f" % time.time(), flush=True)
sys.exit(B.main(sys.argv[1:]))
'''


def _spawn_sync(tmp_path, env):
    drv = tmp_path / "sync_driver.py"
    drv.write_text(_SYNC_DRIVER, encoding="utf-8")
    return subprocess.Popen([sys.executable, str(drv), "--sync-deps"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_two_syncs_at_once_install_once_one_after_the_other(tmp_path):
    """(d) The Startup .lnk and the scheduled Task start 15 s apart; setup can run beside
    either. Two --sync-deps at once: one installs, the other WAITS on the lock, then re-reads
    the state that install wrote and does nothing. Never two pips in one .venv."""
    (tmp_path / "requirements.txt").write_text("no-such-package-xyz>=1\n", encoding="utf-8")
    B.save_state({"done": {"install_deps": True}}, tmp_path / ".setup" / "state.json")
    env = dict(os.environ, DEPS_ROOT=str(tmp_path), DEPS_REPO=str(REPO), PIP_SLEEP="3")
    a = _spawn_sync(tmp_path, env)
    b = _spawn_sync(tmp_path, env)
    outs = [childproc.decode(p.communicate(timeout=120)[0]) for p in (a, b)]
    assert a.returncode == 0 and b.returncode == 0, outs
    runs = [json.loads(l) for l in (tmp_path / "pip_runs.jsonl").read_text().splitlines()]
    assert len(runs) == 1, "both copies ran pip into the same venv: %r\n%s" % (runs, outs)
    verdicts = sorted(o.strip().splitlines()[-1].split()[1] for o in outs)
    assert verdicts == ["installed", "ok"], outs
    # The one that did not install really waited: it started before the install ended and
    # finished after it (a copy that skipped the lock would have read the unfinished state).
    loser = outs[0] if outs[0].strip().splitlines()[-1] == "deps: ok" else outs[1]
    started = float(loser.split("started ", 1)[1].split()[0])
    assert started < runs[0][2], "the second copy only started after the install: no overlap tested"
    st = B.load_state(tmp_path / ".setup" / "state.json")
    assert st[B.DEPS_HASH_KEY] == B.requirements_hash(tmp_path / "requirements.txt")


_HOLD_LOCK = r'''
import sys, time
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/scripts")
import bootstrap as B
with B.install_lock(sys.argv[2]):
    print("held", flush=True)
    time.sleep(float(sys.argv[3]))
'''


def test_a_lock_held_too_long_is_a_start_all_failure_not_a_hang(tmp_path, monkeypatch, capsys):
    (tmp_path / "requirements.txt").write_text("no-such-package-xyz>=1\n", encoding="utf-8")
    monkeypatch.setattr(B, "REQUIREMENTS", tmp_path / "requirements.txt")
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "bootstrap.log")
    monkeypatch.setattr(B, "INSTALL_LOCK_TIMEOUT_SEC", 1)
    sf = tmp_path / ".setup" / "state.json"
    B.save_state({"done": {"install_deps": True}}, sf)
    holder_py = tmp_path / "hold.py"
    holder_py.write_text(_HOLD_LOCK, encoding="utf-8")
    holder = subprocess.Popen([sys.executable, str(holder_py), str(REPO), str(sf.parent), "20"],
                              stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == b"held"
        with mock.patch.object(B.subprocess, "call", _no_pip):
            assert B.sync_deps(state_file=sf) == 1
    finally:
        holder.kill()
        holder.wait()
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("deps: failed: another dependency install")
    assert "start_all.bat" in last and "setup.bat" not in last
    # The kernel dropped the killed holder's lock: the next caller is not wedged.
    with B.install_lock(sf.parent, timeout=5):
        pass


# ---- D8 / D20 ------------------------------------------------------------------------------

def test_a_venv_on_a_too_old_python_is_not_usable():
    with mock.patch.object(B, "_venv_version", return_value=(3, 9)):
        assert not B._venv_usable()
    with mock.patch.object(B, "_venv_version", return_value=(3, 10)):
        assert B._venv_usable()
    with mock.patch.object(B, "_venv_version", return_value=None):
        assert not B._venv_usable()


def test_a_too_old_venv_is_rebuilt_not_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    with mock.patch.object(B, "_venv_runs", return_value=True), \
            mock.patch.object(B, "_venv_version", return_value=(3, 9)), \
            mock.patch.object(B, "_venv_has_pip", return_value=True):
        assert B._venv_is_healthy() is False


def test_an_existing_but_unusable_venv_clears_the_build_checkpoints(tmp_path, monkeypatch):
    """D20: the venv EXISTS, so the missing-venv guard does not fire; before this fix the
    flags were then trusted and ensure_venv -- the step that repairs it -- never ran."""
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    sf = tmp_path / "state.json"
    B.save_state({"done": {n: True for n in ("ensure_venv", "install_deps", "verify")},
                  B.DEPS_HASH_KEY: B.requirements_hash()}, sf)
    present = tmp_path / "python.exe"
    present.write_bytes(b"not a program")
    calls = []
    steps = [(n, (lambda n=n: calls.append(n))) for n in ("ensure_venv", "install_deps", "verify")]
    with mock.patch.object(B, "VENV_PYTHON", present):
        assert B.run_all(steps=steps, state_file=sf) == 0
    assert calls == ["ensure_venv", "install_deps", "verify"]


def test_the_real_probe_rejects_a_file_that_is_not_a_program(tmp_path):
    fake = tmp_path / "python.exe"
    fake.write_bytes(b"MZ-truncated")
    with mock.patch.object(B, "VENV_PYTHON", fake):
        assert B._venv_version() is None
        assert not B._venv_usable()


def test_the_real_probe_reads_a_working_interpreter():
    with mock.patch.object(B, "VENV_PYTHON", Path(sys.executable)):
        assert B._venv_version() == tuple(sys.version_info[:2])


def test_bootstrap_refuses_a_too_old_python_before_importing_anything():
    """Run the real file with sys.version_info forced to 3.9: it must say what to do and stop
    with 2, instead of building a venv the requirements cannot be installed into."""
    # --status, so that if the guard ever stops firing this child only PRINTS the step list --
    # without it a regressed guard ran a real install (venv + pip) from this checkout, which is
    # how the mutation check of this very test found the hazard.
    code = ("import sys, runpy; sys.version_info = (3, 9, 18, 'final', 0); "
            "sys.argv = ['bootstrap.py', '--status']; "
            "runpy.run_path(%r, run_name='__main__')" % str(HERE / "bootstrap.py"))
    r = childproc.run([sys.executable, "-c", code], cwd=str(REPO), timeout=120)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "ACTION NEEDED" in r.stdout and "3.10" in r.stdout and "setup.bat" in r.stdout


# ---- D26 / D23 -----------------------------------------------------------------------------

def test_a_verify_timeout_keeps_the_installed_packages(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    sf = tmp_path / "state.json"
    B.save_state({"done": {"ensure_venv": True, "install_deps": True},
                  B.DEPS_HASH_KEY: B.requirements_hash()}, sf)

    def slow():
        raise B.VerifyTimedOut("timed out")

    steps = [("ensure_venv", lambda: None), ("install_deps", lambda: None), ("verify", slow)]
    with mock.patch.object(B, "VENV_PYTHON", tmp_path / "python.exe"):
        (tmp_path / "python.exe").write_bytes(b"x")
        with mock.patch.object(B, "_venv_usable", return_value=True):
            assert B.run_all(steps=steps, state_file=sf) == 1
    st = B.load_state(sf)
    assert B.is_done(st, "install_deps") and B.is_done(st, "ensure_venv"), \
        "a timeout cleared the install, so the next run reinstalls everything"


def test_a_real_verify_failure_still_clears_them(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    sf = tmp_path / "state.json"
    B.save_state({"done": {"ensure_venv": True, "install_deps": True},
                  B.DEPS_HASH_KEY: B.requirements_hash()}, sf)

    def broken():
        raise B.StepError("import failed")

    steps = [("ensure_venv", lambda: None), ("install_deps", lambda: None), ("verify", broken)]
    (tmp_path / "python.exe").write_bytes(b"x")
    with mock.patch.object(B, "VENV_PYTHON", tmp_path / "python.exe"), \
            mock.patch.object(B, "_venv_usable", return_value=True):
        assert B.run_all(steps=steps, state_file=sf) == 1
    assert not B.is_done(B.load_state(sf), "install_deps")


def test_the_import_timeout_is_reported_as_a_timeout(monkeypatch, tmp_path):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="python", timeout=1)

    monkeypatch.setattr("tools.childproc.run", raise_timeout)
    assert B._count_tools_via_subprocess() is B._IMPORT_TIMED_OUT
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    (tmp_path / ".env").write_text("MCP_API_KEY=abc\nMCP_UNLOCK_PASSWORD=x\n", encoding="utf-8")
    monkeypatch.setattr(B, "ROOT", tmp_path)
    with pytest.raises(B.VerifyTimedOut) as ei:
        B.step_verify()
    assert "KEPT" in str(ei.value) and str(B.VERIFY_IMPORT_TIMEOUT_S) in str(ei.value)


def test_the_timeout_is_well_above_the_measured_import_time():
    # Measured 8.7-10.2 s on the owner's PC (see VERIFY_IMPORT_TIMEOUT_S). A cap within 10x of
    # that is the loop D26 describes, waiting for a slow disk to trigger it.
    assert B.VERIFY_IMPORT_TIMEOUT_S >= 10 * 10.2


def test_the_verify_import_survives_an_apostrophe_in_the_install_path(tmp_path, monkeypatch):
    root = tmp_path / "O'Brien's repo"
    root.mkdir()
    (root / "main.py").write_text("TOOLS = [1, 2, 3]\nclass _M: pass\nmcp = _M()\n",
                                  encoding="utf-8")
    monkeypatch.setattr(B, "ROOT", root)
    monkeypatch.setattr(B, "VENV_PYTHON", root / "no-venv" / "python.exe")
    assert B._count_tools_via_subprocess() == 3


# ---- D1 ------------------------------------------------------------------------------------

def test_minted_secrets_are_framed_repeated_and_never_transcribed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "bootstrap.log")
    monkeypatch.setattr(B, "_MINTED_THIS_RUN", [])
    sf = tmp_path / "state.json"

    def mint():
        B._remember_and_show([("Unlock password", "pw-TESTONLY-1234")])

    def later():
        B.log("a lot of later output")

    assert B.run_all(steps=[("gen", mint), ("other", later)], state_file=sf) == 0
    out = capsys.readouterr().out
    assert out.count("pw-TESTONLY-1234") == 2, "shown once, then repeated at the end"
    assert out.rindex("pw-TESTONLY-1234") > out.index("a lot of later output"), \
        "the repeat must come AFTER everything else"
    assert "copilot_studio_values.bat" in out
    assert "#" * 40 in out
    log = (tmp_path / "bootstrap.log").read_text(encoding="utf-8")
    assert "pw-TESTONLY-1234" not in log


def test_the_repeat_also_happens_when_a_later_step_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "bootstrap.log")
    monkeypatch.setattr(B, "_MINTED_THIS_RUN", [])

    def mint():
        B._remember_and_show([("Unlock password", "pw-TESTONLY-5678")])

    def fail():
        raise B.StepError("boom")

    assert B.run_all(steps=[("gen", mint), ("x", fail)], state_file=tmp_path / "s.json") == 1
    out = capsys.readouterr().out
    assert out.count("pw-TESTONLY-5678") == 2
    assert out.rindex("pw-TESTONLY-5678") > out.index("FAILED at step")


def test_no_message_points_at_a_script_that_does_not_show_the_password():
    import inspect
    src = inspect.getsource(B)
    assert "Re-read it with scripts/copilot_studio_values.ps1" not in src
    assert "Re-read them with scripts/copilot_studio_values.ps1" not in src


@needs_dpapi
def test_a_bearer_minted_into_an_existing_env_is_shown(repo, capsys):
    """It replaces what Copilot Studio holds, so it must be re-pasted -- it used to be minted
    without a word."""
    (repo / ".env").write_text("MCP_UNLOCK_PASSWORD=keep\n", encoding="utf-8")
    B.step_gen_env()
    api = [l.split("=", 1)[1] for l in (repo / ".env").read_text(encoding="utf-8").splitlines()
           if l.startswith("MCP_API_KEY=")][0]
    assert api in capsys.readouterr().out


# ---- dev_tunnel: D4 access, D16 text, D7 carried .env, D29 names ----------------------------

class FakeDevtunnel:
    """Stands in for _dt_run: records every argv, answers from a table keyed by the argv."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, dt, *args):
        self.calls.append(" ".join(args))
        rc, out = self.answers.get(" ".join(args), (0, ""))
        return subprocess.CompletedProcess(list(args), rc, out, "")


def test_a_not_chosen_anonymous_grant_is_revoked_on_an_existing_tunnel(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    fake = FakeDevtunnel({
        "show t1": (0, "Tunnel ID : t1.jpe1\n https://t1-8000.jpe1.devtunnels.ms/"),
        "access list t1": (0, "  +Anonymous [connect]"),
        "access list t1 -p 8000": (0, "  (no entries)"),
    })
    monkeypatch.setattr(B, "_dt_run", fake)
    monkeypatch.setattr(B, "_anon_opt_in", lambda: False)
    monkeypatch.setattr(B, "_write_tunnel_to_env", lambda t, u: None)
    B._provision_dev_tunnel("dt", "t1")
    assert "access reset t1" in fake.calls, fake.calls
    assert "access reset t1 -p 8000" not in fake.calls, "a clean level was reset"
    assert not [c for c in fake.calls if "--anonymous" in c]


def test_an_unreadable_access_list_is_reset_and_a_failed_reset_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    fake = FakeDevtunnel({"access list t1 -p 8000": (1, "boom"),
                          "access reset t1 -p 8000": (1, "forbidden")})
    monkeypatch.setattr(B, "_dt_run", fake)
    with pytest.raises(B.StepError) as ei:
        B._revoke_anonymous_access("dt", "t1", 8000)
    assert "may still be in place" in str(ei.value)
    assert "access reset t1 -p 8000" in fake.calls


def test_a_chosen_anonymous_grant_is_asserted_at_both_levels_even_if_the_tunnel_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "TRANSCRIPT", tmp_path / "log")
    fake = FakeDevtunnel({"show t1": (0, "https://t1-8000.jpe1.devtunnels.ms/"),
                          "access create t1 --anonymous": (1, "Conflict: already exists")})
    monkeypatch.setattr(B, "_dt_run", fake)
    monkeypatch.setattr(B, "_anon_opt_in", lambda: True)
    monkeypatch.setattr(B, "_write_tunnel_to_env", lambda t, u: None)
    B._provision_dev_tunnel("dt", "t1")
    assert "access create t1 --anonymous" in fake.calls
    assert "access create t1 -p 8000 --anonymous" in fake.calls
    assert not [c for c in fake.calls if c.startswith("access reset")]


def test_no_message_tells_anyone_to_pass_a_tenant_id():
    import inspect
    assert "<your-tenant-id>" not in inspect.getsource(B)


def test_a_generated_name_is_not_identifying_for_a_user_named_pan(monkeypatch):
    """D29: "pan" is inside "companion", so every generated name read as identifying and was
    thrown away and regenerated as the same name on every run."""
    monkeypatch.setattr(B.getpass, "getuser", lambda: "pan")
    assert not B._is_identifying_tunnel_name("m365-copilot-companion-" + "0a1b2c3d")
    assert not B._is_identifying_tunnel_name("m365-copilot-companion")
    assert B._is_identifying_tunnel_name("pan-tunnel"), "a user-derived custom name must still count"


def _dev_tunnel_env(repo, lines):
    (repo / ".env").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))


def _env_text(repo):
    return env_file.read_text(repo / ".env")


def _run_dev_tunnel_step(monkeypatch, repo):
    seen = {}
    monkeypatch.setattr(B, "find_executable", lambda *a: "devtunnel")
    monkeypatch.setattr(B, "_devtunnel_logged_in", lambda dt: True)
    monkeypatch.setattr(B, "_anon_opt_in", lambda: False)
    monkeypatch.setattr(B, "_provision_dev_tunnel", lambda dt, t: seen.setdefault("tunnel", t))
    B.step_dev_tunnel()
    return seen.get("tunnel")


def test_a_carried_env_gives_up_its_tunnel_before_provisioning(repo, monkeypatch):
    """D7: the recorded name used to be re-provisioned and stamped as this machine's, so the
    same account hosted ONE tunnel from two PCs."""
    _dev_tunnel_env(repo, ["MCP_API_KEY=k", "MCP_TUNNEL_NAME=team-tunnel",
                           "MCP_TUNNEL_URL=https://team-tunnel-8000.jpe1.devtunnels.ms/",
                           "MCP_TUNNEL_HOST=some-other-pc", "MCP_UNLOCK_PASSWORD_PROTECTED=dpapi:x"])
    tunnel = _run_dev_tunnel_step(monkeypatch, repo)
    assert tunnel == "%s-%s" % (B.DEFAULT_TUNNEL_NAME, B._machine_suffix())
    text = _env_text(repo)
    assert "\n# MCP_TUNNEL_NAME=team-tunnel" in text and "\nMCP_TUNNEL_NAME=" not in text
    assert "\n# MCP_TUNNEL_HOST=some-other-pc" in text
    assert "\nMCP_UNLOCK_PASSWORD_PROTECTED=dpapi:x" in text, "a non-tunnel key was set aside"
    assert text.startswith("MCP_API_KEY=k\r\n") and "\n" not in text.replace("\r\n", ""), \
        "line endings or other lines were disturbed"


def test_an_unstamped_generated_name_from_another_machine_is_foreign(repo, monkeypatch):
    _dev_tunnel_env(repo, ["MCP_TUNNEL_NAME=m365-copilot-companion-deadbeef"])
    monkeypatch.setattr(B, "_machine_suffix", lambda: "0badf00d")
    monkeypatch.setattr(B, "_legacy_machine_suffix", lambda: "abcdef")
    assert "generated on another machine" in B._foreign_env_reason(_env_text(repo))
    assert _run_dev_tunnel_step(monkeypatch, repo) == "m365-copilot-companion-0badf00d"


def test_this_machines_own_stamps_are_not_foreign(repo, monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "LEGACYNAME")
    for stamp in (B._this_host(), "legacyname"):
        _dev_tunnel_env(repo, ["MCP_TUNNEL_NAME=my-custom", "MCP_TUNNEL_HOST=" + stamp])
        assert B._foreign_env_reason(_env_text(repo)) == "", stamp
    _dev_tunnel_env(repo, ["MCP_TUNNEL_NAME=my-custom"])        # no stamp, custom name
    before = (repo / ".env").read_bytes()
    assert B._foreign_env_reason(_env_text(repo)) == ""
    assert _run_dev_tunnel_step(monkeypatch, repo) == "my-custom"
    assert (repo / ".env").read_bytes() == before, "a .env that is not foreign was rewritten"


def test_a_stubbed_reader_cannot_make_it_rewrite_a_file_it_did_not_read(repo, monkeypatch):
    """The decision is made on the text that would be rewritten. A foreign answer from
    _read_env_value alone (what the older tests stub) must not reach the file."""
    _dev_tunnel_env(repo, ["MCP_TUNNEL_NAME=my-custom"])
    before = (repo / ".env").read_bytes()
    fake = {"MCP_TUNNEL_HOST": "elsewhere", "MCP_TUNNEL_URL": "https://u-8000.jpe1.devtunnels.ms/"}
    monkeypatch.setattr(B, "_read_env_value", fake.get)
    _run_dev_tunnel_step(monkeypatch, repo)
    assert (repo / ".env").read_bytes() == before


# ---- D10 -----------------------------------------------------------------------------------

def test_a_pip_failure_behind_a_proxy_names_the_proxy(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    plain = B._pip_failure_message()
    assert "HTTPS_PROXY" in plain            # tells the reader how to supply one
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    msg = B._pip_failure_message()
    assert "proxy.example:8080" in msg and "407" in msg
    assert "certificate" not in msg.lower()
