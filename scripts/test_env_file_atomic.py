# -*- coding: utf-8 -*-
"""scripts/env_file.py -- the atomic .env writer quickstart.bat and bootstrap.py now use (D28).

The property that matters is all-or-nothing: a write that is interrupted must leave the OLD
file intact (never a truncated one, which made the next setup run mint a new Bearer token
silently), and must leave no temporary file behind. Driven by making the final rename fail.
Cross-platform; the CLI half runs the script as quickstart.bat does.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import env_file as E  # noqa: E402
from tools import childproc  # noqa: E402


def test_an_interrupted_write_leaves_the_old_file_whole(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"MCP_API_KEY=old\r\nOTHER=1\r\n")
    with mock.patch.object(E.os, "replace", side_effect=OSError("power cut")):
        with pytest.raises(OSError):
            E.atomic_write_text(env, "MCP_API_KEY=new\r\n")
    assert env.read_bytes() == b"MCP_API_KEY=old\r\nOTHER=1\r\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"], "a temporary file was left behind"


def test_a_completed_write_is_a_swap_not_a_rewrite(tmp_path):
    """A new file identity is the observable difference between "temp file renamed over it"
    and "opened, truncated, rewritten" (which is what every writer did before)."""
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    before = os.stat(env).st_ino
    E.atomic_write_text(env, "A=2\n")
    assert os.stat(env).st_ino != before
    assert env.read_text(encoding="utf-8") == "A=2\n"


def test_a_briefly_locked_file_is_retried_not_failed(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("sharing violation")
        return real(a, b)

    with mock.patch.object(E.os, "replace", side_effect=flaky):
        E.atomic_write_text(env, "A=2\n")
    assert env.read_text(encoding="utf-8") == "A=2\n" and calls["n"] == 3


def test_writes_utf8_without_a_bom(tmp_path):
    env = tmp_path / ".env"
    E.atomic_write_text(env, "# — 日本語\nA=1\n")
    raw = env.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8") == "# — 日本語\nA=1\n"


def test_set_replaces_the_first_and_drops_duplicates_keeping_crlf(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfA=1\r\nMCP_TUNNEL_ALLOW_ANONYMOUS=0\r\nB=2\r\nMCP_TUNNEL_ALLOW_ANONYMOUS=1\r\n")
    E.set_key(env, "MCP_TUNNEL_ALLOW_ANONYMOUS", "1")
    assert env.read_bytes() == b"A=1\r\nMCP_TUNNEL_ALLOW_ANONYMOUS=1\r\nB=2\r\n"


def test_unset_removes_every_active_line_and_leaves_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"# MCP_TUNNEL_ALLOW_ANONYMOUS=0\nMCP_TUNNEL_ALLOW_ANONYMOUS=1\nX=1\n"
                    b"  MCP_TUNNEL_ALLOW_ANONYMOUS = 1\n")
    assert E.unset_key(env, "MCP_TUNNEL_ALLOW_ANONYMOUS") is True
    assert env.read_bytes() == b"# MCP_TUNNEL_ALLOW_ANONYMOUS=0\nX=1\n"
    before = env.stat().st_mtime_ns
    assert E.unset_key(env, "MCP_TUNNEL_ALLOW_ANONYMOUS") is False
    assert env.stat().st_mtime_ns == before, "an absent key must not rewrite the file"


def test_the_cli_as_quickstart_calls_it(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MCP_API_KEY=keep\nMCP_TUNNEL_ALLOW_ANONYMOUS=1\n", encoding="utf-8")
    script = str(HERE / "env_file.py")
    r = childproc.run([sys.executable, script, "unset", "MCP_TUNNEL_ALLOW_ANONYMOUS", "--env", str(env)])
    assert r.returncode == 0 and r.stdout.strip() == "removed"
    r = childproc.run([sys.executable, script, "unset", "MCP_TUNNEL_ALLOW_ANONYMOUS", "--env", str(env)])
    assert r.stdout.strip() == "absent"
    url = "https://m365.cloud.microsoft/chat/agent/T_x.y?a=1&b='q'"
    r = childproc.run([sys.executable, script, "set", "MCP_IMPL_AGENT_URL", url, "--env", str(env)])
    assert r.returncode == 0, r.stderr
    assert env.read_text(encoding="utf-8") == "MCP_API_KEY=keep\nMCP_IMPL_AGENT_URL=" + url + "\n"
    r = childproc.run([sys.executable, script, "set", "BAD KEY", "x", "--env", str(env)])
    assert r.returncode == 2


def test_key_classification():
    text = "A=1\n# B=2\n#C=3\n  D = 4\nnot a line\n"
    assert E.active_keys(text) == {"A", "D"}
    assert E.commented_keys(text) == {"B", "C"}
