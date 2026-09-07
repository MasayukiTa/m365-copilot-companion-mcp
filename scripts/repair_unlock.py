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
    repaired:<password>    a new password was established; this is it
    failed:<reason>        it needed doing and could not be done
"""
from __future__ import annotations

import os
import sys


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
        print("failed:%s: %s" % (type(exc).__name__, exc))
        return 0

    reason = str(result.get("reason") or "")
    if not result.get("acted"):
        print("noop:%s" % reason)
        return 0

    password = str(result.get("password") or "")
    if password:
        print("repaired:%s" % password)
    else:
        # Acted but produced no password to hand over: report it rather than let the operator
        # believe the value they brought with them still works.
        print("failed:repaired but the new password was not returned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
