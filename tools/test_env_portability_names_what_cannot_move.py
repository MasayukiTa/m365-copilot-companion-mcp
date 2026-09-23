# -*- coding: utf-8 -*-
"""merge_for_new_machine, checked against its own docstring, and the CLI setup_devtunnel asks.

D7 in the 2026-09-24 new-PC review: merge_for_new_machine had no caller, and setup_devtunnel.ps1
kept its own idea of what a carried .env may keep (the URL went, the NAME stayed). It now asks
this module through `python tools/env_portability.py machine-bound <file>`, so the rules live in
one place -- which makes this module's rules the ones that have to be right.

Two faults found by reading the docstring against the code first:
  * a BLANK local value overwrote the value the first loop had just carried (and listed in
    `carried`), although the first loop itself treats blank as "not established";
  * MCP_TUNNEL_HOST -- the stamp naming the machine that minted MCP_TUNNEL_URL -- travelled while
    the URL beside it was dropped.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import childproc  # noqa: E402
from tools import env_portability as EP  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_a_blank_local_value_does_not_replace_the_carried_one():
    out = EP.merge_for_new_machine("A=1\n", "A=\n")
    assert "A=1" in out["lines"], out
    assert "A=" not in out["lines"]
    assert out["carried"] == ["A"]


def test_a_real_local_value_still_wins():
    out = EP.merge_for_new_machine("A=1\n", "A=2\n")
    assert "A=2" in out["lines"] and "A=1" not in out["lines"]
    assert out["kept_local"] == ["A"]


def test_a_local_only_blank_is_kept_as_it_is():
    """Nothing carried for it: the local line, blank or not, is the local line."""
    out = EP.merge_for_new_machine("", "B=\n")
    assert "B=" in out["lines"]


def test_the_host_stamp_does_not_travel_with_the_tunnel_it_names():
    assert EP.classify("MCP_TUNNEL_HOST") == "machine_bound"
    text = ("MCP_TUNNEL_NAME=x\nMCP_TUNNEL_URL=https://x.devtunnels.ms/\n"
            "MCP_TUNNEL_HOST=oldpc\nMCP_API_KEY=k\n")
    assert EP.machine_bound_keys_in(text) == ["MCP_TUNNEL_NAME", "MCP_TUNNEL_URL",
                                              "MCP_TUNNEL_HOST"]


def test_the_cli_prints_keys_never_values(tmp_path):
    p = tmp_path / ".env"
    p.write_text("MCP_UNLOCK_PASSWORD_PROTECTED=dpapi:SECRETBLOB\nMCP_TUNNEL_NAME=tun-secret\n"
                 "MCP_API_KEY=abc\n", encoding="utf-8")
    proc = childproc.run([sys.executable, os.path.join(HERE, "tools", "env_portability.py"),
                          "machine-bound", str(p)], timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.split()
    assert lines == ["dropped:MCP_UNLOCK_PASSWORD_PROTECTED", "dropped:MCP_TUNNEL_NAME", "done:2"]
    assert "SECRETBLOB" not in proc.stdout and "tun-secret" not in proc.stdout


def test_the_cli_fails_in_its_own_shape_on_a_missing_file(tmp_path):
    proc = childproc.run([sys.executable, os.path.join(HERE, "tools", "env_portability.py"),
                          "machine-bound", str(tmp_path / "nope.env")], timeout=60)
    assert proc.returncode == 2
    assert proc.stdout.startswith("error:")
