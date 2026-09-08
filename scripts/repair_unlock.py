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

Output on stdout, one line, ASCII:
    noop:<reason>          nothing needed doing
    repaired:<note>        a new password was established and written to .env
    failed:<reason>        it needed doing and could not be done
"""
from __future__ import annotations

import os
import sys


def _scrub(text: str, env: dict) -> str:
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
    for key, val in sorted((env or {}).items(), key=lambda kv: -len(str(kv[1] or ""))):
        v = str(val or "")
        if len(v) >= 6 and v in out:
            out = out.replace(v, "<redacted:%s>" % key)
    return out


def main() -> int:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    env_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(repo, ".env")

    try:
        from dotenv import dotenv_values

        from tools.env_portability import repair_unlock_password
    except Exception as exc:                       # noqa: BLE001
        print("failed:import %s: %s" % (type(exc).__name__, exc))
        return 0

    try:
        env = dict(dotenv_values(env_path))
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
    reason = _scrub(str(result.get("reason") or ""), env)
    if not result.get("acted"):
        print("noop:%s" % reason)
        return 0

    # DO NOT PRINT THE VALUE. repair_unlock_password() has already written the new password to
    # .env in its protected form, so the cleartext does not need to travel back over stdout. This
    # stream is captured by scripts/start_all.ps1 and can reach logs, and the repository is
    # public. Emit only the non-secret fact that the repair happened, keeping the "repaired:"
    # prefix so start_all.ps1's ^(noop|repaired|failed|error): match still holds.
    if result.get("password"):
        print("repaired:the new unlock password was written to .env")
    else:
        # Acted but produced no password: the repair did not actually complete. Report it rather
        # than let the operator believe the value they brought with them still works.
        print("failed:repaired but the new password was not established")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
