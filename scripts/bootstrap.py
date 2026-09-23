#!/usr/bin/env python3
# =============================================================================
# m365-copilot-companion-mcp - resumable, checkpointed environment bootstrap.
#
# setup.bat ensures a Python interpreter exists, then runs:  python scripts/bootstrap.py
# This module holds ALL the real logic. It is an idempotent state machine:
# every step (1) checks its precondition idempotently, (2) does its work, and
# (3) marks itself done in .setup/state.json. Re-running resumes from the first
# not-yet-done step and skips finished ones.
#
# When a step needs admin / a manual install / a Microsoft login / MFA, it
# raises ActionNeeded: bootstrap prints
#     ACTION NEEDED: <english instruction>; then re-run setup.bat
# saves state, and exits non-zero. The next run continues from there.
#
# ASCII / ENGLISH ONLY (comments included) -- this repo's .bat/.ps1 mis-decode
# non-ASCII; we keep the Python files matching that rule for consistency.
#
# CLI:
#   python scripts/bootstrap.py            run / resume
#   python scripts/bootstrap.py --status   print each step done/pending (no changes)
#   python scripts/bootstrap.py --reset    clear saved state (no system changes)
#   python scripts/bootstrap.py --only X   run a single step by name
# =============================================================================
from __future__ import annotations

import argparse
import getpass
import hashlib
import inspect
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

# Repo root = parent of this scripts/ dir. Everything is resolved against it so
# the bootstrap behaves identically regardless of the caller's working dir.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: THE OLDEST PYTHON THE REQUIREMENTS CAN BE INSTALLED ON, derived rather than chosen: fastmcp,
#: mcp, anyio and ddgs all declare Requires-Python >=3.10 (read from their METADATA in the
#: working .venv, 2026-09-24), and CI installs on exactly 3.10. There was no version check at
#: all (D8), so a machine whose `py -3` was 3.9 built the venv on it and then failed
#: "pip install -r requirements.txt ... (network or a wheel build)" on every run -- a wrong
#: diagnosis in an endless loop. setup.bat carries the same number in its probes;
#: scripts/test_install_path_python_version.py holds the two together.
MIN_PYTHON = (3, 10)


def _python_too_old_message(found: tuple, where: str) -> str:
    return ("%s is Python %d.%d, and this project needs Python %d.%d or newer (its "
            "dependencies will not install on anything older). Run setup.bat, which "
            "provisions its own Python %d.12 with uv when the Python on PATH is too old, "
            "or install Python 3.12 from https://www.python.org/downloads/windows/ "
            "(per-user, no admin) and re-run setup.bat."
            % (where, found[0], found[1], MIN_PYTHON[0], MIN_PYTHON[1], MIN_PYTHON[0]))


# REFUSED BEFORE ANYTHING ELSE IS IMPORTED: the imports below may use syntax an old
# interpreter cannot even parse, and a SyntaxError from a helper module is not a message.
if __name__ == "__main__" and tuple(sys.version_info[:2]) < MIN_PYTHON:
    print("ACTION NEEDED: " + _python_too_old_message(
        tuple(sys.version_info[:2]), "The Python running setup (%s)" % sys.executable))
    sys.exit(2)

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)   # appended, not prepended: nothing here may shadow a package
from tools.secret_store import UNLOCK_PASSWORD_PROTECTED_VAR, protect_secret  # noqa: E402
import env_file  # noqa: E402  (scripts/env_file.py -- the atomic .env writer)

STATE_DIR = ROOT / ".setup"
STATE_FILE = STATE_DIR / "state.json"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"  # Windows layout
GENERATED_DIR = ROOT / "generated"

DEFAULT_TUNNEL_NAME = "m365-copilot-companion"

# Privacy guard: some tunnel names leak an identifying (organization/user) token to the
# GLOBAL devtunnels.ms namespace, which is visible to Microsoft and to the tunnel owner.
# These two SHA-256 values are a blocklist of the specific leaked token and the specific
# leaked full tunnel name seen in the wild -- the plaintext is intentionally never written
# here; only its hash is, so this file cannot itself leak it. Hashing is over the UTF-8
# bytes of the lowercased input, hex-encoded lowercase. Mirrors setup_devtunnel.ps1's
# Test-IdentifyingTunnelName -- keep both in sync.
TOKEN_SHA256 = "2a0341296bb96dc7d205036f9f693427809772f6136a46f58b04a1c492de9e04"  # gitleaks:allow
FULLNAME_SHA256 = "5ba174b8e87faf4e8106e36a7cf5a901bbec3435d01fbd56914c2b0346858261"  # gitleaks:allow


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _machine_suffix() -> str:
    """Short, stable-per-machine suffix (~8 hex chars) so a tunnel-name collision
    (devtunnel ids live in the GLOBAL devtunnels.ms namespace, so two different
    users'/machines' clones of this repo trying to create the same default name
    WILL collide) resolves to a name unique per machine/user yet stable across
    re-runs on that same machine. Lowercase hex only, valid for a devtunnel id."""
    seed = "%s|%s" % (platform.node(), getpass.getuser())
    return _sha256_hex(seed.lower())[:8]


_GENERATED_NAME_RE = re.compile(
    "^" + re.escape(DEFAULT_TUNNEL_NAME) + r"(-[0-9a-f]{6}|-[0-9a-f]{8}){0,2}$")


def _is_generated_tunnel_name(name: str | None) -> bool:
    """The default name, optionally followed by hex machine suffixes (8 = this scheme, 6 = the
    legacy setup_devtunnel one). Same pattern as setup_devtunnel.ps1's Test-GeneratedTunnelName."""
    return bool(name and _GENERATED_NAME_RE.match(name.strip().lower()))


def _legacy_machine_suffix() -> str:
    """setup_devtunnel.ps1's pre-D22 suffix: SHA1(COMPUTERNAME|USERNAME)[:6], case as given.
    Still counts as this machine's, as it does there."""
    seed = "%s|%s" % (os.environ.get("COMPUTERNAME", ""), os.environ.get("USERNAME", ""))
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]


def _is_identifying_tunnel_name(name: str | None) -> bool:
    """Returns True if 'name' leaks an identifying token. Mirrors
    Test-IdentifyingTunnelName in setup_devtunnel.ps1 -- keep both in sync.
    Empty/whitespace name -> False (nothing to leak, "no name set")."""
    if not name or not name.strip():
        return False
    lower = name.strip().lower()

    # 1. Whole-name blocklist hash match.
    if _sha256_hex(lower) == FULLNAME_SHA256:
        return True

    # 2. Per-token blocklist hash match (split on any non-alphanumeric).
    tokens = [t for t in re.split(r"[^a-z0-9]+", lower) if t]
    for t in tokens:
        if _sha256_hex(t) == TOKEN_SHA256:
            return True

    # 3. Generic runtime checks (no hash needed) -- catches folder-derived /
    #    user-derived names on any machine, beyond the specific blocklist above.
    #    NOT FOR A NAME THIS REPOSITORY GENERATED (D29, mirrors setup_devtunnel.ps1's
    #    Test-GeneratedTunnelName). Those are the fixed default plus a hash and carry nothing
    #    user-derived, but the substring test below fired whenever the user name occurred
    #    inside "m365-copilot-companion-<hex>" (a user named "pan", "com", "on", or a hex-only
    #    name inside the suffix): the name was thrown away, regenerated identically, and
    #    "The PUBLIC URL will change" was printed on every run while nothing changed.
    if _is_generated_tunnel_name(name):
        return False
    repo_leaf = ROOT.name.lower()
    user_name = getpass.getuser().lower()
    for t in tokens:
        if (repo_leaf and t == repo_leaf) or (user_name and t == user_name):
            return True
    if (repo_leaf and repo_leaf in lower) or (user_name and user_name in lower):
        return True

    return False


# --------------------------------------------------------------------------- #
# Control-flow signals
# --------------------------------------------------------------------------- #
class ActionNeeded(Exception):
    """A step cannot proceed without the user (admin / login / manual install).

    Carries an English, copy-pasteable instruction. Raising this is NOT a
    failure of the bootstrap: state is saved and the step stays pending so the
    next 'setup.bat' run resumes here.
    """


class StepError(Exception):
    """A step genuinely failed (e.g. pip returned non-zero). Also resumable:
    the step stays pending and re-running retries it."""


# --------------------------------------------------------------------------- #
# State persistence (the checkpoint file)
# --------------------------------------------------------------------------- #
def load_state(state_file: Path = STATE_FILE) -> dict:
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # Corrupt/partial state must never wedge the bootstrap; start clean.
            return {"done": {}}
    return {"done": {}}


def save_state(state: dict, state_file: Path = STATE_FILE) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically so a crash mid-write cannot corrupt the checkpoint.
    tmp = state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_file)


def mark_done(state: dict, name: str, state_file: Path = STATE_FILE) -> None:
    state.setdefault("done", {})[name] = True
    save_state(state, state_file)


def is_done(state: dict, name: str) -> bool:
    return bool(state.get("done", {}).get(name))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
#: Everything printed also lands here, so a console window that vanishes does not take the
#: evidence with it.
#:
#: WHY. An operator on a fresh machine reported "the command prompt fell over and re-running
#: still dies partway", and there was nothing to read: the window was gone. Every exit path in
#: quickstart.bat and setup.bat pauses, so a clean failure holds the window -- which means what
#: they saw was an ABNORMAL termination, and that is precisely the case where the on-screen
#: output is the only record and is lost. A transcript costs nothing and is the difference
#: between diagnosing this and guessing at it, which is what happened for two rounds.
TRANSCRIPT = ROOT / ".setup" / "bootstrap.log"


def _transcribe(msg: str) -> None:
    """Append one line to the transcript. NEVER raises: a logging call has taken a run down in
    this project before, and a transcript that can kill the thing it is recording is worse than
    no transcript."""
    try:
        TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
        with open(TRANSCRIPT, "a", encoding="utf-8") as fh:
            fh.write("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg) + chr(10))
    except Exception:
        pass


def _print_safe(msg: str) -> None:
    """Console write, encode-safe. This console is cp932 and a print() that raises
    UnicodeEncodeError has ended a long run here before."""
    try:
        print(msg, flush=True)
    except Exception:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:
            pass


def show_only(msg: str) -> None:
    """Print a line and DO NOT record it. Used for the freshly generated credentials.

    A SEPARATE FUNCTION, NOT A FLAG ON log(). The first version of this passed
    transcribe=False, which behaves correctly and leaves the data flow from the credential to
    the file write sitting in the source -- so the clear-text-storage finding stayed open, and
    a later edit flipping a default would silently restore the leak. There is no path from
    here to _transcribe, which is a property of the shape rather than of an argument.

    The print itself stays and cannot go: a fresh .env stores only the protected form of the
    unlock password, so this is the one occasion the operator can read the real value.
    """
    _print_safe(msg)


def log(msg: str) -> None:
    # The console first: encode-safe, because this console is cp932 and a print() that raises
    # UnicodeEncodeError has ended a long run here before. The transcript is written whatever
    # the console can display, so a character the console cannot show is still recorded.
    _print_safe(msg)
    _transcribe(msg)


def _call_step(fn, state: dict, state_file: Path) -> None:
    """Invoke a step function. Most steps take no arguments, but a few need the
    live state to clear a downstream checkpoint (e.g. ensure_venv must reset
    install_deps when it recreates the venv). We pass state/state_file ONLY to
    steps whose signature declares them, so the simple 0-arg steps (and the
    mocked steps in the tests) keep working unchanged."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs = {}
    if "state" in params:
        kwargs["state"] = state
    if "state_file" in params:
        kwargs["state_file"] = state_file
    fn(**kwargs)


def step_header(msg: str) -> None:
    print("==> " + msg, flush=True)


def venv_python() -> Path:
    """Python executable to use for installs. Prefer the project venv; fall
    back to the interpreter currently running this script."""
    if VENV_PYTHON.exists():
        return VENV_PYTHON
    return Path(sys.executable)


def find_executable(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


# --------------------------------------------------------------------------- #
# STEP: ensure_venv
# --------------------------------------------------------------------------- #
def _venv_runs() -> bool:
    """Can this venv's python execute at all? The question that decides whether it is BROKEN."""
    if not VENV_PYTHON.exists():
        return False
    try:
        from tools.childproc import run as _run_child
        res = _run_child([str(VENV_PYTHON), "-c", "import sys"], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _venv_version() -> tuple | None:
    """(major, minor) of the venv's interpreter, or None when it cannot be run or read.

    RUN, NOT READ. pyvenv.cfg records the version the venv was CREATED with, and a venv whose
    base Python was later upgraded or removed says something different when executed. What
    matters is what runs.
    """
    if not VENV_PYTHON.exists():
        return None
    try:
        from tools.childproc import run as _run_child
        res = _run_child([str(VENV_PYTHON), "-c",
                          "import sys; print('%d.%d' % sys.version_info[:2])"], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    m = re.search(r"(\d+)\.(\d+)", res.stdout or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _venv_usable() -> bool:
    """The venv's interpreter runs AND is new enough for the requirements (D8, D20)."""
    ver = _venv_version()
    return ver is not None and ver >= MIN_PYTHON


def _venv_has_pip() -> bool:
    if not VENV_PYTHON.exists():
        return False
    try:
        from tools.childproc import run as _run_child
        res = _run_child([str(VENV_PYTHON), "-m", "pip", "--version"], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _seed_pip() -> bool:
    """Install pip into a venv that runs but has none. Returns whether it worked.

    A VENV WITHOUT PIP IS NOT A BROKEN VENV. `uv venv` does not seed pip by default, so a venv
    created by the uv bootstrap path is perfectly good and simply has no pip yet -- and the old
    probe called that "broken" and tried to DELETE it. On a fresh machine that ended the run:
    the delete failed with WinError 5 and the operator was told to remove .venv by hand, for a
    venv that was working fine. Destroying something over a missing package is
    disproportionate; installing the package is the proportionate answer.
    """
    try:
        rc = subprocess.call([str(VENV_PYTHON), "-m", "ensurepip", "--upgrade"])
        if rc == 0 and _venv_has_pip():
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def _venv_is_healthy() -> bool:
    """Probe the venv with a REAL command instead of trusting that python.exe merely exists on
    disk. A venv whose python cannot run at all (half-created, wrong Python moved or deleted,
    corrupt) would otherwise pass a bare file-existence check and then "install_deps" silently
    no-ops against it.

    HEALTHY MEANS THE INTERPRETER RUNS. Missing pip is repaired, not treated as corruption --
    see _seed_pip for what that mistake cost on a fresh machine.
    """
    if not _venv_runs():
        return False
    # A VENV ON A TOO-OLD PYTHON RUNS FINE AND CAN NEVER HOLD THE REQUIREMENTS (D8). It has to
    # be rebuilt on the interpreter running this script -- which setup.bat has already checked
    # is new enough -- or install_deps fails on it for ever with a network-shaped message.
    ver = _venv_version()
    if ver is not None and ver < MIN_PYTHON:
        log("    .venv runs Python %d.%d, older than the %d.%d the requirements need; "
            "rebuilding it." % (ver[0], ver[1], MIN_PYTHON[0], MIN_PYTHON[1]))
        return False
    if _venv_has_pip():
        return True
    log("    .venv has no pip (uv creates it that way); seeding with ensurepip")
    return _seed_pip()


def step_ensure_venv(state: dict | None = None, state_file: Path = STATE_FILE) -> None:
    step_header("Ensuring virtual environment (.venv)")

    # Probe with a real command (pip --version), NOT just file existence: a
    # python.exe can exist while the venv is broken, which produced the observed
    # "venv recreated but deps skipped" failure on resume.
    if _venv_is_healthy():
        log("    OK: .venv already present and working (skipping)")
        return

    if VENV_PYTHON.exists():
        log("    WARN: .venv exists but its python failed a 'pip --version' probe; "
            "recreating it.")
        # A recreated venv is EMPTY, so deps must be reinstalled. Clear the
        # install_deps done-flag so the next step actually runs pip again.
        if state is not None:
            if state.get("done", {}).pop("install_deps", None):
                log("    (cleared install_deps checkpoint so dependencies reinstall)")
                save_state(state, state_file)
        # Remove the broken tree so 'python -m venv' can rebuild cleanly.
        # Windows marks some files read-only and rmtree cannot unlink those, so a plain call
        # fails with WinError 5 and the run ends. Clear the bit and retry before giving up.
        def _force(func, path, _exc):
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except OSError:
                pass

        shutil.rmtree(ROOT / ".venv", onerror=_force)
        if (ROOT / ".venv").exists():
            raise ActionNeeded(
                "The existing .venv could not be removed automatically. Delete the .venv "
                "folder in the repo root by hand, then re-run quickstart.bat (or setup.bat). "
                "If a file is locked, close any editor or terminal that is using it first."
            )

    # setup.bat normally creates the venv (via uv or python -m venv) before we
    # get here. If it does not exist we try once with the running interpreter.
    log("    .venv missing; creating with 'python -m venv .venv'")
    rc = subprocess.call([sys.executable, "-m", "venv", str(ROOT / ".venv")])
    if rc != 0 or not VENV_PYTHON.exists():
        raise ActionNeeded(
            "Could not create the .venv automatically. Create it manually "
            "(no admin needed): from the repo root run  py -3 -m venv .venv  "
            "(or use uv:  uv venv .venv )"
        )
    log("    OK: created .venv")


# --------------------------------------------------------------------------- #
# STEP: install_deps
# --------------------------------------------------------------------------- #
REQUIREMENTS = ROOT / "requirements.txt"

#: state.json key holding the sha256 of requirements.txt as it was when install_deps last
#: succeeded (D5). The done flag alone said "dependencies were installed once" and was read as
#: "the dependencies this checkout needs are installed", which stops being true at the first
#: `git pull` that adds one: a new import in main.py then failed verify once (clearing the
#: flags) so only the SECOND setup run reinstalled, and a lazily imported new package was
#: never installed at all.
DEPS_HASH_KEY = "install_deps_requirements_sha256"


def requirements_hash(req: Path = None) -> str | None:
    """sha256 of requirements.txt with line endings normalised (a CRLF/LF checkout of the
    same file is the same requirements), or None when the file is absent."""
    req = REQUIREMENTS if req is None else req
    try:
        data = Path(req).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def deps_are_stale(state: dict, req: Path = None) -> bool:
    """True when install_deps is marked done but not for THIS requirements.txt. A state
    written before the hash existed has no hash, which is "unknown", which reinstalls: pip
    over an up-to-date venv costs a minute, and trusting an unknown costs a broken tool."""
    if not is_done(state, "install_deps"):
        return False
    return state.get(DEPS_HASH_KEY) != requirements_hash(req)


def configured_proxy() -> str | None:
    """The proxy this run was given (setup.bat derives it from the system, D10), if any."""
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return None


def _pip_failure_message() -> str:
    """What to say when pip fails. On a proxied network the likely cause is the proxy, and
    certificate advice sends the reader to the wrong fix (D10)."""
    proxy = configured_proxy()
    if proxy:
        return ("pip install -r requirements.txt failed. This PC reaches the internet through "
                "a proxy (%s), and setup passed it to pip. If the error above mentions "
                "'ProxyError', '407', 'Tunnel connection failed' or a timeout, the proxy "
                "refused the connection: ask IT whether pypi.org and files.pythonhosted.org "
                "are allowed through it for your account, or set HTTPS_PROXY to the proxy "
                "they tell you to use, then re-run quickstart.bat (or setup.bat)." % proxy)
    return ("pip install -r requirements.txt failed (network or a wheel build). If this "
            "network needs a proxy that Windows does not know about, set HTTPS_PROXY="
            "http://<proxy-host>:<port> in this window first. Re-running quickstart.bat "
            "(or setup.bat) retries this step.")


def step_install_deps(state: dict | None = None, state_file: Path = STATE_FILE) -> None:
    step_header("Installing Python dependencies (requirements.txt)")
    py = str(venv_python())
    req = REQUIREMENTS
    if not req.exists():
        raise StepError("requirements.txt not found at repo root.")

    # Corporate TLS-inspecting proxy: its root CA is not in pip's bundled certifi store,
    # so pip otherwise dies with SSL CERTIFICATE_VERIFY_FAILED ("unable to get local issuer
    # certificate") on pypi.org / files.pythonhosted.org. Pass --trusted-host ON THE COMMAND
    # LINE so the bypass applies regardless of whether a user/system pip.ini is read (the
    # venv may be a uv-provisioned CPython that does not pick up %APPDATA%\pip\pip.ini).
    trusted = [
        "--trusted-host", "pypi.org",
        "--trusted-host", "files.pythonhosted.org",
        "--trusted-host", "pypi.python.org",
    ]

    # Best-effort pip upgrade; never fatal.
    subprocess.call([py, "-m", "pip", "install", *trusted, "--upgrade", "pip", "--quiet"])

    rc = subprocess.call([py, "-m", "pip", "install", *trusted, "-r", str(req)])
    if rc != 0:
        raise StepError(_pip_failure_message())

    # pip returning 0 is NECESSARY but not SUFFICIENT: a partial download, a
    # broken wheel, or an install against the wrong interpreter can leave core
    # deps unimportable while pip still exits 0. Verify by actually importing a
    # CORE sentinel packages in the venv (including the v0.3 headless LOCAL_LOOP
    # execution path). sqlite3 is from the standard library, but checking it here also
    # catches unusually stripped Python distributions. If any import fails to
    # import, the environment is not usable; raise a novice-readable StepError.
    sentinels = ["fastmcp", "httpx", "dotenv", "playwright", "psutil", "sqlite3"]
    from tools.childproc import run as _run_child
    check = _run_child([py, "-c", "import " + ", ".join(sentinels)])
    if check.returncode != 0:
        detail = (check.stderr or check.stdout or "").strip().splitlines()
        last = detail[-1] if detail else "(no error text)"
        raise StepError(
            "Dependencies did not import after install: could not 'import %s' in "
            ".venv. This usually means the download was incomplete or a package "
            "failed to build. Check your internet connection, then re-run "
            "quickstart.bat (or setup.bat) to retry. Technical detail: %s"
            % (", ".join(sentinels), last)
        )

    # IMPORTANT: 'playwright' is pulled in (for the optional relay/bridge), but
    # we deliberately DO NOT run 'playwright install'. The relay attaches to an
    # already-running browser via connect_over_cdp (CDP), so it uses the user's
    # existing Edge/Chrome -- it never drives a Playwright-managed browser. A
    # 'playwright install' would download ~400MB of browser binaries for nothing
    # and can require extra permissions. So: no browser download here, on purpose.
    log("    OK: dependencies installed and import-verified (server + headless LOCAL_LOOP)")
    # RECORDED BESIDE THE FLAG, AND ONLY ON SUCCESS: the hash names the requirements.txt this
    # install satisfied, so run_all can tell when a pull has changed it (D5). The driver's
    # mark_done saves it together with the flag.
    if state is not None:
        state[DEPS_HASH_KEY] = requirements_hash(req)


# --------------------------------------------------------------------------- #
# STEP: gen_env
# --------------------------------------------------------------------------- #
#: Used only when .env.example is missing from the checkout. The template is the source of
#: truth; this is the floor under it.
_FALLBACK_DEFAULTS = (
    ("MCP_UNLOCK_TTL_DAYS", "30"),
    ("MCP_ALLOWED_BASE", "~"),
    ("TASK_JOB_APPROVAL_MODE", "default"),
    ("MCP_TOOL_MAP", "1"),
    ("MCP_TOOL_MAP_MAX", "8"),
    ("MCP_REVIEW_P2C", "0"),
    ("MCP_EXECUTION_PROFILES", "0"),
    ("MCP_DEEP_REVIEW_TRANSPORT", "auto"),
    ("MCP_LOCAL_REVIEW_MAX_CONCURRENT", "2"),
    ("MCP_LOCAL_ROTATE_AFTER_TURNS", "3"),
    ("MCP_LOCAL_EDGE_MB_LIMIT", "1400"),
)

#: Template keys that are SECRETS: never copied from the template (its values are
#: placeholders); the secret branch below mints them instead.
_SECRET_TEMPLATE_KEYS = ("MCP_API_KEY", "MCP_UNLOCK_PASSWORD")


def missing_template_lines(current: str, example_text: str | None) -> tuple:
    """(lines_to_append, keys_left_commented) for an EXISTING .env.

    EVERY ACTIVE KEY OF THE TEMPLATE, NOT A HAND-KEPT SUBSET (D6). This used to append seven
    named defaults, so a .env that existed before bootstrap ran -- configure_env.ps1 creates
    one holding only the agent URLs when start_all runs first -- never received the other
    template keys: MCP_ALLOWED_BASE=~ (absent, the file tools reached every drive),
    MCP_TOOL_MAP=1 / MCP_TOOL_MAP_MAX (absent, Copilot Studio's tool budget overflowed) and
    MCP_UNLOCK_TTL_DAYS. The list had to be remembered and was not; deriving it cannot drift.

    NEVER OVERWRITES. A key with an active line keeps its value, whatever it is. A key the
    user has COMMENTED OUT is also left alone and reported: `# MCP_TOOL_MAP=1` is a statement
    (e.g. a Claude Code user who wants every tool registered), and re-activating it behind
    their back would be the overwrite this function promises not to do.
    """
    have = env_file.active_keys(current)
    commented = env_file.commented_keys(current) - have
    if example_text is not None:
        pairs = env_file.example_assignments(example_text)
    else:
        pairs = [(k, "%s=%s" % (k, v)) for k, v in _FALLBACK_DEFAULTS]
    out, left, seen = [], [], set()
    for key, line in pairs:
        if key in seen or key in _SECRET_TEMPLATE_KEYS or key in have:
            seen.add(key)
            continue
        seen.add(key)
        if key in commented:
            left.append(key)
            continue
        out.append(line)
    return out, left


#: Secrets minted by THIS process, held in memory only, so the end of the run can repeat them
#: (D1). Never written anywhere; show_only is the only thing that reads it.
_MINTED_THIS_RUN: list = []


def _show_secrets_box(items: list, repeated: bool = False) -> None:
    """Frame the freshly minted secrets so they cannot scroll past unnoticed (D1).

    The unlock password was printed as one plain line in the middle of the install output, and
    .env keeps only its DPAPI-protected form -- so a window closed before it was copied lost it,
    and nothing on screen said where to find it again (the two messages that tried both pointed
    at a script that did not print it). Framed, repeated at the end of setup, and naming the
    command that shows it again.
    """
    bar = "    " + "#" * 75
    show_only("")
    show_only(bar)
    if repeated:
        show_only("    #  YOUR SECRETS AGAIN (the same values printed earlier in this run)")
    else:
        show_only("    #  COPY THESE NOW -- you paste them into Copilot Studio")
    show_only("    #")
    for label, value in items:
        show_only("    #  %-32s %s" % (label + ":", value))
    show_only("    #")
    show_only("    #  .env keeps the unlock password only in PROTECTED form, so opening .env")
    show_only("    #  will not show it. To see both again at any time, double-click")
    show_only("    #      copilot_studio_values.bat      (in this folder)")
    show_only("    #  or run  scripts\\copilot_studio_values.ps1")
    show_only(bar)
    show_only("")


def _remember_and_show(items: list) -> None:
    _MINTED_THIS_RUN.extend(items)
    _show_secrets_box(items)


def repeat_minted_secrets() -> None:
    """Called at the very end of a run: the one-time display must be impossible to miss."""
    if _MINTED_THIS_RUN:
        _show_secrets_box(list(_MINTED_THIS_RUN), repeated=True)


_AGAIN_HINT = ("Show them again with copilot_studio_values.bat "
               "(scripts\\copilot_studio_values.ps1)")


def step_gen_env() -> None:
    step_header("Preparing .env")
    env_path = ROOT / ".env"
    example = ROOT / ".env.example"

    if env_path.exists():
        # Preserve every existing value/secret, but backfill every key the template defines
        # and this file lacks. Append-only; never changes a value the user has.
        # Read with its own line endings kept, so the append below writes in the same ones.
        current = env_file.read_text(env_path)
        example_text = example.read_text(encoding="utf-8-sig") if example.exists() else None
        missing, left_commented = missing_template_lines(current, example_text)
        if left_commented:
            log("    NOTE: left commented out as you had them (not re-enabled): "
                + ", ".join(left_commented))

        # THE SECRETS ARE ALSO A MISSING KEY. The block above only ever backfilled non-secret
        # defaults, so an existing .env that had lost MCP_API_KEY or the unlock password (a bad
        # hand-edit, a partial merge, a truncated sync) was left without them -- and because
        # this step returns early on "exists", it never regenerated them either. The result was
        # step_verify failing on every subsequent run with no step able to repair it, i.e. a
        # dead end that only a human editing .env by hand could break. So treat an ABSENT
        # secret as a missing key and mint a fresh one, on exactly the same append-only terms:
        # if the key is already present (even blank/placeholder -- that is the user's value to
        # keep or fix), it is NOT touched. We never overwrite an existing secret here.
        secret_lines = []
        have_api = any(line.lstrip().startswith("MCP_API_KEY=") for line in current.splitlines())
        have_unlock = any(
            line.lstrip().startswith("MCP_UNLOCK_PASSWORD=")
            or line.lstrip().startswith(UNLOCK_PASSWORD_PROTECTED_VAR + "=")
            for line in current.splitlines()
        )
        minted_unlock = None
        # THE KEY NAMES ARE COLLECTED SEPARATELY, NOT RECOVERED FROM THE LINES LATER.
        #
        # The transcript line below used to read them back with `s.split("=", 1)[0]`, which is
        # correct and is a sanitizer nobody can see: it leaves a data flow from the minted
        # secret to the file write sitting in the source, so the clear-text-storage finding
        # stayed open (alert #30) and a later edit dropping the split would restore a real leak
        # silently. show_only, twenty lines up, was made a separate function rather than a
        # `transcribe=False` flag for exactly this reason -- "a property of the shape rather
        # than of an argument" -- and this is the same rule applied to the same file twice.
        #
        # A list of names that never held a value cannot leak one.
        minted_keys = []
        minted_api = None
        if not have_api:
            minted_api = secrets.token_hex(20)
            secret_lines.append("MCP_API_KEY=" + minted_api)
            minted_keys.append("MCP_API_KEY")
        if not have_unlock:
            minted_unlock = secrets.token_hex(8)
            secret_lines.append(UNLOCK_PASSWORD_PROTECTED_VAR + "=" + protect_secret(minted_unlock))
            minted_keys.append(UNLOCK_PASSWORD_PROTECTED_VAR)

        appended = missing + secret_lines
        if appended:
            # ATOMIC (D28): a truncated .env makes the next run mint a new MCP_API_KEY, and
            # Copilot Studio then gets 401 with every local check green.
            env_file.atomic_write_text(env_path, env_file.append_lines(current, appended))
            if missing:
                log("    OK: .env already exists; added the template key(s) it lacked: "
                    + ", ".join(line.split("=", 1)[0] for line in missing))
            if minted_keys:
                # Name the KEYS, never the values, in the transcript -- same rule the fresh-.env
                # path follows: the log file is what an operator is asked to send when setup fails.
                log("    OK: .env was missing required secret(s); generated: "
                    + ", ".join(minted_keys))
                # ON SCREEN ONLY (show_only never reaches the transcript). A NEW BEARER IS SHOWN
                # TOO: it replaces whatever Copilot Studio holds, so it must be re-pasted, and it
                # used to be minted without a word.
                shown = []
                if minted_api is not None:
                    shown.append(("Bearer token (MCP_API_KEY)", minted_api))
                if minted_unlock is not None:
                    shown.append(("Unlock password", minted_unlock))
                _remember_and_show(shown)
                _transcribe("    (the new secret(s) were shown on screen; not recorded here. "
                            + _AGAIN_HINT + ")")
        else:
            log("    OK: .env already exists (left untouched)")
        return

    api_key = secrets.token_hex(20)       # 40 hex chars
    unlock_code = secrets.token_hex(8)    # 16 hex chars
    protected_unlock_code = protect_secret(unlock_code)

    if example.exists():
        lines = example.read_text(encoding="utf-8-sig").splitlines()
    else:
        lines = [
            "MCP_API_KEY=replace",
            "MCP_UNLOCK_PASSWORD=replace",
            "MCP_UNLOCK_TTL_DAYS=30",
            "MCP_ALLOWED_BASE=~",
        ]

    out_lines = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("MCP_API_KEY="):
            out_lines.append("MCP_API_KEY=" + api_key)
        elif stripped.startswith("MCP_UNLOCK_PASSWORD="):
            out_lines.append(UNLOCK_PASSWORD_PROTECTED_VAR + "=" + protected_unlock_code)
        else:
            # Keep MCP_ALLOWED_BASE=~ and leave the agent-URL vars commented as-is.
            out_lines.append(line)

    # Note: the MCP_*_AGENT_URL / bridge vars stay commented in .env.example, so
    # they remain commented here too. They are optional and embed tenant GUIDs;
    # the user fills them in only if they use the relay/bridge.
    # ATOMIC (D28). Two quickstarts at once each saw no .env and each wrote one with different
    # secrets; the lock in quickstart.bat stops that, and this makes sure the file that wins is
    # whole.
    # CRLF, as the text-mode write_text this replaces produced on Windows.
    env_file.atomic_write_text(env_path, "\r\n".join(out_lines) + "\r\n")
    log("    OK: wrote .env with fresh random MCP_API_KEY and MCP_UNLOCK_PASSWORD")
    # ON SCREEN ONLY. These two are freshly minted and are printed so they can be copied into
    # Copilot Studio; they must not reach .setup/bootstrap.log, which is the file an operator
    # is asked to send when setup fails. A note goes to the transcript in their place, because
    # "the credentials were shown here" is worth recording and the values are not.
    _remember_and_show([("Bearer token (MCP_API_KEY)", api_key),
                        ("Unlock password", unlock_code)])
    _transcribe("    (the Bearer token and unlock password were shown on screen; not recorded "
                "here. " + _AGAIN_HINT + ")")
    log("    Keep these secret. Optional MCP_*_AGENT_URL vars stay commented in .env.")


# --------------------------------------------------------------------------- #
# STEP: check_edge
# --------------------------------------------------------------------------- #
def _edge_candidates() -> list[Path]:
    cands = []
    for base in (
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if base:
            cands.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return cands


def step_check_edge() -> None:
    step_header("Checking Microsoft Edge (optional, only for the relay/bridge)")
    found = None
    on_path = find_executable("msedge", "msedge.exe")
    if on_path:
        found = on_path
    else:
        for c in _edge_candidates():
            if c.exists():
                found = str(c)
                break

    if found:
        log("    OK: Edge found at " + found)
    else:
        # Not fatal: Edge is only needed for the optional CDP relay/bridge.
        log("    WARN: msedge.exe not found in the usual locations.")
        log("          Edge (or Chrome) is only needed for the optional relay/bridge.")

    # We never force-launch the browser. Tell the user how to start it with the
    # CDP debug port when/if they want the relay. (This is informational only.)
    log("    To use the relay/bridge later, launch Edge with the debug port:")
    log('      & "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" '
        "--remote-debugging-port=9222")
    log("    (Chrome works too; the relay attaches over CDP, no extra download.)")


# --------------------------------------------------------------------------- #
# STEP: dev_tunnel
# --------------------------------------------------------------------------- #
def step_dev_tunnel() -> None:
    step_header("Checking Dev Tunnels CLI (devtunnel)")
    dt = find_executable("devtunnel", "devtunnel.exe")
    # Also check the winget per-user install location used by supervisor.ps1.
    if not dt:
        # Both install locations: winget puts it under WinGet\Links, and the direct
        # download setup_devtunnel.ps1 falls back to puts it under LOCALAPPDATA\devtunnel
        # and appends THAT to the user PATH -- which a running session cannot see.
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        cand = local / "Microsoft" / "WinGet" / "Links" / "devtunnel.exe"
        if not cand.exists():
            cand = local / "devtunnel" / "devtunnel.exe"
        if cand.exists():
            dt = str(cand)

    # Prefer the user's chosen tunnel name (MCP_TUNNEL_NAME in .env) when set, else a
    # fresh, machine-unique default (DEFAULT_TUNNEL_NAME + a per-machine suffix, so
    # different clones/machines never collide in devtunnels.ms's GLOBAL namespace, and
    # the default never identifies anyone). Without honoring a recorded name,
    # provisioning a renamed setup that happens to be missing MCP_TUNNEL_URL would
    # target the DEFAULT tunnel, and _write_tunnel_to_env would then overwrite
    # MCP_TUNNEL_NAME -- silently flipping a renamed setup back to the default.
    # Mirrors setup_devtunnel.ps1, which reads MCP_TUNNEL_NAME from .env.
    #
    # Privacy guard: if the recorded name is itself identifying (leaks an org/user
    # token to the tunnel service), it must NOT be reused -- fall through to the fresh
    # safe name instead, and tell the user the public URL will change.
    recorded = _read_env_value("MCP_TUNNEL_NAME")
    safe_default = "%s-%s" % (DEFAULT_TUNNEL_NAME, _machine_suffix())
    if recorded and _is_identifying_tunnel_name(recorded):
        log("    NOTICE: the previous Dev Tunnel name ('%s') is identifying (it leaks" % recorded)
        log("            an organization/user token to the dev tunnel service) and will be")
        log("            replaced with a private name for this machine.")
        log("            The PUBLIC URL will change -- after this run, re-paste the new")
        log("            MCP_TUNNEL_URL into the Copilot Studio MCP connector.")
        log("            Manual cleanup of the old tunnel (optional; not done automatically):")
        log("                devtunnel delete %s" % recorded)
        tunnel = safe_default
    else:
        tunnel = recorded or safe_default

    # This step must NEVER wall a clean-PC novice. The script that actually
    # installs devtunnel (without winget) and walks the interactive Microsoft
    # sign-in is setup_devtunnel.ps1, which runs LATER at quickstart STEP 4 --
    # unreachable if we abort here with ActionNeeded. So when devtunnel is not
    # ready yet we only WARN and return normally (this step is marked done); the
    # real install/login happens at STEP 4.
    if not dt:
        log("    WARN: devtunnel not ready yet -- quickstart STEP 4 "
            "(setup_devtunnel.ps1) will install and sign you in; nothing to do now.")
        log("          (devtunnel is only required if a REMOTE client such as "
            "Copilot Studio must reach this server; skip it for local-only use.)")
        return

    log("    OK: devtunnel found at " + dt)
    anon = _anon_opt_in()
    log("    To expose the server to Copilot Studio, run this sequence:")
    log("      devtunnel user login                                 # opens Microsoft login (MFA)")
    if anon:
        log("      devtunnel create %s --allow-anonymous" % tunnel)
    else:
        log("      devtunnel create %s" % tunnel)
    log("      devtunnel port create %s -p 8000 --protocol http" % tunnel)
    # --tenant IS A FLAG, NOT A VALUE (D16; `devtunnel access create --help`, CLI 1.0.1516:
    # "Allow or deny all users in the current Entra tenant"). The text told people to append a
    # tenant id, which the CLI takes as a stray argument.
    if anon:
        log("      devtunnel access create %s -p 8000 --anonymous" % tunnel)
    else:
        log("      devtunnel access create %s -p 8000 --tenant   # the signed-in account's Entra tenant"
            % tunnel)
    log("      devtunnel host %s" % tunnel)
    if not anon:
        log("    NOTE: MCP_TUNNEL_ALLOW_ANONYMOUS is not set to 1, so anonymous access is NOT")
        log("          granted, and an anonymous grant already on the tunnel is REMOVED. A remote")
        log("          client (e.g. Copilot Studio) will NOT be able to reach this tunnel until")
        log("          quickstart.bat asks how it may connect (A = anonymous, gated only by the")
        log("          MCP_API_KEY app-layer key; T = the signed-in account's Entra tenant,")
        log("          'devtunnel access create <name> --tenant').")

    # Whether the tunnel has actually been created/logged-in is something we
    # cannot complete unattended: 'devtunnel user login' needs an interactive
    # Microsoft sign-in (and usually MFA). If the user has not logged in yet,
    # WARN and return normally -- setup_devtunnel.ps1 at STEP 4 does the login.
    logged_in = _devtunnel_logged_in(dt)
    if not logged_in:
        log("    WARN: devtunnel not ready yet -- quickstart STEP 4 "
            "(setup_devtunnel.ps1) will install and sign you in; nothing to do now.")
        log("          (devtunnel is installed but not signed in yet; the STEP 4 "
            "script runs 'devtunnel user login' interactively.)")
        return
    log("    OK: devtunnel reports a signed-in user.")

    # Short-circuit: if .env already carries a URL THIS MACHINE minted, the tunnel was
    # provisioned on a previous run here. Do NOT re-host (each host costs ~30s) on every
    # resume -- just report it and move on.
    #
    # "ALREADY SET" WAS NOT THE QUESTION. This tested only that the value was non-empty, and
    # the comment justified that with "provisioned on a previous run" -- true only if the
    # previous run was on this machine. A .env carried over from another PC, which is how a
    # new machine is normally set up, also has a non-empty URL, so setup skipped provisioning
    # and left the old machine's tunnel address in place. Everything reported OK; the URL
    # pasted into Copilot Studio pointed at a tunnel this machine does not host, and the
    # operator was left with a working-looking setup that cannot connect. Reported from a
    # real new-PC setup, 2026-09-08.
    #
    # So the URL is trusted only when MCP_TUNNEL_HOST says this machine minted it. A URL with
    # no host recorded is of unknown provenance: re-provision, because hosting the SAME tunnel
    # name yields the same URL, so the cost of being wrong is ~30s while the cost of trusting
    # it is the silent failure above. The re-host writes MCP_TUNNEL_HOST, so this is a
    # one-time correction per machine.
    #
    # AND A .env THAT PROVABLY CAME FROM ANOTHER MACHINE LOSES ITS TUNNEL KEYS FIRST (D7). This
    # step used to re-provision with the RECORDED name and then stamp it as this machine's --
    # so when the same Microsoft account owned that tunnel, BOTH machines hosted it and Copilot
    # Studio's calls were split between them; setup_devtunnel.ps1's fix was undone here, one
    # step earlier. The same test as there ("provably" = a host stamp naming another machine,
    # or no stamp and a generated name with another machine's suffix), and the same classifier:
    # tools/env_portability.machine_bound_keys_in decides which keys cannot travel.
    env_path = ROOT / ".env"
    env_text = env_file.read_text(env_path)
    foreign = _foreign_env_reason(env_text)
    if foreign:
        aside = _set_aside_machine_bound_tunnel_keys(env_path, env_text)
        if aside is None:
            log("    WARN: .env came from another machine (%s), and tools/env_portability.py "
                "could not be used to decide what to set aside. Not provisioning here; quickstart "
                "STEP 4 (setup_devtunnel.ps1) will handle it." % foreign)
            return
        log("    NOTE: .env came from another machine: %s." % foreign)
        log("          Hosting that tunnel here too would make both machines serve one URL, so")
        log("          these are set aside (kept as comments): %s" % (", ".join(aside) or "(none)"))
        log("          This machine gets its own tunnel; paste its NEW URL into Copilot Studio.")
        tunnel = safe_default

    existing_url = _read_env_value("MCP_TUNNEL_URL")
    recorded_host = _read_env_value("MCP_TUNNEL_HOST")
    if existing_url and recorded_host and _host_is_mine(recorded_host):
        log("    OK: MCP_TUNNEL_URL already set in .env by this machine (%s); skipping re-host."
            % existing_url)
        return
    if existing_url and recorded_host:
        log("    NOTE: .env carries a MCP_TUNNEL_URL minted on '%s', not this machine (%s)."
            % (recorded_host, _this_host()))
        log("          That tunnel is not hosted here, so the URL would not connect. "
            "Re-provisioning for this machine.")
    elif existing_url:
        log("    NOTE: .env carries a MCP_TUNNEL_URL with no record of which machine minted "
            "it. Re-provisioning to be sure it belongs to this one.")

    # Signed in and no URL recorded yet: finish the rest unattended -- create the
    # tunnel + port + access (idempotent), briefly host it to obtain the public
    # URL, then record MCP_TUNNEL_NAME/MCP_TUNNEL_URL in .env. Any failure here
    # only WARNs and returns normally (STEP 4's setup_devtunnel.ps1 is the real
    # provisioner) -- and it must NEVER blank an existing MCP_TUNNEL_URL.
    try:
        _provision_dev_tunnel(dt, tunnel)
    except Exception as e:  # noqa: BLE001 - never crash/wall bootstrap on a tunnel hiccup
        log("    WARN: could not auto-provision the dev tunnel (%s). quickstart "
            "STEP 4 (setup_devtunnel.ps1) will provision it; nothing to do now." % e)
        return


def _anon_opt_in() -> bool:
    """MCP_TUNNEL_ALLOW_ANONYMOUS opt-in gate for anonymous tunnel access.
    Default OFF: granting --allow-anonymous / --anonymous makes this server
    (which has file/shell tools) reachable by ANYONE on the internet, gated only
    by the app-layer MCP_API_KEY -- that must be a deliberate choice, not a
    silent default. Checked as a real environment variable first (so a value
    exported in the shell wins), falling back to the value recorded in .env
    (this step runs BEFORE .env is loaded into os.environ at step_verify).
    Accepts "1"/"true"/"yes" case-insensitively; anything else -- including
    unset -- is OFF."""
    v = os.environ.get("MCP_TUNNEL_ALLOW_ANONYMOUS")
    if v is None:
        v = _read_env_value("MCP_TUNNEL_ALLOW_ANONYMOUS")
    return (v or "").strip().lower() in ("1", "true", "yes")


#: `devtunnel access list` prints an allow entry as "+Anonymous [connect]" (measured on the
#: owner's machine; the same parse as setup_devtunnel.ps1's Test-AnonymousInListing).
_ANONYMOUS_GRANT_RE = re.compile(r"\+\s*Anonymous\b")


def _access_level_args(tunnel: str, level: int | None) -> list:
    return [tunnel] + (["-p", str(level)] if level else [])


def _grant_anonymous_access(dt: str, tunnel: str, port: int) -> None:
    """Grant anonymous connect at the tunnel and at its port. An "already exists"/"conflict"
    answer is success; anything else is a real error for the caller to report."""
    for level in (None, port):
        res = _dt_run(dt, "access", "create", *_access_level_args(tunnel, level), "--anonymous")
        text = (res.stdout or "") + (res.stderr or "")
        if res.returncode != 0 and not re.search(r"already exists|already in use|conflict", text, re.I):
            raise StepError("'devtunnel access create %s --anonymous' failed (rc=%d)"
                            % (" ".join(_access_level_args(tunnel, level)), res.returncode))


def _revoke_anonymous_access(dt: str, tunnel: str, port: int) -> list:
    """Reset every level whose access list shows an anonymous grant or cannot be read.
    Returns the levels reset (None = the tunnel itself). A failed reset raises: the grant may
    still be in place, and saying nothing would be the defect this exists to fix."""
    reset = []
    for level in (None, port):
        where = ("port %d of '%s'" % (level, tunnel)) if level else ("tunnel '%s'" % tunnel)
        res = _dt_run(dt, "access", "list", *_access_level_args(tunnel, level))
        listing = (res.stdout or "") + (res.stderr or "")
        if res.returncode == 0 and not _ANONYMOUS_GRANT_RE.search(listing):
            continue
        if res.returncode != 0:
            log("    could not read the access list of %s -> resetting it so no anonymous "
                "grant can remain" % where)
        else:
            log("    %s has an ANONYMOUS grant from an earlier choice -> removing it "
                "(access reset)" % where)
        r = _dt_run(dt, "access", "reset", *_access_level_args(tunnel, level))
        if r.returncode != 0:
            raise StepError("'devtunnel access reset' failed for %s (rc=%d), so an anonymous "
                            "grant may still be in place. quickstart STEP 4 tries again and "
                            "stops if it cannot remove it" % (where, r.returncode))
        reset.append(level)
    return reset


def _provision_dev_tunnel(dt: str, tunnel: str) -> None:
    """Ensure the tunnel exists, host it briefly to learn its public URL, and
    write MCP_TUNNEL_NAME/MCP_TUNNEL_URL into .env. Mirrors setup_devtunnel.ps1
    (host-then-read-then-write). Assumes the user is already signed in."""
    port = 8000
    anon = _anon_opt_in()

    # 1. Ensure the tunnel exists (idempotent). 'devtunnel show' succeeds only if
    #    the tunnel is already there; on failure/missing we create it + the port
    #    + (ONLY if opted in via MCP_TUNNEL_ALLOW_ANONYMOUS) anonymous access.
    if _dt_run(dt, "show", tunnel).returncode == 0:
        log("    OK: tunnel '%s' already exists (skipping create)." % tunnel)
    else:
        if anon:
            log("    Creating tunnel '%s' (anonymous-reachable; MCP_TUNNEL_ALLOW_ANONYMOUS=1)..." % tunnel)
            rc1 = _dt_run(dt, "create", tunnel, "--allow-anonymous").returncode
        else:
            log("    Creating tunnel '%s' (NOT anonymous-reachable)..." % tunnel)
            rc1 = _dt_run(dt, "create", tunnel).returncode
        rc2 = _dt_run(dt, "port", "create", tunnel, "-p", str(port), "--protocol", "http").returncode
        if rc1 != 0:
            # If create itself failed the tunnel won't be usable; bubble up so the
            # caller turns it into an ActionNeeded with the manual sequence.
            raise StepError("'devtunnel create %s' failed (rc=%d)." % (tunnel, rc1))
        if rc2 != 0:
            log("    WARN: port create returned non-zero (rc=%d); continuing -- it may "
                "already exist." % rc2)

    # ACCESS ON EVERY RUN, BOTH WAYS (D4). This step granted anonymous access when the tunnel
    # was CREATED and never looked again: an existing tunnel was not re-granted when anonymous
    # WAS chosen, and -- the dangerous half -- an anonymous grant from an earlier choice was
    # never REMOVED when it was not. Same rule as setup_devtunnel.ps1 (Resolve-AccessMode,
    # Revoke-AnonymousAccess): this step has no tenant input, so the mode is "anonymous" when
    # MCP_TUNNEL_ALLOW_ANONYMOUS opts in, else "none", and "none" resets any level (the tunnel,
    # or its port) whose access list shows +Anonymous -- or cannot be read, because then the
    # absence of a grant cannot be shown. A tenant grant on a level without an anonymous one is
    # left alone; the tenant grant itself is STEP 4's to apply (quickstart asks for it).
    if anon:
        _grant_anonymous_access(dt, tunnel, port)
    else:
        _revoke_anonymous_access(dt, tunnel, port)

    # 2. Obtain the public URL. A freshly-created tunnel has NO port URL in
    #    'devtunnel show' until it has been HOSTED at least once (Host
    #    connections must be >= 1). So if the URL is not there yet, start a host
    #    in the BACKGROUND, poll 'devtunnel show' for up to ~30s, then stop it.
    url = _dt_tunnel_url(dt, tunnel)
    if not url:
        log("    Hosting the tunnel briefly to obtain its public URL (a few seconds)...")
        host_proc = None
        try:
            host_proc = subprocess.Popen(
                [dt, "host", tunnel],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for _ in range(15):  # 15 * 2s = ~30s
                try:
                    host_proc.wait(timeout=2)
                    # Host exited early (e.g. it could not bind); stop polling.
                    break
                except subprocess.TimeoutExpired:
                    pass
                url = _dt_tunnel_url(dt, tunnel)
                if url:
                    break
        finally:
            # Stop the temporary host process; the supervisor (start_all) hosts
            # the tunnel for real later. We only needed it to mint the URL.
            if host_proc is not None and host_proc.poll() is None:
                host_proc.terminate()
                try:
                    host_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    host_proc.kill()

    # 3. Record name + URL in .env (preserving every other key/secret).
    _write_tunnel_to_env(tunnel, url)

    if url:
        log("    OK: dev tunnel public URL: " + url)
        log("    Recorded MCP_TUNNEL_NAME and MCP_TUNNEL_URL in .env.")
    else:
        # We could not parse a URL (host did not come up in time). The tunnel and
        # name are written; tell the user how to read the URL by hand. Still not
        # fatal -- everything else in .env is intact, and any PRE-EXISTING
        # MCP_TUNNEL_URL is preserved by _write_tunnel_to_env (never blanked).
        log("    WARN: tunnel '%s' is set up but no NEW port URL was parsed." % tunnel)
        log("          quickstart STEP 4 (setup_devtunnel.ps1) will provision the "
            "URL, or run 'devtunnel show %s' and copy the" % tunnel)
        log("          https://...devtunnels.ms URL into MCP_TUNNEL_URL in .env.")
        log("    Recorded MCP_TUNNEL_NAME in .env (any existing MCP_TUNNEL_URL kept).")


def _dt_run(dt: str, *args: str) -> subprocess.CompletedProcess:
    """Run a devtunnel subcommand, capturing output, never raising. Returns the
    CompletedProcess (rc 124-style sentinel on timeout/spawn failure)."""
    try:
        from tools.childproc import run as _run_child
        return _run_child([dt, *args], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(args=[dt, *args], returncode=124, stdout="", stderr="")


_TUNNEL_URL_RE = re.compile(
    r"https://[A-Za-z0-9-]+\.[A-Za-z0-9-]+\.devtunnels\.ms\S*"
)


def _dt_tunnel_url(dt: str, tunnel: str) -> str | None:
    """Parse the public https://...-8000.<region>.devtunnels.ms/ URL out of
    'devtunnel show'. Returns None if no URL is present yet."""
    res = _dt_run(dt, "show", tunnel)
    text = (res.stdout or "") + "\n" + (res.stderr or "")
    m = _TUNNEL_URL_RE.search(text)
    return m.group(0) if m else None


def _this_host() -> str:
    """The machine identity recorded beside a minted tunnel URL.

    A devtunnel URL is only reachable while some machine HOSTS that tunnel, so a URL is a
    fact about a machine, not about an account -- and .env travels between machines. This is
    what lets a later run tell "I minted this" from "I inherited this".

    platform.node() rather than COMPUTERNAME: same answer on Windows, and it does not return
    an empty string on a box where the variable is unset. Lowercased because Windows reports
    the name in either case depending on how it is read, and a case flip must not read as a
    different machine.
    """
    return (platform.node() or "").strip().lower()


def _host_is_mine(stamp: str | None) -> bool:
    """A host stamp names THIS machine: the current identity, or setup_devtunnel.ps1's legacy
    %COMPUTERNAME% stamp (D22), which is migrated on the next write rather than distrusted."""
    s = (stamp or "").strip().lower()
    return bool(s) and s in (_this_host(), (os.environ.get("COMPUTERNAME") or "").strip().lower())


def _env_text_value(text: str, key: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(key + "="):
            return s.split("=", 1)[1].strip()
    return ""


def _foreign_env_reason(text: str) -> str:
    """Why the .env `text` provably came from another machine, or "" when nothing proves it (D7).

    Mirrors setup_devtunnel.ps1 section [0]: a host stamp naming a different machine, or -- with
    no stamp -- a GENERATED name whose hash suffix is not this machine's. A custom name with no
    stamp proves nothing and is kept: dropping a name this machine really owns would change a
    working URL.

    DECIDED ON THE TEXT THAT WILL BE REWRITTEN, not through _read_env_value: a decision made from
    one read and acted on through another can disagree -- and did, in a test that stubbed the
    reader and so rewrote the real .env it had never looked at."""
    stamp = _env_text_value(text, "MCP_TUNNEL_HOST").lower()
    name = _env_text_value(text, "MCP_TUNNEL_NAME")
    if stamp and not _host_is_mine(stamp):
        return "its tunnel was recorded on '%s', not this machine ('%s')" % (stamp, _this_host())
    if not stamp and _is_generated_tunnel_name(name):
        m = re.match(r"^-([0-9a-f]+)", name.lower()[len(DEFAULT_TUNNEL_NAME):])
        if m and m.group(1) not in (_machine_suffix(), _legacy_machine_suffix()):
            return ("its tunnel name '%s' was generated on another machine (the suffix is not "
                    "this machine's)" % name)
    return ""


def _set_aside_machine_bound_tunnel_keys(env_path: Path, text: str) -> list | None:
    """Comment out, in `text` (the content of `env_path`), the MCP_TUNNEL_* keys
    tools/env_portability says cannot move to a new machine, and write it back atomically.
    Returns the keys set aside, or None when the classifier could not be used. The values stay
    readable as comments, as setup_devtunnel.ps1 leaves them."""
    try:
        from tools.env_portability import machine_bound_keys_in
    except Exception:  # noqa: BLE001
        return None
    try:
        keys = [k for k in machine_bound_keys_in(text) if k.startswith("MCP_TUNNEL_")]
    except Exception:  # noqa: BLE001
        return None
    if not keys:
        return []
    nl = env_file.newline_of(text)
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in keys:
            out.append("# set aside by bootstrap.py: made on another machine, not valid on this one")
            out.append("# " + line)
        else:
            out.append(line)
    env_file.atomic_write_text(env_path, nl.join(out) + nl)
    return keys


def _write_tunnel_to_env(tunnel: str, url: str | None) -> None:
    """Write MCP_TUNNEL_NAME (and MCP_TUNNEL_URL if known) into .env, preserving
    every other line. Strips any prior '# devtunnel (auto)' / MCP_TUNNEL_* lines
    first. Writes UTF-8 WITHOUT BOM and CRLF endings -- a BOM here previously
    broke the .env parser (PowerShell Set-Content -Encoding UTF8 writes EF BB BF
    which folds into the first key name).

    IMPORTANT: never DESTROY an existing MCP_TUNNEL_URL on a transient failure.
    If 'url' is None (we could not mint a URL this run) but .env already carries
    a non-empty MCP_TUNNEL_URL, we keep the existing value instead of dropping
    it -- a hosting hiccup must not silently un-configure a working tunnel."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        log("    WARN: .env not found; skipping MCP_TUNNEL_* write.")
        return
    # utf-8-sig tolerates a possible pre-existing BOM on read.
    existing = env_path.read_text(encoding="utf-8-sig").splitlines()

    # Preserve a prior non-empty MCP_TUNNEL_URL if we did not obtain one now.
    if not url:
        for ln in existing:
            if ln.startswith("MCP_TUNNEL_URL="):
                prev = ln.split("=", 1)[1].strip()
                if prev:
                    url = prev
                break

    kept = [
        ln for ln in existing
        if not (
            ln.startswith("# devtunnel (auto)")
            or ln.startswith("MCP_TUNNEL_NAME=")
            or ln.startswith("MCP_TUNNEL_URL=")
            or ln.startswith("MCP_TUNNEL_HOST=")
        )
    ]
    kept.append(
        "# devtunnel (auto) -- the public URL to register in Copilot Studio; "
        "supervisor hosts MCP_TUNNEL_NAME"
    )
    kept.append("MCP_TUNNEL_NAME=" + tunnel)
    if url:
        kept.append("MCP_TUNNEL_URL=" + url)
        # STAMPED ONLY BESIDE A URL. The host answers "who minted this URL", so writing it
        # without one would claim provenance for a value that is not there -- and a later run
        # would then trust an inherited URL that arrives afterwards. When the URL below is a
        # preserved earlier value rather than one minted now, this still names the machine
        # that is keeping it, which is the machine that must host it.
        kept.append("MCP_TUNNEL_HOST=" + _this_host())
    # CRLF endings, UTF-8 WITHOUT BOM, and ATOMIC (D28): tmp file + rename, never a truncate.
    env_file.atomic_write_text(env_path, "\r\n".join(kept) + "\r\n")


def _devtunnel_logged_in(dt: str) -> bool:
    """Best-effort: returns True only if 'devtunnel user show' clearly reports a
    logged-in account. Any error / 'not logged in' text -> False (we pause)."""
    try:
        from tools.childproc import run as _run_child
        out = _run_child([dt, "user", "show"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    text = (out.stdout + out.stderr).lower()
    if "not logged in" in text or "logged out" in text or "please log in" in text:
        return False
    # 'Logged in as ...' / an email address present -> treat as logged in.
    return "logged in" in text or "@" in text


# --------------------------------------------------------------------------- #
# STEP: gen_connector
# --------------------------------------------------------------------------- #
def _read_env_value(key: str) -> str | None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return None
    # utf-8-sig tolerates a leading BOM: a .env saved by PowerShell's `Set-Content
    # -Encoding UTF8` (PS 5.1) starts with EF BB BF, which plain utf-8 would fold into
    # the first key name ("﻿MCP_API_KEY") and make this reader miss it.
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if s.startswith(key + "="):
            return s.split("=", 1)[1].strip()
    return None


def step_gen_connector() -> None:
    step_header("Generating Copilot Studio connector helper (generated/copilot-connector.md)")
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out = GENERATED_DIR / "copilot-connector.md"
    out.write_text(_connector_markdown("<your MCP_API_KEY from .env>"), encoding="utf-8")
    log("    OK: wrote " + str(out))


def _connector_markdown(bearer_value: str) -> str:
    return f"""# Add this MCP server to a Copilot Studio agent

Generated by `scripts/bootstrap.py`. This is a helper checklist -- the actual
Copilot Studio configuration happens in your browser and requires your Microsoft
sign-in. The bootstrap does NOT automate the Studio UI.

## What you need

- The server running locally:  `http://127.0.0.1:8000/mcp`  (start with `.\\scripts\\start.ps1`)
- A public HTTPS URL via Dev Tunnels:
  `https://<your-tunnel>-8000.<region>.devtunnels.ms/mcp`
- Your Bearer key (from `.env`, `MCP_API_KEY`):

      Authorization: Bearer {bearer_value}

## Steps in Copilot Studio

1. Open your agent in Copilot Studio.
2. Go to **Tools -> Add a tool -> Model Context Protocol**.
3. **Server URL**: paste your tunnel URL ending in `/mcp`
   (`https://<your-tunnel>-8000.<region>.devtunnels.ms/mcp`).
4. **Authentication**: choose *API key (manual)*.
   - Header name: `Authorization`
   - Header value: `Bearer {bearer_value}`
5. Save. Studio will list the tools the server exposes
   (run `list_my_tools` from any client to preview the catalog).
6. While you are there, enable the **native M365 connectors**
   (mail / calendar / Teams / SharePoint) you need -- cloud capabilities come
   from those connectors, local capabilities from this companion.
7. **Publish to yourself only** first, then test, then widen access.

## Notes

- `--allow-anonymous` on the tunnel is now an EXPLICIT opt-in (env var
  `MCP_TUNNEL_ALLOW_ANONYMOUS=1`; default OFF). When granted, the app-layer
  Bearer key above plus a per-IP `unlock(password)` for mutating tools still
  apply -- but the tunnel itself is then reachable by anyone on the internet,
  so only opt in if you accept that. The hardened alternative is Entra/tenant-
  scoped access (`devtunnel access create <name> --tenant`: `--tenant` is a flag
  meaning the Entra tenant of the account devtunnel is signed in with; it takes
  no id).
- If Microsoft changes the Studio UI, the field names may differ slightly but
  the three inputs are always: server URL, header name, header value.
- Keep the Bearer key secret. Rotate it by editing `MCP_API_KEY` in `.env` and
  restarting the server.
"""


# --------------------------------------------------------------------------- #
# STEP: verify
# --------------------------------------------------------------------------- #
def step_verify() -> None:
    step_header("Verifying environment")

    # 1. Required .env keys must be present and non-placeholder.
    required = ["MCP_API_KEY"]
    missing = []
    for k in required:
        v = _read_env_value(k)
        if not v or v.startswith("replace"):
            missing.append(k)
    unlock_plain = _read_env_value("MCP_UNLOCK_PASSWORD")
    unlock_protected = _read_env_value(UNLOCK_PASSWORD_PROTECTED_VAR)
    if not unlock_plain and not unlock_protected:
        missing.append("MCP_UNLOCK_PASSWORD or " + UNLOCK_PASSWORD_PROTECTED_VAR)
    if missing:
        raise StepError(
            ".env is missing or has placeholder values for: " + ", ".join(missing)
            + ". Re-run quickstart.bat (or setup.bat) (the gen_env step fills these)."
        )
    log("    OK: .env has required keys (MCP_API_KEY and unlock password)")

    # 2. Import main.py and report the tool count. main.py reads MCP_API_KEY from
    #    the environment at import, so load .env into os.environ first.
    _load_dotenv_into_env(ROOT / ".env")
    count = _count_tools_via_subprocess()
    if count is _IMPORT_TIMED_OUT:
        raise VerifyTimedOut(
            "Importing main.py took longer than %d seconds, so it was stopped before it "
            "finished. Nothing is known to be broken: this happens on a slow PC, often while "
            "antivirus scans the freshly installed packages for the first time. The installed "
            "packages are KEPT. Wait a minute and re-run quickstart.bat (or setup.bat); a second "
            "import is much faster." % VERIFY_IMPORT_TIMEOUT_S)
    if count is None:
        raise StepError(
            "Could not import main.py to count tools. Check that dependencies "
            "installed correctly, then re-run quickstart.bat (or setup.bat)."
        )
    log("    OK: main.py imported; registered tool count = %d" % count)


def _load_dotenv_into_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    # utf-8-sig tolerates a leading BOM: a .env saved by PowerShell's `Set-Content
    # -Encoding UTF8` (PS 5.1) starts with EF BB BF, which plain utf-8 would fold into
    # the first key name ("﻿MCP_API_KEY") and make this reader miss it.
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        # OVERRIDE any pre-set environment variable with the .env value. The
        # verification must reflect what the SERVER will read from .env at
        # runtime; a stale exported MCP_* var in the parent shell would otherwise
        # shadow .env (os.environ.setdefault keeps the pre-set value) and make us
        # verify against the wrong secret. .env is the source of truth here.
        os.environ[k.strip()] = v.strip()


#: How long the verify import of main.py may take (D26). MEASURED 2026-09-24 on the owner's PC:
#: a fresh clone (no __pycache__) importing main with the real .venv took 8.7 s, 8.8 s and
#: 10.2 s. The old cap of 120 s was ~12x that; a slow PC with on-access antivirus scanning a
#: few thousand freshly installed .pyc files for the first time can plausibly spend a good part
#: of it. 300 s is ~30x the measurement: generous for a one-off first import, and still short
#: enough that a genuinely hung import is reported within a coffee break.
VERIFY_IMPORT_TIMEOUT_S = 300

#: Sentinel: the import was stopped by the timeout, as opposed to failing.
_IMPORT_TIMED_OUT = object()


class VerifyTimedOut(StepError):
    """verify could not finish in time. NOT evidence that the install is broken, so the
    driver must not clear install_deps over it (D26): clearing it turned a slow first import
    into a full reinstall on every run -- each reinstall re-triggering the same antivirus scan
    that made the import slow."""


def _count_tools_via_subprocess():
    """Import main.py in a CHILD process (using the venv interpreter) and print
    the registered tool count. A child process keeps main.py's import side
    effects (and its heavy deps) out of the bootstrap process, and uses the
    venv where the deps were actually installed.

    Returns the count, None on a failed import, or _IMPORT_TIMED_OUT."""
    py = str(venv_python())
    # %r, NOT r'%s' (D23): a raw string literal cannot hold a path with an apostrophe in it
    # (<repo> under a folder named with a quote), so verify failed on every run there and cleared
    # install_deps each time. repr() produces a literal that round-trips any path.
    code = (
        "import os, sys; "
        "sys.path.insert(0, %r); "
        "import main; "
        "tm = getattr(main.mcp, '_tool_manager', None); "
        "n = len(tm._tools) if tm is not None else len(main.TOOLS); "
        "print(n)" % str(ROOT)
    )
    env = dict(os.environ)
    try:
        from tools.childproc import run as _run_child
        res = _run_child([py, "-c", code], cwd=str(ROOT), env=env,
                         timeout=VERIFY_IMPORT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _IMPORT_TIMED_OUT
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        # Explain the wall of traceback to a novice BEFORE dumping it, so they
        # know the stack trace below is the reason main.py would not load (not
        # some unrelated crash of the bootstrap itself).
        sys.stderr.write(
            "The server code (main.py) failed to load; the technical error follows:\n"
        )
        sys.stderr.write(res.stderr)
        return None
    try:
        return int(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------------------------- #
# Step registry + driver
# --------------------------------------------------------------------------- #
# Order matters: each step may depend on earlier ones (venv -> deps -> env ...).
STEPS = [
    ("ensure_venv", step_ensure_venv),
    ("install_deps", step_install_deps),
    ("gen_env", step_gen_env),
    ("check_edge", step_check_edge),
    ("dev_tunnel", step_dev_tunnel),
    ("gen_connector", step_gen_connector),
    ("verify", step_verify),
]


#: Never skipped, whatever the checkpoint says. `verify` is a CHECK -- it imports main.py in a
#: subprocess and counts the tools -- and a check that is skipped has stopped being one. Its
#: flag can also arrive from another machine inside a copied .setup/state.json.
ALWAYS_REVALIDATE = ("verify",)

#: Cleared when a revalidating step fails, so the next run rebuilds rather than skipping back to
#: the same failure. These are the steps that produce what `verify` verifies.
REPAIRED_BY_RERUN = ("ensure_venv", "install_deps")


def run_all(steps=STEPS, state=None, state_file=STATE_FILE) -> int:
    """Resumable driver. Returns a process exit code.

    Skips steps already marked done. On ActionNeeded -> print instruction, save
    state, exit non-zero (the step stays pending so the next run resumes here).
    On StepError -> same resume behavior with a FAILED message.
    """
    if state is None:
        state = load_state(state_file)

    # A DONE FLAG IS A MEMORY OF AN ACT, NOT EVIDENCE OF ITS RESULT. state.json travels with a
    # folder copy, a OneDrive sync or a ZIP restore; .venv does not, or arrives broken. Skipping
    # on the flag alone let STEP 1 report success on a machine with no interpreter, after which
    # supervisor.ps1 falls back to bare `python` -- the Store alias, on a fresh Windows box --
    # and every later symptom points somewhere else.
    #
    # Only the steps that make or need the venv are cleared. The other four produce their own
    # artefacts and re-running them is not free.
    if not VENV_PYTHON.exists():
        cleared = [n for n in ("ensure_venv", "install_deps", "verify")
                   if state.get("done", {}).pop(n, None)]
        if cleared:
            log("    .venv is missing, so these are not done after all: %s" % ", ".join(cleared))
            save_state(state, state_file)
    elif is_done(state, "ensure_venv") and not _venv_usable():
        # EXISTS IS NOT USABLE (D20, D8). A .venv whose python cannot run, or runs a Python too
        # old for the requirements, passed the existence check above and was then skipped on
        # its flag -- so ensure_venv, the step that repairs exactly this, never ran again.
        cleared = [n for n in ("ensure_venv", "install_deps", "verify")
                   if state.get("done", {}).pop(n, None)]
        log("    .venv exists but cannot run or is older than Python %d.%d, so these are not "
            "done after all: %s" % (MIN_PYTHON[0], MIN_PYTHON[1], ", ".join(cleared)))
        save_state(state, state_file)

    # A DONE FLAG FOR A DIFFERENT requirements.txt IS NOT DONE (D5). Cleared, not merely
    # warned about: a `git pull` that adds a dependency is the normal way this file changes,
    # and the only thing that installs it is this step running again.
    if deps_are_stale(state):
        state.get("done", {}).pop("install_deps", None)
        log("    requirements.txt has changed since dependencies were last installed "
            "(or this install predates that record); installing them again.")
        save_state(state, state_file)

    # First-line resume banner: when at least one step is already done, tell the
    # user up front how far along we are and which step we resume from, so an
    # interrupted install reads as "continuing" rather than "starting over".
    done_count = sum(1 for name, _ in steps if is_done(state, name))
    total = len(steps)
    if 0 < done_count < total:
        next_pending = next(name for name, _ in steps if not is_done(state, name))
        log("RESUMING: %d/%d steps already done -- continuing from '%s'"
            % (done_count, total, next_pending))

    for name, fn in steps:
        # A CHECK THAT IS SKIPPED IS NOT A CHECK. `verify` imports main.py and counts the tools,
        # so it is the step that catches an empty or broken venv -- and it was being skipped on
        # a flag that can arrive from another machine in a copied .setup/state.json, while
        # setup.bat has meanwhile created a fresh EMPTY venv with uv (so the "is .venv missing"
        # guard above never fires on that path). It is cheap next to what it protects.
        if is_done(state, name) and name not in ALWAYS_REVALIDATE:
            log("--- %-14s already done (skipping)" % name)
            continue
        try:
            _call_step(fn, state, state_file)
            mark_done(state, name, state_file)
        except ActionNeeded as e:
            save_state(state, state_file)
            log("")
            log("ACTION NEEDED: %s; then re-run quickstart.bat (or setup.bat)" % str(e))
            log("(Progress saved. Completed steps will be skipped on the next run.)")
            repeat_minted_secrets()
            return 2
        except StepError as e:
            # RE-RUNNING MUST REPAIR, NOT RETRY THE SAME SKIP. When verification fails, the steps
            # that were supposed to produce what it verifies are no longer trustworthy -- whatever
            # their flags say -- so they are cleared. Otherwise the advice below sends the reader
            # back into a run that skips straight to the same failure.
            # EXCEPT WHEN IT DID NOT FAIL BUT RAN OUT OF TIME (D26): a timeout says nothing about
            # the install, and clearing it made a slow PC reinstall everything on every run.
            if name in ALWAYS_REVALIDATE and not isinstance(e, VerifyTimedOut):
                for stale in REPAIRED_BY_RERUN:
                    if state.get("done", {}).pop(stale, None):
                        log("    (cleared '%s' so the next run rebuilds it)" % stale)
            save_state(state, state_file)
            log("")
            log("FAILED at step '%s': %s" % (name, str(e)))
            log("(Progress saved. Re-run quickstart.bat (or setup.bat) to retry this step.)")
            repeat_minted_secrets()
            return 1
    log("")
    log("All steps complete. Environment is ready.")
    log("Next: start the server with  .\\scripts\\start.ps1")
    repeat_minted_secrets()
    return 0


def run_only(step_name: str, steps=STEPS, state=None, state_file=STATE_FILE) -> int:
    if state is None:
        state = load_state(state_file)
    table = dict(steps)
    if step_name not in table:
        log("Unknown step: %s" % step_name)
        log("Known steps: " + ", ".join(n for n, _ in steps))
        return 1
    try:
        _call_step(table[step_name], state, state_file)
        mark_done(state, step_name, state_file)
    except ActionNeeded as e:
        save_state(state, state_file)
        log("ACTION NEEDED: %s; then re-run quickstart.bat (or setup.bat)" % str(e))
        return 2
    except StepError as e:
        save_state(state, state_file)
        log("FAILED at step '%s': %s" % (step_name, str(e)))
        return 1
    log("Step '%s' complete." % step_name)
    return 0


def print_status(steps=STEPS, state=None, state_file=STATE_FILE) -> int:
    if state is None:
        state = load_state(state_file)
    log("Bootstrap status (state file: %s)" % state_file)
    for name, _ in steps:
        log("  [%s] %s" % ("x" if is_done(state, name) else " ", name))
    pending = [n for n, _ in steps if not is_done(state, n)]
    if pending:
        log("Pending: " + ", ".join(pending))
    else:
        log("All steps done.")
    return 0


def reset_state(state_file: Path = STATE_FILE) -> int:
    if state_file.exists():
        state_file.unlink()
        log("Cleared saved progress: %s" % state_file)
    else:
        log("No saved progress to clear (%s does not exist)." % state_file)
    log("Note: --reset only clears bootstrap progress. It does NOT delete .venv, "
        ".env, or anything installed on the system.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Resumable environment bootstrap for m365-copilot-companion-mcp.",
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="print each step done/pending, change nothing")
    g.add_argument("--reset", action="store_true", help="clear saved progress, change nothing on the system")
    g.add_argument("--only", metavar="STEP", help="run a single step by name")
    g.add_argument("--check-deps", action="store_true",
                   help="exit 3 if requirements.txt changed since the last dependency "
                        "install (so setup.bat must run), else 0; changes nothing")
    args = parser.parse_args(argv)

    if args.check_deps:
        # FOR THE DAILY START PATH (D5). start_all.ps1 never runs bootstrap, so a pull that adds
        # a dependency is only installed by the next setup/quickstart. This lets a launcher ask
        # the question in a few milliseconds without importing anything heavy.
        state = load_state()
        if deps_are_stale(state) or not is_done(state, "install_deps"):
            log("requirements.txt differs from the last dependency install -- run setup.bat.")
            return 3
        log("Dependencies match requirements.txt.")
        return 0
    if args.status:
        return print_status()
    if args.reset:
        return reset_state()
    if args.only:
        return run_only(args.only)
    return run_all()


if __name__ == "__main__":
    # SAY WHERE THE RECORD IS, FIRST. An operator whose window vanished has nothing to read
    # and nothing to send; the path has to be on screen before anything can go wrong, not
    # printed at the end where a crash never reaches it.
    log("Transcript: %s" % TRANSCRIPT)

    # AN UNHANDLED TRACEBACK IS THE CASE THIS IS FOR. ActionNeeded and StepError already go
    # through log() and therefore into the transcript; a crash does not, and a crash is exactly
    # what leaves an operator saying "it fell over" with no evidence. Record it, then re-raise
    # so the exit code and the console output are unchanged.
    def _record_crash(exc_type, exc, tb):
        import traceback
        try:
            _transcribe("UNHANDLED %s: %s" % (exc_type.__name__, exc))
            for line in traceback.format_exception(exc_type, exc, tb):
                _transcribe(line.rstrip())
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _record_crash
    sys.exit(main())
