"""The one temp directory this product (and the agents working on it) writes under.

Nothing used to sweep %TEMP%: job logs, pasted images, bench scratch and agents' venvs piled up
there until the disk was tight. Everything we own now goes under ONE directory,

    %TEMP%\\m365-companion\\

so a single age-based sweep (relay.fleet_retention.temp_home, run from fleet_retention.apply)
can cover it without ever having to guess which of the thousands of other entries in %TEMP%
belong to someone else.

Convention for agents and tools: put venvs, clones and test scratch under
    %TEMP%\\m365-companion\\agents\\<name>\\
(temp_dir("agents/<name>")) and they are covered by the same sweep once idle.

This module has no dependencies on the rest of the repo so any script may import it.
"""
from __future__ import annotations

import os
import re
import tempfile

#: Directory name under the system temp dir. One place; the sweep and the writers agree on it.
HOME_NAME = "m365-companion"


def system_temp() -> str:
    """The system temp directory. A function (not a constant) so tests can redirect it."""
    return tempfile.gettempdir()


def home_path() -> str:
    """Where the home is, WITHOUT creating it (the sweep must not create what it sweeps)."""
    return os.path.join(system_temp(), HOME_NAME)


def temp_home() -> str:
    """`%TEMP%\\m365-companion`, created on first use. Falls back to the plain system temp dir
    when it cannot be created, so a writer is never worse off than before this module existed."""
    path = home_path()
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return system_temp()


_BAD = re.compile(r"[^A-Za-z0-9._\-]")


def temp_dir(name: str) -> str:
    """A named subdirectory of the home, created on first use. `name` may be nested with `/`
    (`agents/fix-12`); every segment is reduced to safe characters and `..` is refused, so a
    caller cannot walk out of the home."""
    segs = []
    for seg in re.split(r"[\\/]+", str(name or "")):
        seg = _BAD.sub("_", seg).strip(".")
        if seg:
            segs.append(seg)
    if not segs:
        return temp_home()
    path = os.path.join(temp_home(), *segs)
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return temp_home()
