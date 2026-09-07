#!/usr/bin/env python3
"""Can the unlock password be read by THIS Windows account? One line of output, for doctor.

Exists as a FILE because the alternative did not work. doctor passed this logic to python as a
`-c` payload through `Start-Process -ArgumentList @("-c", $code)`, which does not quote the
element: python received the first word and answered

    File "<string>", line 1
        import
    SyntaxError: invalid syntax

The check then read the non-zero exit as "could not ask" and reported PASS, so it could only
ever be green -- a health check that cannot fail. Both sides were tested (problems() in python,
the doctor line rendering green) and the seam between them never was, which is the only place it
was broken.

Output, on stdout, one line:
    ok                     -- a password is configured and this account can read it
    undecryptable          -- set, but written by another account or machine (the .env-carried case)
    unset                  -- nothing configured at all; mutating tools will be refused too
    error:<detail>         -- this script could not decide. NOT the same as a pass.

Exit code is 0 whenever a verdict was reached, including a bad one: the verdict is the answer,
and a non-zero exit would be indistinguishable from the failure above.
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

        from tools.env_portability import problems
        from tools.secret_store import (PROBLEM_UNDECRYPTABLE, unlock_password_from_env,
                                        unlock_password_problem)
    except Exception as exc:                       # noqa: BLE001 -- any import fault is a verdict
        print("error:import %s: %s" % (type(exc).__name__, exc))
        return 0

    try:
        env = dict(dotenv_values(env_path))
    except Exception as exc:                       # noqa: BLE001
        print("error:cannot read %s (%s)" % (env_path, type(exc).__name__))
        return 0

    try:
        # UNSET IS ALSO A FAULT, and problems() does not report it -- it is scoped to portability.
        # A machine with no unlock password at all refuses every mutating tool just as surely as
        # one that cannot decrypt the value it has, so both are reported here.
        value = unlock_password_from_env(env)
        if value:
            print("ok")
            return 0
        if unlock_password_problem() == PROBLEM_UNDECRYPTABLE:
            print("undecryptable")
            return 0
        if any(p.get("problem") == "undecryptable" for p in problems(env)):
            print("undecryptable")
            return 0
        print("unset")
        return 0
    except Exception as exc:                       # noqa: BLE001
        print("error:%s: %s" % (type(exc).__name__, exc))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
