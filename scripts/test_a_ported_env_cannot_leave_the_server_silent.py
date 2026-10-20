# -*- coding: utf-8 -*-
r"""A .env copied from another PC must end in a REPAIR or in a loud, specific message -- never in a
bare KeyError, and never in a doctor line that claims "dying, no output" about a server that is alive.

WHAT WAS MEASURED (2026-10-20, a scratch worktree, main.py on :8100, the production .env copied
and corrupted the way a copy from another PC is):

  * every path-like key pointed at a drive and a user that do not exist, a tunnel name/URL of
    another PC, a foreign MCP_ALLOWED_BASE, helper dirs: the server STARTS and serves /health. The
    only trace is one line per unusable path in the log.
  * MCP_UNLOCK_PASSWORD_PROTECTED undecryptable: starts, serves /health (the unlock repair belongs
    to start_all and is covered elsewhere).
  * MCP_API_KEY_PROTECTED undecryptable (a DPAPI blob of another account) or malformed: main.py died
    at `API_KEY = os.environ["MCP_API_KEY"]` with `KeyError: 'MCP_API_KEY'` -- a traceback that
    names a variable the .env does not even contain, and nothing about DPAPI or what to do. And
    nothing relaunched it differently: the supervisor started the same failing server every minute.
  * doctor.ps1 said "the supervisor is relaunching the server and it is dying ... producing no output
    at all" for ANY server_up failure, without looking whether something listens on :8000. A server
    that is alive but did not answer /health inside doctor's 4 s has a log holding only its normal
    start lines, and that was read as "the current crash prints nothing".

Windows-only for the PowerShell parts; they run extracted functions in a child PowerShell and never
touch the real supervisor, server, tunnel or any port.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import secret_store as S  # noqa: E402

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)
needs_ps = pytest.mark.skipif(os.name != "nt" or not _POWERSHELL, reason="PowerShell scripts, Windows only")


def _read(rel: str) -> str:
    with open(os.path.join(REPO, rel), "r", encoding="utf-8-sig") as fh:
        return fh.read()


def _extract_braced_block(text: str, start_marker: str) -> str:
    idx = text.index(start_marker)
    brace_start = text.index("{", idx)
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[idx:i + 1]
    raise AssertionError("unbalanced braces at %r" % (start_marker,))


def _run_ps(tmp_path, body: str, timeout: int = 60) -> str:
    script = tmp_path / "t.ps1"
    script.write_text(body, encoding="utf-8")
    r = subprocess.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                       capture_output=True, timeout=timeout)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return r.stdout.decode("utf-8", "replace")


# ---- main.py / secret_store: loud, specific, with a code the supervisor knows -------------------

def test_an_unreadable_token_ends_the_server_with_a_named_cause_and_code_78(capsys):
    blob = "dpapi:" + "QUJD" * 50          # valid base64 of nothing any account can open
    env = {"MCP_API_KEY_PROTECTED": blob}
    with pytest.raises(SystemExit) as ei:
        S.require_api_key(env)
    assert ei.value.code == S.EXIT_NO_API_KEY == 78
    err = capsys.readouterr().err
    assert "MCP_API_KEY_PROTECTED" in err            # the key that is wrong, by name
    assert "cannot decrypt" in err and "another PC" in err
    assert "start_all.bat" in err                    # what to do
    assert blob not in err and "QUJD" not in err     # never the value


def test_a_missing_token_is_named_too(capsys):
    with pytest.raises(SystemExit) as ei:
        S.require_api_key({})
    assert ei.value.code == 78
    err = capsys.readouterr().err
    assert "MCP_API_KEY_PROTECTED" in err and "MCP_API_KEY" in err and "neither" in err


def test_a_readable_token_is_returned_and_kept_in_the_environment():
    env = {"MCP_API_KEY": "  plain-token  "}
    assert S.require_api_key(env) == "plain-token"
    assert env["MCP_API_KEY"] == "plain-token"


def test_main_py_uses_it_instead_of_indexing_the_environment():
    src = _read("main.py")
    assert "API_KEY = require_api_key()" in src
    assert 'os.environ["MCP_API_KEY"]' not in src


# ---- supervisor: re-make the token before the next launch ----------------------------------------

def _repair_fn() -> str:
    return _extract_braced_block(_read(os.path.join("scripts", "supervisor.ps1")), "function Invoke-ApiKeyRepair")


@needs_ps
def test_repair_runs_gen_env_and_reports_the_marker(tmp_path):
    root = tmp_path / "repo"
    (root / ".setup").mkdir(parents=True)
    stub = tmp_path / "bootstrap_stub.py"
    stub.write_text(
        "import os, sys\n"
        "assert sys.argv[1:] == ['--only', 'gen_env'], sys.argv\n"
        "open(os.path.join(os.environ['STUB_ROOT'], '.setup', 'bearer_remade'), 'w').write('x')\n"
        "print('SECRET-IN-BOOTSTRAP-OUTPUT')\n", encoding="utf-8")
    body = ("$env:STUB_ROOT = '%s'\n%s\n$r = Invoke-ApiKeyRepair -Python '%s' -Bootstrap '%s' -RootDir '%s'\n"
            "'ok=' + $r.Ok; 'code=' + $r.Code; 'remade=' + $r.Remade\n"
            % (root, _repair_fn(), sys.executable, stub, root))
    out = _run_ps(tmp_path, body)
    assert "ok=True" in out and "code=0" in out and "remade=True" in out
    assert "SECRET-IN-BOOTSTRAP-OUTPUT" not in out   # bootstrap output is discarded, never relayed


@needs_ps
def test_repair_without_a_bootstrap_script_reports_failure_not_an_exception(tmp_path):
    body = ("%s\n$r = Invoke-ApiKeyRepair -Python '%s' -Bootstrap '%s' -RootDir '%s'\n'ok=' + $r.Ok\n"
            % (_repair_fn(), sys.executable, tmp_path / "missing.py", tmp_path))
    assert "ok=False" in _run_ps(tmp_path, body)


def test_start_server_repairs_after_reading_the_exit_record_and_before_launching():
    sup = _read(os.path.join("scripts", "supervisor.ps1"))
    start = sup[sup.index("function Start-Server"):]
    record = start.index("Get-ServerExitRecord -Process")
    repair = start.index("Invoke-ApiKeyRepair -Python")
    launch = start.index("Start-Process -FilePath $Py -ArgumentList \"main.py\"")
    assert record < repair < launch
    assert re.search(r"ExitCode -eq 78", start[:launch])


# ---- doctor: never call a listening server a silent crash ---------------------------------------

def _advice_fn() -> str:
    return _extract_braced_block(_read(os.path.join("scripts", "doctor.ps1")), "function Get-ServerDownAdvice")


def _advice(tmp_path, supervisor: bool, listening: bool, started_log: str = "$null", py: str = "") -> str:
    out_arg = "$null" if started_log == "$null" else started_log
    body = ("%s\n$o = %s\n$t = Get-ServerDownAdvice -SupervisorRunning $%s -Out $o -PyProblem '%s' -Listening $%s\n"
            "Write-Output $t\n" % (_advice_fn(), out_arg, str(supervisor).lower(), py, str(listening).lower()))
    return _run_ps(tmp_path, body)


@needs_ps
def test_a_listening_server_is_alive_not_a_silent_crash(tmp_path):
    started = "@{ lines = @('INFO: Uvicorn running'); started = $true; source = 'x' }"
    text = _advice(tmp_path, True, True, started)
    assert "IS listening on :8000" in text and "alive" in text
    assert "dying" not in text.lower() and "producing no output" not in text
    assert "m365-companion-supervisor.log" in text      # where the real reason is recorded


@needs_ps
def test_a_port_nobody_holds_with_only_an_old_start_does_not_claim_a_crash_with_no_output(tmp_path):
    started = "@{ lines = @('INFO: Application startup complete'); started = $true; source = 'x' }"
    text = _advice(tmp_path, True, False, started)
    assert "nothing is listening" in text and "STARTED SUCCESSFULLY" in text
    assert "producing no output at all" not in text


@needs_ps
def test_a_real_traceback_is_still_quoted_and_no_supervisor_still_says_start_all(tmp_path):
    died = "@{ lines = @('KeyError: boom'); started = $false; source = 'x' }"
    assert "KeyError: boom" in _advice(tmp_path, True, False, died)
    assert "start_all.bat" in _advice(tmp_path, False, False)
