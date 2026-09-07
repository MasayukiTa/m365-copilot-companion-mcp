# -*- coding: utf-8 -*-
"""Which .env values survive a move to another machine, and which cannot.

WHY THIS EXISTS. A new PC was set up by copying the repository, .env included, and the stack
looked healthy: the server started, /health answered 200, doctor showed green. The first
mutating tool then failed, and the message said MCP_UNLOCK_PASSWORD was not configured -- while
MCP_UNLOCK_PASSWORD_PROTECTED sat in .env holding a DPAPI blob written by a DIFFERENT Windows
account, which this one cannot open. Three days of "the server will not start" went into a
server that was starting perfectly.

THE POINT IS THE CLASSIFICATION, NOT THE COPY. Most of .env is tenant- or account-level and
moves fine. A few values are bound to one machine or one Windows user, and those are exactly
the ones that fail silently later rather than loudly at the door. Sorting them is what makes an
install distributable: the machine can decide what to carry and what to rebuild, and nobody has
to know which is which.

NO HUMAN TYPES A COMMAND. This module only classifies and merges; start_all.ps1 already owns the
pattern for the values a person genuinely has to supply -- it detects the gap and pops the
configure_env dialog. What this adds is the ability to detect the gap correctly in the first
place, including the case where a value is PRESENT but unusable.
"""
from __future__ import annotations

import os

#: Values bound to one machine or one Windows user. Copying them produces a file that looks
#: configured and is not, so they are dropped on transfer and rebuilt on the new host.
#:
#: Each entry says WHY it cannot travel, because a list of names invites a future reader to add
#: one on a hunch, and a wrong entry here silently discards a working setting.
MACHINE_BOUND = {
    # DPAPI, CryptProtectData with no LOCAL_MACHINE flag -- see tools/secret_store.protect_secret.
    # Decryptable only by the Windows account that wrote it, on the machine that wrote it.
    "MCP_UNLOCK_PASSWORD_PROTECTED":
        "DPAPI value bound to the Windows account that created it",
    # A dev tunnel belongs to the account that hosts it and cannot be renamed
    # (scripts/setup_devtunnel.ps1). Two machines sharing one name fight over the host.
    "MCP_TUNNEL_NAME":
        "the dev tunnel is owned by the account that hosts it",
    "MCP_TUNNEL_URL":
        "derived from the tunnel this machine hosts",
    # Points at a host reachable from the machine it was configured on.
    "SWE_EVAL_HOST":
        "names a host resolved from the original machine",
}

#: Present in a working install and easy to leave out of a hand-made one. Absence is not an
#: error -- each has a defined default -- but it changes behaviour in ways that read as faults
#: elsewhere, so a transfer should carry them rather than let them quietly vanish.
BEHAVIOURAL_DEFAULTS = {
    # Without this a goal that arrives with no fleet running parks as awaiting_fleet forever.
    # Eleven goals were lost that way before the flag reached its branch.
    "FLEET_INTAKE_AUTOSTART": "1",
    # Without this the unlock token is not required, which is a weaker gate, not a broken one.
    "MCP_REQUIRE_UNLOCK_TOKEN": "1",
}


def classify(key: str) -> str:
    """"machine_bound" if `key` cannot travel, else "portable"."""
    return "machine_bound" if key in MACHINE_BOUND else "portable"


def parse_env(text: str) -> "list[tuple[str, str]]":
    """[(key, value)] in file order. Comments and blanks are dropped, not preserved.

    Deliberately not a dict: a real .env can repeat a key, and the LAST one is what dotenv
    applies. Collapsing to a dict here would silently pick the first and change behaviour.
    """
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out.append((k.strip(), v.strip()))
    return out


def merge_for_new_machine(old_text: str, current_text: str = "") -> dict:
    """Build the .env for THIS machine from one carried over from another.

    `old_text` is the .env being transferred; `current_text` is whatever this machine already
    has. Anything the new machine has ALREADY established wins -- it was made here, so it is
    valid here, and overwriting it with a foreign value is the whole failure being prevented.

    Returns {"lines", "carried", "dropped", "kept_local", "added_defaults"}.
    """
    local = dict(parse_env(current_text))
    carried, dropped, kept_local, added = [], [], [], []

    merged = {}
    for key, value in parse_env(old_text):
        if classify(key) == "machine_bound":
            dropped.append((key, MACHINE_BOUND[key]))
            continue
        if key in local and local[key]:
            kept_local.append(key)
            continue
        merged[key] = value
        carried.append(key)

    # Whatever this machine already established stays, including its own machine-bound values.
    for key, value in parse_env(current_text):
        merged[key] = value

    for key, value in BEHAVIOURAL_DEFAULTS.items():
        if key not in merged:
            merged[key] = value
            added.append(key)

    lines = ["%s=%s" % (k, v) for k, v in merged.items()]
    return {"lines": lines, "carried": carried, "dropped": dropped,
            "kept_local": kept_local, "added_defaults": added}


def problems(environ=None) -> list:
    """Configuration faults that a health check would otherwise miss. [] when there are none.

    THE ONES THAT LOOK FINE. A missing value is caught at the door by start_all; these are the
    ones that are PRESENT and unusable, which nothing was looking for.
    """
    env = dict(os.environ if environ is None else environ)
    found = []
    try:
        from tools.secret_store import (PROBLEM_UNDECRYPTABLE, UNLOCK_PASSWORD_PROTECTED_VAR,
                                        unlock_password_from_env, unlock_password_problem)
        unlock_password_from_env(env)
        if unlock_password_problem() == PROBLEM_UNDECRYPTABLE:
            found.append({
                "key": UNLOCK_PASSWORD_PROTECTED_VAR,
                "problem": PROBLEM_UNDECRYPTABLE,
                "detail": ("set, but this Windows account cannot decrypt it -- the value was "
                           "created by another account or on another machine"),
                "remedy": "re-protect the unlock password on this machine",
            })
    except Exception:
        pass
    return found
