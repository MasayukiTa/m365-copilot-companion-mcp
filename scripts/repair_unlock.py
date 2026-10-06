#!/usr/bin/env python3
"""Re-establish the unlock password for THIS machine without ever printing it.

Automatic startup captures this program's stdout into logs. Therefore stdout is STATUS ONLY:
    noop:<fixed note>
    repaired:<fixed note>
    failed:<fixed note>

Cleartext retrieval belongs to scripts/copilot_studio_values.ps1, which reads the local .env and
decrypts the DPAPI value only in the interactive PowerShell process that displays it.

Legacy --show/--current are refused deliberately; they used to put a secret on stdout.
"""
from __future__ import annotations

import os
import sys


def _scrub(text: str, env: dict, known_secrets=()) -> str:
    """Exception text with every .env VALUE removed, each replaced by its own key name.

    The line below prints the exception raised by repair_unlock_password(env_path, env), and
    `env` is the parsed .env -- it holds the unlock password. A Python exception routinely
    carries the offending value in its message, so that print can put the password on stdout
    even though nothing here asks it to. This stream is captured by scripts/start_all.ps1 and
    can reach logs, and the repository is public.

    Dropping the message would silence it and cost the only diagnosis this path emits, so
    remove the values instead: they are known exactly, right here. Short values are left alone
    because a two-character value matches everywhere and would redact the message into
    uselessness -- and a secret that short is not one.
    """
    out = str(text)
    # KNOWN SECRETS FIRST, AND WITH NO LENGTH FLOOR. The floor below is right for the blanket
    # sweep -- it is guessing which of .env's values are worth redacting, and a two-character
    # value matches everywhere -- and it is wrong for a value we have been told IS the secret.
    #
    # This argument exists because the sweep was scrubbing the wrong set entirely. `env` is the
    # .env as it was parsed, and repair_unlock_password's whole job is to mint a password that
    # is in no .env anybody parsed, so the one value most in need of removal was the one value
    # this function could never see. The mint site now redacts its own (tools/env_portability),
    # and this takes whatever comes back out, so neither half is trusted alone.
    for v in known_secrets:
        v = str(v or "")
        if v and v in out:
            out = out.replace(v, "<redacted:secret>")
    for key, val in sorted((env or {}).items(), key=lambda kv: -len(str(kv[1] or ""))):
        v = str(val or "")
        if len(v) >= 6 and v in out:
            out = out.replace(v, "<redacted:%s>" % key)
    return out


def _read_env(env_path: str) -> dict:
    """.env as python-dotenv reads it, or -- when dotenv is missing, as it is for a bare system
    Python -- as tools/env_portability.parse_env reads it (last occurrence wins, like dotenv)."""
    try:
        from dotenv import dotenv_values
        return dict(dotenv_values(env_path))
    except ImportError:
        from tools.env_portability import parse_env
        with open(env_path, "r", encoding="utf-8-sig") as fh:
            return dict(parse_env(fh.read()))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)

    if "--show" in argv or "--current" in argv:
        print("failed:cleartext output was removed; run scripts\\copilot_studio_values.bat")
        return 0

    env_path = argv[0] if argv else os.path.join(repo, ".env")
    try:
        from tools.env_portability import repair_unlock_password
    except Exception as exc:                       # noqa: BLE001
        print("failed:import %s" % type(exc).__name__)
        return 0

    try:
        env = _read_env(env_path)
        result = repair_unlock_password(env_path, env)
    except Exception as exc:                       # noqa: BLE001
        # Exception messages on this path may carry data from .env. Type only.
        print("failed:%s" % type(exc).__name__)
        return 0

    if not result.get("acted"):
        print("noop:unlock password unchanged")
        return 0

    print("repaired:a new unlock password was written to .env -- show it with "
          "copilot_studio_values.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
