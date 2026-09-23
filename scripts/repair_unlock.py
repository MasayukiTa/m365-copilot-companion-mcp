#!/usr/bin/env python3
"""Re-establish the unlock password for THIS machine, and say what it now is.

repair_unlock_password() generates a NEW random password when the one in .env cannot be
decrypted here -- which is the right thing to do, because the old one is unreadable on this
account -- but it returned only a reason string and a backup path. So the operator, who came
with a password written down from the machine that produced the .env, ends up with a machine
where that password no longer works and nothing ever told them the new one. Everything looks
green; unlock() just refuses.

Automatic unlock inside the fleet and the bridge keeps working either way (they decrypt the
value locally), so this only matters to a person typing unlock(password) by hand -- which is
exactly what someone setting up a new machine does.

Written as a file rather than a `-c` payload for the reason the unlock CHECK had to be: an
argument that is a program is one long string, and every layer between here and python is
another chance for it to be split or de-escaped.

Usage:
    repair_unlock.py [--show] [ENV_PATH]     repair if needed (what start_all.ps1 runs)
    repair_unlock.py --current [ENV_PATH]    read-only: the password this account can open

Output on stdout, ASCII, one verdict line:
    noop:<reason>          nothing needed doing
    repaired:<note>        a new password was established and written to .env
    failed:<reason>        it needed doing and could not be done
followed, ONLY when the new password is revealed (see _reveal_allowed), by
    password:<value>

--current prints exactly one of
    password:<value>       what unlock(password) accepts on this machine
    unset:<reason>         there is no unlock password in .env yet
    undecryptable:<reason> it is there, but this Windows account cannot open it
    failed:<reason>        it could not be read at all
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


def _reveal_allowed(show_flag: bool, stream=None) -> bool:
    """May the cleartext password be written to stdout on THIS run?

    TWO READERS SHARE THIS STREAM, AND ONLY ONE OF THEM MAY SEE THE VALUE. scripts/start_all.ps1
    runs this with stdout captured (2>&1 into a variable, and from there into console logs of a
    hidden logon start), which is why the value was taken off this stream after CodeQL alert
    #32. But taking it off everywhere left the new password readable by NOBODY: the repair
    mints it, protects it, and the operator who needs it for unlock(password) was pointed at
    scripts/copilot_studio_values.ps1, which did not print it either (D1 in the 2026-09-24
    new-PC review). So the value goes out only to a reader who is a person: a console (stdout
    is a TTY -- a human ran this by hand), or an explicit --show from a caller that displays it
    and does not log it (copilot_studio_values.ps1). start_all passes neither and pipes stdout,
    so its capture never holds the value; it points the operator at copilot_studio_values.bat,
    which can now show it.
    """
    if show_flag:
        return True
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except Exception:                              # noqa: BLE001
        return False


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


def _show_current(env_path: str) -> int:
    """Read-only: print the unlock password this Windows account can decrypt from .env.

    THE SAME FUNCTION THE SERVER USES. tools.secret_store.unlock_password_from_env is what
    main.py's unlock gate calls: plain MCP_UNLOCK_PASSWORD first, else DPAPI-unprotect
    MCP_UNLOCK_PASSWORD_PROTECTED for the current user. Showing anything else (a second DPAPI
    call in PowerShell, say) could show a value the gate would not accept.
    """
    env = {}
    try:
        env = _read_env(env_path)
        from tools.secret_store import (PROBLEM_UNDECRYPTABLE, unlock_password_from_env,
                                        unlock_password_problem)
        value = unlock_password_from_env(env)
        problem = unlock_password_problem()
    except Exception as exc:                       # noqa: BLE001
        print("failed:%s: %s" % (type(exc).__name__, _scrub(exc, env)))
        return 0
    if value:
        print("password:%s" % value)
    elif problem == PROBLEM_UNDECRYPTABLE:
        print("undecryptable:this Windows account cannot decrypt the value in .env "
              "(it was made by another account or on another PC)")
    else:
        print("unset:there is no unlock password in .env yet")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    show = "--show" in argv
    current = "--current" in argv
    rest = [a for a in argv if a not in ("--show", "--current")]
    env_path = rest[0] if rest else os.path.join(repo, ".env")

    if current:
        return _show_current(env_path)

    try:
        from tools.env_portability import repair_unlock_password
    except Exception as exc:                       # noqa: BLE001
        print("failed:import %s: %s" % (type(exc).__name__, exc))
        return 0

    env = {}
    try:
        env = _read_env(env_path)
        result = repair_unlock_password(env_path, env)
    except Exception as exc:                       # noqa: BLE001
        print("failed:%s: %s" % (type(exc).__name__, _scrub(exc, env)))
        return 0

    # SCRUB EVERYTHING THAT COMES BACK OUT, not just the field that obviously holds a secret.
    # `result` is what repair_unlock_password(env_path, env) returned, and `env` carried the
    # password in, so every field of it is downstream of the password -- a reason string that
    # quoted the offending value would put it on this stream just as surely as printing
    # `password` did. Scrubbing at the boundary means the later prints do not each have to
    # remember; the cost is one pass over a short string.
    reason = _scrub(str(result.get("reason") or ""), env,
                    known_secrets=(result.get("password"),))
    if not result.get("acted"):
        print("noop:%s" % reason)
        return 0

    new_password = result.get("password")
    if not new_password:
        # Acted but produced no password: the repair did not actually complete. Report it rather
        # than let the operator believe the value they brought with them still works.
        print("failed:repaired but the new password was not established")
        return 0

    # THE VERDICT LINE NEVER CARRIES THE VALUE; it is what start_all.ps1 selects and prints,
    # keeping the "repaired:" prefix its ^(noop|repaired|failed|error): match relies on.
    if _reveal_allowed(show):
        print("repaired:a new unlock password was established for this machine and written "
              "to .env (it is on the next line)")
        print("password:%s" % new_password)
    else:
        print("repaired:a new unlock password was written to .env -- show it with "
              "copilot_studio_values.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
