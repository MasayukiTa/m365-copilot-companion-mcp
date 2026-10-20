# -*- coding: utf-8 -*-
"""An ABSENT MCP_ALLOWED_BASE leaves the file tools on every drive (owner decision 2026-10-20).

From 2026-09-24 (D6) it meant the home directory only; the owner reversed that: the default is
the whole PC and narrowing is the opt-out (name a folder). A value naming no usable root still
stays narrow (home), because that is a mistake in a restriction the person asked for.

ALLOWED_BASES is computed at import, so each case runs in a fresh interpreter with the
environment under test, and resolves a path OUTSIDE the home directory through the real
_validate_path. Cross-platform: the home directory and an outside path are chosen per OS.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import childproc  # noqa: E402

PROBE = r"""
import json, os, sys
sys.path.insert(0, %r)
from tools import file_ops as f
out = {"bases": None if f.ALLOWED_BASES is None else [str(b) for b in f.ALLOWED_BASES],
       "base": str(f.ALLOWED_BASE)}
for name, p in (("outside", %r), ("inside", %r)):
    try:
        f._validate_path(p)
        out[name] = "allowed"
    except PermissionError:
        out[name] = "refused"
print(json.dumps(out))
"""


def _run(tmp_path, value):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    outside = str(tmp_path / "elsewhere" / "x.txt")      # tmp_path itself is not under `home`
    inside = str(home / "Documents" / "x.txt")
    env = dict(os.environ, USERPROFILE=str(home), HOME=str(home))
    env.pop("MCP_ALLOWED_BASE", None)
    if value is not None:
        env["MCP_ALLOWED_BASE"] = value
    r = childproc.run([sys.executable, "-c", PROBE % (str(REPO), outside, inside)],
                      env=env, cwd=str(REPO), timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1]), home


def test_absent_means_every_drive(tmp_path):
    out, home = _run(tmp_path, None)
    assert out["bases"] is None
    assert out["outside"] == "allowed" and out["inside"] == "allowed"
    assert out["base"] == str(home.resolve()), "state dirs must still live under home"


def test_empty_means_every_drive(tmp_path):
    out, _ = _run(tmp_path, "")
    assert out["bases"] is None and out["outside"] == "allowed"


def test_star_still_means_every_drive(tmp_path):
    out, _ = _run(tmp_path, "*")
    assert out["bases"] is None and out["outside"] == "allowed"


def test_naming_the_home_directory_is_the_opt_out(tmp_path):
    out, home = _run(tmp_path, str(tmp_path / "home"))
    assert out["bases"] == [str(home.resolve())]
    assert out["outside"] == "refused" and out["inside"] == "allowed"


def test_a_value_naming_no_usable_root_fails_closed(tmp_path):
    out, home = _run(tmp_path, os.pathsep)
    assert out["bases"] == [str(home.resolve())] and out["outside"] == "refused"


def test_an_explicit_list_is_unchanged(tmp_path):
    target = tmp_path / "elsewhere"
    out, _ = _run(tmp_path, str(target))
    assert out["bases"] == [str(target.resolve())] and out["outside"] == "allowed"
    assert out["inside"] == "refused"
