# -*- coding: utf-8 -*-
r"""A command-line entry point that fails must SAY SO: readable text, a next step, and a
non-zero exit code -- never a silent 0, a bare traceback, mojibake, or a window that closes
before anyone can read it.

METHOD. Every test here drives a REAL entry point -- a .bat file or a `python -m` / `python
script.py` invocation -- as a subprocess, exactly the way an operator or a caller would, and
reads back exit code + decoded stdout/stderr. Nothing here imports the target module and calls
its internals; the whole point is the OUTSIDE view: what a person watching the console, or a
caller checking `%ERRORLEVEL%` / `$LASTEXITCODE` / `proc.returncode`, actually sees.

SAFETY -- READ BEFORE CHANGING THIS FILE. This suite never runs against the live checkout's own
state:

  * `clone` (module-scoped fixture) makes a `git clone --local` throwaway copy of this repo
    under pytest's own tmp dir and runs every entry point FROM THAT COPY. A fresh clone carries
    no `.env`, no `.venv`, no `.fleet`, no `.setup` (all gitignored) -- which is exactly the
    "missing config / missing venv" state this file wants to inject, for free.
  * The one thing borrowed from the live checkout is its `.venv\Scripts\python.exe` BINARY, to
    run the clone's own copy of each script with the project's real dependencies installed.
    Nothing is written back to it; see `_venv_python()`.
  * Nothing here starts, stops, or reconfigures the live supervisor, MCP server, bridge,
    tunnel, or Edge. `doctor.ps1` (invoked twice, read-only) DOES probe fixed localhost ports
    (8000/8765/9222/9223) the way it always does, which is a read, not a write, and is the
    same thing doctor.bat does on an ordinary double-click.
  * Permission-denial cases lock down a directory INSIDE THE CLONE with `icacls ... /deny`,
    scoped to the current user, and restore it in a `finally` even if the assertion fails.

Windows-only: every entry point under test is a `.bat` file or Windows-specific script.

CLASSIFICATION USED BELOW (matches the audit this file backs):
  GOOD        readable message, says what to do, non-zero exit on failure.
  SILENT      exits 0 (or the wrapper discards a non-zero inner exit) on a real failure.
  UNREADABLE  a bare traceback / WinError with no next step, or mojibake that eats the message.
  MISLEADING  claims success ("Done.") while the underlying operation did not happen.

Defects are asserted with `xfail(strict=True)`: the assertion states the GOOD behaviour that
should exist. It fails today (documenting the defect); the moment it is fixed, `strict=True`
turns the unexpected pass into a hard failure so the marker cannot rot in place -- remove it
then.
"""
from __future__ import annotations

import os
import socket
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402  (see: repository ratchet against text=True)

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="every entry point under test is Windows-specific (.bat / winreg-ish paths)"
)

_GIT = None
for _cand in ("git", "git.exe"):
    import shutil as _shutil
    _found = _shutil.which(_cand)
    if _found:
        _GIT = _found
        break


# ── the throwaway clone ─────────────────────────────────────────────────────────────────────

def _venv_python() -> str:
    """The live checkout's own venv interpreter, used READ-ONLY to run the CLONE's scripts.

    Borrowing the binary is not "running against the live setup": it changes nothing there,
    and it is the only fast way to get the project's real dependencies (fastmcp, dotenv, ...)
    without a from-scratch `pip install` per test session. Falls back to `sys.executable` (a
    bare Python with none of those installed) when the live venv is absent -- which is itself
    one of the "wrong Python on PATH" scenarios this file cares about, so several tests below
    are written to pass either way.
    """
    candidate = os.path.join(REPO, ".venv", "Scripts", "python.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


@pytest.fixture(scope="module")
def clone(tmp_path_factory) -> str:
    if not _GIT:
        pytest.skip("git not found on PATH -- cannot make the required throwaway clone")
    dest = str(tmp_path_factory.mktemp("entrypoint_clone") / "clone")
    proc = childproc.run([_GIT, "clone", "--quiet", "--local", REPO, dest], timeout=120)
    assert proc.returncode == 0, (
        "throwaway `git clone --local` of the live repo failed -- cannot safely test entry "
        "points without one (never test against the live checkout itself):\n%s%s"
        % (proc.stdout, proc.stderr)
    )
    assert not os.path.isdir(os.path.join(dest, ".venv")), (
        "the clone unexpectedly carries a .venv -- .venv is supposed to be gitignored; "
        "something is wrong with the clone or the repo's .gitignore, and tests below that "
        "rely on 'no venv in a fresh checkout' would be meaningless"
    )
    assert not os.path.isfile(os.path.join(dest, ".env")), (
        "the clone unexpectedly carries a .env -- .env is supposed to be gitignored"
    )
    return dest


def _stripped_path_env(extra: dict | None = None) -> dict:
    """A copy of the real environment with PATH cut down to just C:\\Windows\\System32.

    MEASURED: this excludes every `python.exe` / `python3.exe` found via a normal PATH lookup
    AND excludes `powershell.exe` (which lives under System32\\WindowsPowerShell, not directly
    in System32) -- but it does NOT exclude `py.exe`, because the Python launcher installs
    straight into C:\\Windows itself on this kind of machine. Cutting PATH to System32 only
    (not System32;C:\\Windows) is what actually removes every interpreter; verified empirically
    against this machine before relying on it here.
    """
    env = dict(os.environ)
    env["PATH"] = r"C:\Windows\System32"
    if extra:
        env.update(extra)
    return env


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── permission-denied helper (used by more than one test) ──────────────────────────────────

class _ReadOnlyDir:
    """`icacls <path> /deny <user>:(W,WD,AD)` for the duration of a `with` block, current user
    only, always reset in `finally` -- even if the body raises. Windows-only by construction
    (this whole module is Windows-only already)."""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        os.makedirs(self.path, exist_ok=True)
        user = os.environ.get("USERNAME", "")
        proc = childproc.run(
            ["icacls", self.path, "/deny", "%s:(W,WD,AD)" % user], timeout=30)
        assert proc.returncode == 0, "icacls /deny failed: %s%s" % (proc.stdout, proc.stderr)
        return self

    def __exit__(self, *exc):
        childproc.run(["icacls", self.path, "/reset"], timeout=30)
        return False


# ═════════════════════════════════════════════════════════════════════════════════════════
# FORMERLY DEFECTS, NOW FIXED -- these seven were strict xfail (SILENT/UNREADABLE/MISLEADING,
# per the classification in the module docstring) until the entry points they cover were fixed
# to say what failed and exit non-zero. Kept as ordinary regression guards for the same reason
# as the "GOOD" section below: a future change that reopens one of these should fail a test,
# not wait to be rediscovered by hand.
# ═════════════════════════════════════════════════════════════════════════════════════════

def test_doctor_bat_exit_code_reflects_failed_checks(clone):
    ps1 = childproc.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", os.path.join(clone, "scripts", "doctor.ps1")],
        cwd=clone, timeout=120)
    assert ps1.returncode != 0, (
        "doctor.ps1 itself is expected to report at least one FAIL on a bare clone (no .env, "
        "no built ui\\*.exe) -- if this is 0 the fixture's premise changed and the test below "
        "is no longer meaningful. Got 0.\nstdout:\n%s" % ps1.stdout)

    bat = childproc.run(
        [os.path.join(clone, "doctor.bat")],
        cwd=clone, input="\n", timeout=120)
    assert bat.returncode != 0, (
        "doctor.bat should exit non-zero whenever doctor.ps1 reported a failure (it reported "
        "%d), the same way scripts\\doctor.ps1 does when run directly. Actual: doctor.bat "
        "exited 0 regardless -- a caller relying on its exit code sees success over a red "
        "screen.\nbat stdout tail:\n%s" % (ps1.returncode, bat.stdout[-2000:]))


def test_rotate_secrets_bat_does_not_claim_done_when_it_did_nothing(clone):
    proc = childproc.run(
        [os.path.join(clone, "rotate_secrets.bat")],
        cwd=clone, input="\n", timeout=60, env=_stripped_path_env())
    combined = proc.stdout + proc.stderr
    # "not recognized" is cmd.exe's own OS-localized text for "command not found" -- on an
    # English-locale Windows box it says so in English; on this machine (Japanese locale) the
    # same scenario prints the Japanese equivalent instead, unreadable here as UTF-8/cp932
    # mojibake (see the sibling check_nothing_new_is_invisible xfail reason, which hit the
    # same thing). 9009 is cmd's own numeric errorlevel for that exact scenario and is not
    # localized, so it is the locale-independent half of this OR.
    assert "not recognized" in combined or proc.returncode == 9009, (
        "expected evidence that python could not be launched (English 'not recognized' text, "
        "or exit code 9009 -- cmd's own 'command not found' code) so this test is exercising "
        "the intended scenario:\n%s" % combined)
    assert proc.returncode != 0, (
        "rotate_secrets.bat rotated nothing (python could not be launched) but still exited "
        "0. Actual output:\n%s" % combined)
    assert "Done." not in proc.stdout, (
        "rotate_secrets.bat printed 'Done. Review the next-steps above, then restart the "
        "server.' even though no python was ever found to run rotate_secrets.py -- nothing "
        "was rotated. Actual stdout:\n%s" % proc.stdout)


def test_check_ci_test_manifest_says_when_git_is_unavailable(clone):
    proc = childproc.run(
        [_venv_python(), os.path.join(clone, "scripts", "check_ci_test_manifest.py")],
        cwd=clone, timeout=60, env=_stripped_path_env())
    combined = proc.stdout + proc.stderr
    assert "not recognized" in combined or "WinError 2" in combined, (
        "expected evidence that git could not be launched, so this test is exercising the "
        "intended scenario:\n%s" % combined)
    mentions_git_problem = "git" in combined.lower() and (
        "not found" in combined.lower() or "unavailable" in combined.lower()
        or "could not" in combined.lower())
    assert proc.returncode != 0 or mentions_git_problem, (
        "with git unavailable, check_ci_test_manifest.py should say it could not verify "
        "against the git index (and/or exit non-zero) rather than silently reporting a normal "
        "OK. Actual: exit=%s, output:\n%s" % (proc.returncode, combined))


def test_check_no_identifying_names_gives_readable_message_when_git_is_missing(clone):
    proc = childproc.run(
        [_venv_python(), os.path.join(clone, "scripts", "check_no_identifying_names.py")],
        cwd=clone, timeout=60, env=_stripped_path_env())
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, "expected a failure exit when git is unavailable"
    assert "Traceback" not in combined, (
        "check_no_identifying_names.py should report a readable 'git is not available' "
        "message, not an unhandled Python traceback. Actual output:\n%s" % combined)


def test_check_nothing_new_is_invisible_gives_readable_message_when_git_is_missing(clone):
    proc = childproc.run(
        [_venv_python(), os.path.join(clone, "scripts", "check_nothing_new_is_invisible.py")],
        cwd=clone, timeout=60, env=_stripped_path_env())
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, "expected a failure exit when git is unavailable"
    assert "Traceback" not in combined, (
        "check_nothing_new_is_invisible.py should report a readable 'git is not available' "
        "message, not an unhandled Python traceback. Actual output:\n%s" % combined)


def test_session_store_compact_gives_readable_message_on_permission_denied(clone, tmp_path):
    store_dir = str(tmp_path / "readonly_store")
    with _ReadOnlyDir(store_dir):
        env = dict(os.environ)
        env["MCP_SESSION_STORE_DIR"] = store_dir
        proc = childproc.run(
            [_venv_python(), "-m", "bridge.session_store", "compact"],
            cwd=clone, timeout=60, env=env)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, "expected a failure exit against an unwritable store dir"
    assert "Traceback" not in combined, (
        "bridge.session_store compact should report a readable permission/IO problem, not an "
        "unhandled Python traceback naming sqlite3 internals. Actual output:\n%s" % combined)


def test_ui_build_check_gives_readable_message_when_csc_is_missing(clone, tmp_path):
    driver = tmp_path / "drive_missing_csc.py"
    driver.write_text(
        "import sys, os\n"
        "sys.path.insert(0, r'%s')\n"
        "os.chdir(r'%s')\n"
        "import importlib\n"
        "mod = importlib.import_module('bench.ui_build_check')\n"
        "mod.CSC = r'C:\\\\does\\\\not\\\\exist\\\\csc.exe'\n"
        "mod.main()\n" % (clone, clone),
        encoding="utf-8",
    )
    proc = childproc.run([_venv_python(), str(driver)], cwd=clone, timeout=60)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, "expected a failure exit when csc.exe cannot be found"
    assert "Traceback" not in combined, (
        "ui_build_check should report a readable 'csc.exe not found at <path>' message, not an "
        "unhandled Python traceback. Actual output:\n%s" % combined)


# ═════════════════════════════════════════════════════════════════════════════════════════
# GOOD -- regression guards. These entry points already behave correctly for the injected
# failure; kept here as ordinary (non-xfail) tests so a future change that breaks one is
# caught immediately rather than rediscovered by hand.
# ═════════════════════════════════════════════════════════════════════════════════════════

def test_submit_goal_with_no_args_says_what_to_do_and_exits_nonzero(clone):
    proc = childproc.run(
        [_venv_python(), os.path.join(clone, "scripts", "submit_goal.py")],
        cwd=clone, timeout=60)
    assert proc.returncode == 2
    assert "nothing to submit" in (proc.stdout + proc.stderr)


def test_submit_goal_missing_file_says_what_to_do_and_exits_nonzero(clone):
    proc = childproc.run(
        [_venv_python(), os.path.join(clone, "scripts", "submit_goal.py"),
         "--file", "does_not_exist_anywhere.txt"],
        cwd=clone, timeout=60)
    assert proc.returncode == 2
    combined = proc.stdout + proc.stderr
    assert "could not read" in combined
    assert "does_not_exist_anywhere.txt" in combined


def test_submit_goal_reports_a_write_failure_instead_of_claiming_success(clone, tmp_path):
    """A normal user hits this as a read-only checkout, a full disk, or OneDrive holding the
    file open. fleet_submit() (tools/fleet_intake.py) already wraps its write in try/except
    and returns a "[fleet_submit: ...]" string on failure; submit_goal.py's main() already
    detects that prefix and turns it into a non-zero exit (see its own comment: "a refusal is
    the answer"). This confirms both halves still hold when the write really does fail."""
    pending_dir = os.path.join(clone, ".fleet", "tasks", "pending")
    with _ReadOnlyDir(pending_dir):
        proc = childproc.run(
            [_venv_python(), os.path.join(clone, "scripts", "submit_goal.py"),
             "a goal that cannot be written"],
            cwd=clone, timeout=60)
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 1, "actual output:\n%s" % combined
    assert "refused" in combined
    assert "PermissionError" in combined or "Permission denied" in combined


def test_submit_goal_works_from_a_path_with_spaces_and_japanese_characters(tmp_path_factory):
    """Clones a SEPARATE throwaway copy at a path containing both a space and Japanese
    characters -- the exact combination the owner's goal calls out -- and confirms the normal
    success path still works and prints a clean, non-garbled confirmation."""
    if not _GIT:
        pytest.skip("git not found on PATH")
    dest = str(tmp_path_factory.mktemp("jp") / "\u65e5\u672c\u8a9e \u30d1\u30b9 clone")
    proc = childproc.run([_GIT, "clone", "--quiet", "--local", REPO, dest], timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = childproc.run(
        [_venv_python(), os.path.join(dest, "scripts", "submit_goal.py"),
         "goal via a japanese-and-space path"],
        cwd=dest, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "queued" in result.stdout


def test_submit_goal_runs_with_a_bare_python_lacking_project_dependencies(clone):
    """The 'wrong Python on PATH' scenario: a Python interpreter that exists but has none of
    the project's third-party packages installed (fastmcp, dotenv, ...). submit_goal.py's own
    dependency chain (argparse/os/sys/json + tools.fleet_intake + relay.task_router, whose
    `import dotenv` is already wrapped in try/except) is stdlib-only up to that guarded import,
    so this should still queue successfully rather than dying on a missing package."""
    bare = sys.executable
    if os.path.normcase(bare) == os.path.normcase(_venv_python()):
        pytest.skip("sys.executable is already the project venv -- no bare interpreter to "
                    "test against on this machine")
    proc = childproc.run(
        [bare, os.path.join(clone, "scripts", "submit_goal.py"), "goal via bare python"],
        cwd=clone, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "queued" in proc.stdout


def test_ensure_m365_signin_check_only_against_an_unused_port_is_readable(clone):
    unused_port = _free_tcp_port()
    proc = childproc.run(
        [_venv_python(), os.path.join(clone, "scripts", "ensure_m365_signin.py"),
         "--check-only", "--port", str(unused_port)],
        cwd=clone, timeout=30)
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 2, combined  # 2 == "cannot tell", never mis-reported as 0 or 1
    assert "VERDICT: cannot_tell" in combined
    assert "no Edge is answering" in combined


def test_ensure_m365_signin_check_only_against_a_wrong_service_on_the_port(clone):
    """Something IS listening on the port (a plain TCP accept-and-close), but it is not Edge's
    CDP endpoint -- the "wrong/unexpected thing on this port" cousin of "port already in use".
    tabs() (scripts/ensure_m365_signin.py) wraps its urlopen()+json.loads() in a bare except
    and returns None on any failure, so this should degrade to the same 'cannot tell' answer
    as an unused port, not a traceback."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        proc = childproc.run(
            [_venv_python(), os.path.join(clone, "scripts", "ensure_m365_signin.py"),
             "--check-only", "--port", str(port)],
            cwd=clone, timeout=15)
    finally:
        srv.close()
    combined = proc.stdout + proc.stderr
    assert "Traceback" not in combined
    assert proc.returncode in (1, 2), combined


def test_task_router_once_against_an_empty_queue_is_clean(clone):
    proc = childproc.run(
        [_venv_python(), "-m", "relay.task_router", "--once"],
        cwd=clone, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "[]"


def test_session_store_with_no_subcommand_prints_usage_and_exits_nonzero(clone, tmp_path):
    env = dict(os.environ)
    env["MCP_SESSION_STORE_DIR"] = str(tmp_path / "sess_usage")
    proc = childproc.run(
        [_venv_python(), "-m", "bridge.session_store"], cwd=clone, timeout=30, env=env)
    assert proc.returncode == 2
    assert "usage" in (proc.stdout + proc.stderr).lower()


def test_session_store_with_bad_subcommand_prints_usage_and_exits_nonzero(clone, tmp_path):
    env = dict(os.environ)
    env["MCP_SESSION_STORE_DIR"] = str(tmp_path / "sess_usage2")
    proc = childproc.run(
        [_venv_python(), "-m", "bridge.session_store", "not-a-real-subcommand"],
        cwd=clone, timeout=30, env=env)
    assert proc.returncode == 2
    assert "invalid choice" in (proc.stdout + proc.stderr).lower()


def test_setup_bat_with_no_python_and_no_network_gives_an_actionable_message(clone):
    """The worst case for setup.bat: nothing on PATH that can run Python (no venv in a fresh
    clone, no py/python, PATH stripped to System32 so not even the `py` launcher survives), AND
    no network (HTTP(S)_PROXY pointed at a closed port, so the no-admin `uv` download fails
    fast instead of hanging). This must exit non-zero with concrete next steps, not just die."""
    env = _stripped_path_env({
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
    })
    proc = childproc.run(
        [os.path.join(clone, "setup.bat"), "--status"],
        cwd=clone, input="\n", timeout=120, env=env)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "ACTION NEEDED" in combined
    assert "python.org" in combined.lower() or "astral" in combined.lower()


def test_ui_buildcheck_bat_with_no_python_says_python_and_exits_nonzero(clone):
    proc = childproc.run(
        [os.path.join(clone, "ui", "_buildcheck.bat")],
        cwd=os.path.join(clone, "ui"), timeout=30, env=_stripped_path_env())
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "python" in combined.lower()


def test_task_router_dotenv_load_survives_a_bom_and_garbage_env_file(clone, tmp_path):
    """relay/task_router.py loads .env via `from dotenv import load_dotenv`, wrapped in a bare
    try/except (see its own comment, ~line 87: "override=False, so an explicitly exported
    variable still wins"). A BOM'd or malformed .env must not turn a routine `--once` pass into
    a crash."""
    env_path = os.path.join(clone, ".env")
    with open(env_path, "wb") as fh:
        fh.write(b"\xef\xbb\xbf")  # UTF-8 BOM
        fh.write("MCP_API_KEY=not a real key\n".encode("utf-8"))
        fh.write(b"this line has no equals sign and is not valid dotenv syntax\n")
        fh.write("\u65e5\u672c\u8a9e\u30b3\u30e1\u30f3\u30c8 = value\n".encode("utf-8"))
    try:
        proc = childproc.run(
            [_venv_python(), "-m", "relay.task_router", "--once"], cwd=clone, timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "Traceback" not in (proc.stdout + proc.stderr)
    finally:
        os.remove(env_path)
