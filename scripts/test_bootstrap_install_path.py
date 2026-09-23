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
    ok = subprocess.CompletedProcess([], 0, "", "")
    state = {"done": {}}
    with mock.patch.object(B.subprocess, "call", return_value=0), \
            mock.patch("tools.childproc.run", return_value=ok):
        B.step_install_deps(state=state)
    assert state[B.DEPS_HASH_KEY] == B.requirements_hash(req)


def test_a_failed_install_records_nothing(tmp_path, monkeypatch):
    req = tmp_path / "requirements.txt"
    req.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(B, "REQUIREMENTS", req)
    state = {"done": {}}
    with mock.patch.object(B.subprocess, "call", return_value=1):
        with pytest.raises(B.StepError):
            B.step_install_deps(state=state)
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
