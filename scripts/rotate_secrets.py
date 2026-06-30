#!/usr/bin/env python3
# =============================================================================
# m365-copilot-companion-mcp - SECRET ROTATION
#
# Emergency / routine rotation of the two secrets that live in .env:
#   - MCP_API_KEY        Bearer token. Validated by the MCP server's
#                        StaticTokenVerifier (main.py) AND used as the
#                        Copilot Studio connector header:
#                            Authorization: Bearer <MCP_API_KEY>
#                        Generated with secrets.token_hex(20) -> 40 hex chars.
#   - MCP_UNLOCK_PASSWORD  Per-IP unlock password for write/exec tools
#                        (tools/security.py unlock()/require_unlocked()).
#                        Generated with secrets.token_hex(8)  -> 16 hex chars.
#
# Use this the moment a .env is leaked: it re-issues the chosen secret(s),
# rewrites .env IN PLACE (preserving every other key/line/order), and prints
# the exact follow-up actions (update the connector, re-unlock, restart).
#
# This script is stdlib-only and makes NO network calls. It NEVER writes the
# secret values to any file or log -- it only writes them into .env (which
# already holds secrets) and optionally echoes them to the console so you can
# copy them.
#
# ASCII / ENGLISH ONLY (comments included) -- this repo's .bat/.ps1 mis-decode
# non-ASCII; the Python files match that rule for consistency.
#
# CLI:
#   python scripts/rotate_secrets.py            rotate BOTH secrets (default)
#   python scripts/rotate_secrets.py --api-key  rotate only MCP_API_KEY
#   python scripts/rotate_secrets.py --unlock   rotate only MCP_UNLOCK_PASSWORD
#   python scripts/rotate_secrets.py --no-print  do not echo new values to console
# =============================================================================
from __future__ import annotations

import argparse
import secrets
import shutil
import sys
from pathlib import Path

# Repo root = parent of this scripts/ dir, so the script behaves identically
# regardless of the caller's working directory.
ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_BAK_PATH = ROOT / ".env.bak"

API_KEY_VAR = "MCP_API_KEY"
UNLOCK_VAR = "MCP_UNLOCK_PASSWORD"


def gen_api_key() -> str:
    """40 hex chars, matching .env.example / bootstrap.py."""
    return secrets.token_hex(20)


def gen_unlock_password() -> str:
    """16 hex chars, matching .env.example / bootstrap.py."""
    return secrets.token_hex(8)


def read_env_lines(env_path: Path) -> list[str]:
    """Read .env as a list of lines (without line endings), tolerating a BOM.

    utf-8-sig strips a leading BOM if one is present: a .env saved by an older
    PowerShell `Set-Content -Encoding UTF8` starts with EF BB BF, which plain
    utf-8 would fold into the first key name. We always REWRITE without a BOM
    (see write_env_lines), so this also cleans up any pre-existing BOM.
    """
    text = env_path.read_text(encoding="utf-8-sig")
    # splitlines() drops the trailing newline; we re-add CRLFs on write.
    return text.splitlines()


def write_env_lines(env_path: Path, lines: list[str]) -> None:
    """Write .env as UTF-8 WITHOUT a BOM, CRLF line endings.

    This mirrors configure_env.ps1 (lines ~142-147) and scripts/bootstrap.py:
    PowerShell 5.1's `Set-Content -Encoding UTF8` PREPENDS a BOM, which corrupts
    the first key for plain parsers (bootstrap.py read the first key as
    "﻿MCP_API_KEY" and reported it missing; python-dotenv left it unset and
    main.py crashed with KeyError). We therefore write no BOM, CRLF endings.
    """
    text = "\r\n".join(lines) + "\r\n"
    # encoding="utf-8" (NOT "utf-8-sig") => no BOM. newline="" => do not let
    # Python translate our explicit \r\n into \r\r\n on Windows.
    with open(env_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def rotate_in_lines(lines: list[str], updates: dict[str, str]) -> tuple[list[str], set[str]]:
    """Replace `KEY=...` lines for keys in `updates`, preserving all other
    lines and their order. Only matches non-comment assignment lines whose key
    (ignoring leading whitespace) equals one of the target keys.

    Returns (new_lines, keys_actually_found).
    """
    found: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        replaced = False
        # Never touch commented-out lines.
        if not stripped.startswith("#"):
            for key, new_val in updates.items():
                # Match "KEY=" possibly with surrounding spaces around the key.
                head = stripped.split("=", 1)[0].rstrip() if "=" in stripped else ""
                if head == key:
                    out.append(f"{key}={new_val}")
                    found.add(key)
                    replaced = True
                    break
        if not replaced:
            out.append(line)
    return out, found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rotate the m365-copilot-companion-mcp secrets in .env "
            "(MCP_API_KEY and/or MCP_UNLOCK_PASSWORD). Default: rotate BOTH."
        ),
    )
    parser.add_argument(
        "--api-key", action="store_true",
        help="Rotate only MCP_API_KEY (the Bearer token).",
    )
    parser.add_argument(
        "--unlock", action="store_true",
        help="Rotate only MCP_UNLOCK_PASSWORD (the per-IP unlock password).",
    )
    parser.add_argument(
        "--no-print", action="store_true",
        help="Do not echo the new secret value(s) to the console.",
    )
    args = parser.parse_args(argv)

    # Default = rotate both when neither flag is given.
    rotate_api = args.api_key or not (args.api_key or args.unlock)
    rotate_unlock = args.unlock or not (args.api_key or args.unlock)

    if not ENV_PATH.exists():
        print(f"ERROR: .env not found at {ENV_PATH}", file=sys.stderr)
        print("Run setup first (setup.bat) so a .env exists before rotating.", file=sys.stderr)
        return 1

    # 1. Back up the current .env first (overwrite-safe: always replaces .env.bak).
    shutil.copy2(ENV_PATH, ENV_BAK_PATH)
    print(f"Backed up current .env -> {ENV_BAK_PATH.name}")

    # 2. Generate the chosen new secret(s).
    new_api_key = gen_api_key() if rotate_api else None
    new_unlock = gen_unlock_password() if rotate_unlock else None

    updates: dict[str, str] = {}
    if new_api_key is not None:
        updates[API_KEY_VAR] = new_api_key
    if new_unlock is not None:
        updates[UNLOCK_VAR] = new_unlock

    # 3. Update .env IN PLACE, preserving all other keys/lines/order.
    lines = read_env_lines(ENV_PATH)
    new_lines, found = rotate_in_lines(lines, updates)

    # If a target key was not present at all, append it so rotation still works
    # (an .env that somehow lost the key still ends up valid).
    for key, val in updates.items():
        if key not in found:
            new_lines.append(f"{key}={val}")
            print(f"NOTE: {key} was not present in .env; appended it.")

    write_env_lines(ENV_PATH, new_lines)
    print(f"Rewrote {ENV_PATH.name} (UTF-8 no BOM, CRLF), preserving all other keys.")
    print("")

    # 4. Print clear, actionable next steps. NEVER write secrets to a log file.
    print("=" * 70)
    print("ROTATION COMPLETE -- DO THESE NEXT:")
    print("=" * 70)

    step = 1
    if rotate_api:
        print(f"{step}. Update the Copilot Studio MCP connector connection:")
        if not args.no_print:
            print(f"     Authorization header value -> `Bearer {new_api_key}`")
        else:
            print("     Authorization header value -> `Bearer <NEW_API_KEY>` "
                  "(read it from .env)")
        print("   The OLD Bearer token no longer authenticates once the server restarts.")
        step += 1

    if rotate_unlock:
        print(f"{step}. All existing per-IP unlocks are now invalid; agents must call")
        print("     unlock(<new password>) again to use write/exec tools.")
        if not args.no_print:
            print(f"     New unlock password: {new_unlock}")
        else:
            print("     (read the new unlock password from .env)")
        step += 1

    # 5. Restart instruction. Determined from main.py + supervisor.ps1:
    #    - MCP_API_KEY is read ONCE at import (main.py: API_KEY = os.environ["MCP_API_KEY"]
    #      baked into StaticTokenVerifier) -> a full PROCESS restart is required.
    #    - MCP_UNLOCK_PASSWORD is read per-request in tools/security.py unlock(), so it
    #      would pick up a new value without a restart -- but a restart is harmless and
    #      keeps both consistent, so we always tell the user to restart.
    #    The server is NOT a Windows Service: it runs as `python main.py`, launched and
    #    kept alive by supervisor.ps1 (which auto-relaunches a killed/stale instance).
    print(f"{step}. Restart the server so it loads the new value(s).")
    print("   The MCP_API_KEY is read once at startup (main.py bakes it into the JWT")
    print("   verifier), so a RESTART of the python process is required -- editing .env")
    print("   alone does NOT take effect.")
    print("   This server runs as `python main.py` under supervisor.ps1 (NOT a Windows")
    print("   Service). To restart, either:")
    print("     a) Stop the running main.py and let the supervisor relaunch it:")
    print("          Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |")
    print("            Where-Object { $_.CommandLine -like '*main.py*' } |")
    print("            ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    print("        (supervisor.ps1 detects the dead instance and starts a fresh one), OR")
    print("     b) Re-run  .\\start_all.ps1  from the repo root.")

    print("=" * 70)
    print("If anything looks wrong, the previous .env is saved as .env.bak")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
