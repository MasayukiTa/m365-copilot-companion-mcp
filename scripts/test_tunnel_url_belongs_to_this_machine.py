# -*- coding: utf-8 -*-
"""A devtunnel URL inherited from another PC must not be treated as this machine's.

REPORTED FROM A REAL NEW-PC SETUP (2026-09-08). Install succeeded, the devtunnel step
errored as expected on an unconfigured machine -- and .env still held the PREVIOUS PC's
MCP_TUNNEL_URL, untouched. Setup had skipped provisioning because the value was non-empty,
so the operator pasted an address into Copilot Studio that pointed at a tunnel this machine
does not host, and nothing anywhere said so.

The premise in the code was "already set means provisioned on a previous run", which is only
true when the previous run was on the SAME machine. .env travels between machines; a devtunnel
URL does not, because it is reachable only while some machine hosts that tunnel. So the URL is
trusted only alongside a record of who minted it.
"""
import io

import pytest

import scripts.bootstrap as B


def _env(tmp_path, text):
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def at(tmp_path, monkeypatch):
    """Point bootstrap at a temporary ROOT so a real .env is never read or written."""
    monkeypatch.setattr(B, "ROOT", tmp_path)
    return tmp_path


def test_the_host_is_recorded_beside_a_minted_url(at, monkeypatch):
    monkeypatch.setattr(B, "_this_host", lambda: "pc-new")
    _env(at, "MCP_API_KEY=k\n")
    B._write_tunnel_to_env("my-tunnel", "https://abc-8000.devtunnels.ms")
    out = (at / ".env").read_text(encoding="utf-8")
    assert "MCP_TUNNEL_URL=https://abc-8000.devtunnels.ms" in out
    assert "MCP_TUNNEL_HOST=pc-new" in out
    assert "MCP_API_KEY=k" in out, "every other line must survive"


def test_a_stale_host_line_is_replaced_not_duplicated(at, monkeypatch):
    """Re-provisioning on a machine that already has a record must leave ONE record. Two
    MCP_TUNNEL_HOST lines and the reader takes the first, which would be the stale one."""
    monkeypatch.setattr(B, "_this_host", lambda: "pc-new")
    _env(at, "MCP_TUNNEL_HOST=pc-old\nMCP_TUNNEL_URL=https://old.devtunnels.ms\n")
    B._write_tunnel_to_env("t", "https://new.devtunnels.ms")
    lines = (at / ".env").read_text(encoding="utf-8").splitlines()
    assert sum(1 for l in lines if l.startswith("MCP_TUNNEL_HOST=")) == 1
    assert sum(1 for l in lines if l.startswith("MCP_TUNNEL_URL=")) == 1
    assert B._read_env_value("MCP_TUNNEL_HOST") == "pc-new"


def test_no_host_is_claimed_when_there_is_no_url(at, monkeypatch):
    """The host answers 'who minted this URL'. Stamping it with no URL present would claim
    provenance for a value that is not there -- and a URL arriving later would inherit the
    claim, which is the original bug wearing a new hat."""
    monkeypatch.setattr(B, "_this_host", lambda: "pc-new")
    _env(at, "MCP_API_KEY=k\n")
    B._write_tunnel_to_env("t", None)
    out = (at / ".env").read_text(encoding="utf-8")
    assert "MCP_TUNNEL_HOST=" not in out
    assert "MCP_TUNNEL_URL=" not in out


def test_an_existing_url_is_still_preserved_on_a_failed_mint(at, monkeypatch):
    """The older guarantee has to survive this change: a hosting hiccup (url=None) must not
    un-configure a working tunnel."""
    monkeypatch.setattr(B, "_this_host", lambda: "pc-new")
    _env(at, "MCP_TUNNEL_URL=https://kept.devtunnels.ms\n")
    B._write_tunnel_to_env("t", None)
    assert B._read_env_value("MCP_TUNNEL_URL") == "https://kept.devtunnels.ms"
    assert B._read_env_value("MCP_TUNNEL_HOST") == "pc-new", \
        "the machine keeping the URL is the one that must host it"


# ---- the decision the bug was in -----------------------------------------

def _trusted(at, monkeypatch, url, host, me):
    """Reproduce the short-circuit's condition from .env, as step_dev_tunnel evaluates it."""
    body = ""
    if url:
        body += "MCP_TUNNEL_URL=%s\n" % url
    if host:
        body += "MCP_TUNNEL_HOST=%s\n" % host
    _env(at, body)
    monkeypatch.setattr(B, "_this_host", lambda: me)
    return bool(B._read_env_value("MCP_TUNNEL_URL")
                and B._read_env_value("MCP_TUNNEL_HOST")
                and B._read_env_value("MCP_TUNNEL_HOST") == B._this_host())


def test_a_url_this_machine_minted_is_trusted(at, monkeypatch):
    assert _trusted(at, monkeypatch, "https://u", "pc-new", "pc-new") is True


def test_a_url_from_another_machine_is_not_trusted(at, monkeypatch):
    """THE REPORTED FAILURE. .env copied from the old PC: non-empty URL, different machine."""
    assert _trusted(at, monkeypatch, "https://u", "pc-old", "pc-new") is False


def test_a_url_with_no_recorded_host_is_not_trusted(at, monkeypatch):
    """Unknown provenance. Re-hosting the same tunnel name yields the same URL, so being
    wrong here costs ~30s, while trusting it costs a setup that looks finished and cannot
    connect. It also self-corrects: the re-host writes the host record."""
    assert _trusted(at, monkeypatch, "https://u", None, "pc-new") is False


def test_the_comparison_ignores_case(at, monkeypatch):
    """Windows reports the machine name in either case depending on how it is read, and a
    case flip must not read as a different machine."""
    _env(at, "MCP_TUNNEL_URL=https://u\nMCP_TUNNEL_HOST=pc-new\n")
    monkeypatch.setattr(B, "platform", type("P", (), {"node": staticmethod(lambda: "PC-New")}))
    assert B._this_host() == "pc-new"


def test_the_source_still_gates_on_the_host(at):
    """The decision lives inside step_dev_tunnel, which cannot be called here -- it shells out
    to devtunnel. Assert the gate is still spelled against the host, so a future edit that
    reverts to 'if existing_url:' does not pass silently."""
    import re
    src = io.open(B.__file__, encoding="utf-8").read()
    assert "recorded_host == _this_host()" in src
    # Anchored, because "if existing_url:" is a substring of the legitimate "elif
    # existing_url:" that reports an inherited URL -- an unanchored check fails on the fix.
    bad = re.search(r"^\s*if existing_url:\s*$", src, re.M)
    assert not bad, "the non-empty-only short-circuit is back: %s" % (bad and bad.group(0))
