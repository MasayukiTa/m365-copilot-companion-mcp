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
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

# Repo root = parent of this scripts/ dir. Everything is resolved against it so
# the bootstrap behaves identically regardless of the caller's working dir.
ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".setup"
STATE_FILE = STATE_DIR / "state.json"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"  # Windows layout
GENERATED_DIR = ROOT / "generated"


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
def log(msg: str) -> None:
    print(msg, flush=True)


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
def step_ensure_venv() -> None:
    step_header("Ensuring virtual environment (.venv)")
    if VENV_PYTHON.exists():
        log("    OK: .venv already present (skipping)")
        return
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
def step_install_deps() -> None:
    step_header("Installing Python dependencies (requirements.txt)")
    py = str(venv_python())
    req = ROOT / "requirements.txt"
    if not req.exists():
        raise StepError("requirements.txt not found at repo root.")

    # Best-effort pip upgrade; never fatal.
    subprocess.call([py, "-m", "pip", "install", "--upgrade", "pip", "--quiet"])

    rc = subprocess.call([py, "-m", "pip", "install", "-r", str(req)])
    if rc != 0:
        raise StepError(
            "pip install -r requirements.txt failed (network or a wheel build). "
            "Re-running setup.bat retries this step."
        )

    # IMPORTANT: 'playwright' is pulled in (for the optional relay/bridge), but
    # we deliberately DO NOT run 'playwright install'. The relay attaches to an
    # already-running browser via connect_over_cdp (CDP), so it uses the user's
    # existing Edge/Chrome -- it never drives a Playwright-managed browser. A
    # 'playwright install' would download ~400MB of browser binaries for nothing
    # and can require extra permissions. So: no browser download here, on purpose.
    log("    OK: dependencies installed (no Playwright browser download -- CDP attach only)")


# --------------------------------------------------------------------------- #
# STEP: gen_env
# --------------------------------------------------------------------------- #
def step_gen_env() -> None:
    step_header("Preparing .env")
    env_path = ROOT / ".env"
    example = ROOT / ".env.example"

    if env_path.exists():
        # Never overwrite an existing .env -- it holds the user's live secrets.
        log("    OK: .env already exists (left untouched)")
        return

    api_key = secrets.token_hex(20)       # 40 hex chars
    unlock_pw = secrets.token_hex(8)      # 16 hex chars

    if example.exists():
        lines = example.read_text(encoding="utf-8").splitlines()
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
            out_lines.append("MCP_UNLOCK_PASSWORD=" + unlock_pw)
        else:
            # Keep MCP_ALLOWED_BASE=~ and leave the agent-URL vars commented as-is.
            out_lines.append(line)

    # Note: the MCP_*_AGENT_URL / bridge vars stay commented in .env.example, so
    # they remain commented here too. They are optional and embed tenant GUIDs;
    # the user fills them in only if they use the relay/bridge.
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    log("    OK: wrote .env with fresh random MCP_API_KEY and MCP_UNLOCK_PASSWORD")
    log("    Your Bearer token (MCP_API_KEY): " + api_key)
    log("    Your unlock password:           " + unlock_pw)
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
        cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "devtunnel.exe"
        if cand.exists():
            dt = str(cand)

    if not dt:
        raise ActionNeeded(
            "Dev Tunnels CLI (devtunnel) is not installed. Install it per-user "
            "(no admin):  winget install Microsoft.devtunnel  -- then re-run "
            "setup.bat. (devtunnel is only required if a REMOTE client such as "
            "Copilot Studio must reach this server; skip it for local-only use.)"
        )

    log("    OK: devtunnel found at " + dt)
    tunnel = "m365-copilot-companion"
    log("    To expose the server to Copilot Studio, run this sequence:")
    log("      devtunnel user login                                 # opens Microsoft login (MFA)")
    log("      devtunnel create %s --allow-anonymous" % tunnel)
    log("      devtunnel port create %s -p 8000 --protocol http" % tunnel)
    log("      devtunnel access create %s -p 8000 --anonymous" % tunnel)
    log("      devtunnel host %s" % tunnel)

    # Whether the tunnel has actually been created/logged-in is something we
    # cannot complete unattended: 'devtunnel user login' needs an interactive
    # Microsoft sign-in (and usually MFA). If the user has not logged in yet,
    # pause here so the next run can re-verify.
    logged_in = _devtunnel_logged_in(dt)
    if not logged_in:
        raise ActionNeeded(
            "devtunnel is installed but you are not signed in. Run "
            "'devtunnel user login' (this opens a Microsoft sign-in, usually "
            "with MFA), then run the create/port/access/host sequence printed "
            "above, and re-run setup.bat. This login cannot be automated."
        )
    log("    OK: devtunnel reports a signed-in user.")


def _devtunnel_logged_in(dt: str) -> bool:
    """Best-effort: returns True only if 'devtunnel user show' clearly reports a
    logged-in account. Any error / 'not logged in' text -> False (we pause)."""
    try:
        out = subprocess.run(
            [dt, "user", "show"],
            capture_output=True, text=True, timeout=20,
        )
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
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith(key + "="):
            return s.split("=", 1)[1].strip()
    return None


def step_gen_connector() -> None:
    step_header("Generating Copilot Studio connector helper (generated/copilot-connector.md)")
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    api_key = _read_env_value("MCP_API_KEY") or "<your MCP_API_KEY from .env>"
    out = GENERATED_DIR / "copilot-connector.md"
    out.write_text(_connector_markdown(api_key), encoding="utf-8")
    log("    OK: wrote " + str(out))


def _connector_markdown(api_key: str) -> str:
    return f"""# Add this MCP server to a Copilot Studio agent

Generated by `scripts/bootstrap.py`. This is a helper checklist -- the actual
Copilot Studio configuration happens in your browser and requires your Microsoft
sign-in. The bootstrap does NOT automate the Studio UI.

## What you need

- The server running locally:  `http://127.0.0.1:8000/mcp`  (start with `.\\start.ps1`)
- A public HTTPS URL via Dev Tunnels:
  `https://<your-tunnel>-8000.<region>.devtunnels.ms/mcp`
- Your Bearer key (from `.env`, `MCP_API_KEY`):

      Authorization: Bearer {api_key}

## Steps in Copilot Studio

1. Open your agent in Copilot Studio.
2. Go to **Tools -> Add a tool -> Model Context Protocol**.
3. **Server URL**: paste your tunnel URL ending in `/mcp`
   (`https://<your-tunnel>-8000.<region>.devtunnels.ms/mcp`).
4. **Authentication**: choose *API key (manual)*.
   - Header name: `Authorization`
   - Header value: `Bearer {api_key}`
5. Save. Studio will list the tools the server exposes
   (run `list_my_tools` from any client to preview the catalog).
6. While you are there, enable the **native M365 connectors**
   (mail / calendar / Teams / SharePoint) you need -- cloud capabilities come
   from those connectors, local capabilities from this companion.
7. **Publish to yourself only** first, then test, then widen access.

## Notes

- `--allow-anonymous` on the tunnel is safe: this server still enforces the
  Bearer key above plus a per-IP `unlock(password)` for mutating tools.
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
    required = ["MCP_API_KEY", "MCP_UNLOCK_PASSWORD"]
    missing = []
    for k in required:
        v = _read_env_value(k)
        if not v or v.startswith("replace"):
            missing.append(k)
    if missing:
        raise StepError(
            ".env is missing or has placeholder values for: " + ", ".join(missing)
            + ". Re-run setup.bat (the gen_env step fills these)."
        )
    log("    OK: .env has required keys (" + ", ".join(required) + ")")

    # 2. Import main.py and report the tool count. main.py reads MCP_API_KEY from
    #    the environment at import, so load .env into os.environ first.
    _load_dotenv_into_env(ROOT / ".env")
    count = _count_tools_via_subprocess()
    if count is None:
        raise StepError(
            "Could not import main.py to count tools. Check that dependencies "
            "installed correctly, then re-run setup.bat."
        )
    log("    OK: main.py imported; registered tool count = %d" % count)


def _load_dotenv_into_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _count_tools_via_subprocess() -> int | None:
    """Import main.py in a CHILD process (using the venv interpreter) and print
    the registered tool count. A child process keeps main.py's import side
    effects (and its heavy deps) out of the bootstrap process, and uses the
    venv where the deps were actually installed."""
    py = str(venv_python())
    code = (
        "import os, sys; "
        "sys.path.insert(0, r'%s'); "
        "import main; "
        "tm = getattr(main.mcp, '_tool_manager', None); "
        "n = len(tm._tools) if tm is not None else len(main.TOOLS); "
        "print(n)" % str(ROOT)
    )
    env = dict(os.environ)
    try:
        res = subprocess.run(
            [py, "-c", code],
            capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
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


def run_all(steps=STEPS, state=None, state_file=STATE_FILE) -> int:
    """Resumable driver. Returns a process exit code.

    Skips steps already marked done. On ActionNeeded -> print instruction, save
    state, exit non-zero (the step stays pending so the next run resumes here).
    On StepError -> same resume behavior with a FAILED message.
    """
    if state is None:
        state = load_state(state_file)

    for name, fn in steps:
        if is_done(state, name):
            log("--- %-14s already done (skipping)" % name)
            continue
        try:
            fn()
            mark_done(state, name, state_file)
        except ActionNeeded as e:
            save_state(state, state_file)
            log("")
            log("ACTION NEEDED: %s; then re-run setup.bat" % str(e))
            log("(Progress saved. Completed steps will be skipped on the next run.)")
            return 2
        except StepError as e:
            save_state(state, state_file)
            log("")
            log("FAILED at step '%s': %s" % (name, str(e)))
            log("(Progress saved. Re-run setup.bat to retry this step.)")
            return 1
    log("")
    log("All steps complete. Environment is ready.")
    log("Next: start the server with  .\\start.ps1")
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
        table[step_name]()
        mark_done(state, step_name, state_file)
    except ActionNeeded as e:
        save_state(state, state_file)
        log("ACTION NEEDED: %s; then re-run setup.bat" % str(e))
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
    args = parser.parse_args(argv)

    if args.status:
        return print_status()
    if args.reset:
        return reset_state()
    if args.only:
        return run_only(args.only)
    return run_all()


if __name__ == "__main__":
    sys.exit(main())
