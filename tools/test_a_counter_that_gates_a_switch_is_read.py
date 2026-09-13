# -*- coding: utf-8 -*-
"""A counter kept for 26 days to decide a security switch, that nothing ever read.

`tools/lock_state.py::record_token_gap` counts every call that passed the unlock gate on the
strength of the identity alone. Its docstring says exactly what the count is for:

    MCP_REQUIRE_UNLOCK_TOKEN defaults to off: turning it on before anyone has re-unlocked would
    refuse every existing session at once ... This counter is what says when it is safe -- when
    it stops growing, every live caller is presenting a token and the switch costs nothing.

`token_gap()`, the reader, had no caller. MEASURED 2026-09-13 on the live file:

    count 154   first 2026-08-18 06:14   last 2026-09-13 11:05 (that morning)
    ips   one address: 146, + five others (the address itself is in the live file, not here)

So the answer the counter had been holding since August was "no, and emphatically" -- one caller
still making 95% of the token-less calls -- and there was no way to ask it.

TWO READERS, DELIBERATELY. A subcommand answers when asked; the startup line answers without
being asked. The 26 days of silence were not caused by a missing formatter.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import lock_state as LS  # noqa: E402

DAY = 86400.0


def _gap(ip="203.0.113.9", ago=0.0):
    LS.record_token_gap(ip, ts=time.time() - ago)


# ── the report ────────────────────────────────────────────────────────────────────────────

def test_an_empty_counter_says_the_switch_is_free():
    """No call has ever needed the identity-only path, so enforcement costs nothing."""
    r = LS.token_gap_report()
    assert r["count"] == 0
    assert r["safe_to_enforce"] is True
    assert r["last_ts"] is None


def test_a_call_this_morning_says_it_is_not():
    """THE LIVE CASE. 154 records, the newest hours old -- enforcement would refuse them."""
    _gap("203.0.113.9")
    r = LS.token_gap_report()
    assert r["count"] == 1
    assert r["safe_to_enforce"] is False
    assert r["ips"]["203.0.113.9"] == 1


def test_the_count_and_the_callers_are_both_reported():
    """The count alone cannot tell one live integration from a hundred stragglers, and that
    difference is the entire decision."""
    for _ in range(3):
        _gap("203.0.113.9")
    _gap("203.0.113.4")
    r = LS.token_gap_report()
    assert r["count"] == 4
    assert r["ips"] == {"203.0.113.9": 3, "203.0.113.4": 1}


def test_a_gap_older_than_a_full_grant_ttl_releases_the_switch(monkeypatch):
    """The quiet window is DERIVED from MCP_UNLOCK_TTL_DAYS, not chosen: once a full grant
    lifetime has passed with no identity-only call, every grant still in the table was
    established after the last gap."""
    monkeypatch.setenv("MCP_UNLOCK_TTL_DAYS", "30")
    _gap(ago=31 * DAY)
    r = LS.token_gap_report()
    assert r["count"] == 1, "the record itself is still there"
    assert r["safe_to_enforce"] is True
    assert r["quiet_required_seconds"] == 30 * DAY


def test_the_window_follows_the_ttl_it_is_derived_from(monkeypatch):
    """A shorter TTL means a shorter wait. If these ever stop moving together, the derivation
    has been replaced by a number somebody picked."""
    monkeypatch.setenv("MCP_UNLOCK_TTL_DAYS", "30")
    _gap(ago=10 * DAY)
    assert LS.token_gap_report()["safe_to_enforce"] is False
    monkeypatch.setenv("MCP_UNLOCK_TTL_DAYS", "7")
    assert LS.token_gap_report()["safe_to_enforce"] is True


def test_the_report_says_whether_enforcement_is_already_on(monkeypatch):
    monkeypatch.setenv("MCP_REQUIRE_UNLOCK_TOKEN", "1")
    assert LS.token_gap_report()["enforcing"] is True
    monkeypatch.delenv("MCP_REQUIRE_UNLOCK_TOKEN", raising=False)
    assert LS.token_gap_report()["enforcing"] is False


# ── the reader that runs without being asked ──────────────────────────────────────────────

def test_the_startup_line_names_the_callers_that_would_break(monkeypatch):
    monkeypatch.delenv("MCP_REQUIRE_UNLOCK_TOKEN", raising=False)
    for _ in range(5):
        _gap("203.0.113.9")
    _gap("203.0.113.4")
    line = LS.token_gap_warning()
    assert "203.0.113.9 x5" in line, line
    assert "MCP_REQUIRE_UNLOCK_TOKEN is OFF" in line


def test_silence_is_the_good_case(monkeypatch):
    """Nothing to say when the switch is already on, and nothing to say when it is free. A line
    that prints on every boot regardless is a line that stops being read."""
    monkeypatch.delenv("MCP_REQUIRE_UNLOCK_TOKEN", raising=False)
    assert LS.token_gap_warning() == "", "empty counter"

    monkeypatch.setenv("MCP_UNLOCK_TTL_DAYS", "30")
    _gap(ago=40 * DAY)
    assert LS.token_gap_warning() == "", "quiet for longer than a grant lifetime"

    _gap()
    assert LS.token_gap_warning() != "", "a fresh gap has something to say"
    monkeypatch.setenv("MCP_REQUIRE_UNLOCK_TOKEN", "1")
    assert LS.token_gap_warning() == "", "already enforcing"


def test_the_server_prints_it_at_startup():
    """THE POINT OF THE WHOLE CHANGE. A subcommand nobody runs is what the last 26 days were.

    Asserted against main.py's source because the alternative is booting the server: the entry
    block is not importable on its own. Scoped to the __main__ block so a stray mention
    elsewhere cannot satisfy it."""
    src = io.open(os.path.join(REPO, "main.py"), encoding="utf-8").read()
    i = src.index('if __name__ == "__main__":')
    assert "token_gap_warning" in src[i:], "起動時に読む経路が消えている"


# ── the surface the operator already uses ─────────────────────────────────────────────────

def test_the_admin_cli_answers_the_question():
    """`python -m tools.lock_state` is what the cockpit's 詳細設定 panel shells out to, so it is
    where the operator deciding the switch already is."""
    env = dict(os.environ)
    env.pop("MCP_REQUIRE_UNLOCK_TOKEN", None)
    r = subprocess.run([sys.executable, "-m", "tools.lock_state", "token-gap"],
                       cwd=REPO, capture_output=True, text=True, errors="replace",
                       timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert set(out) >= {"count", "ips", "safe_to_enforce", "enforcing"}, sorted(out)


def test_the_existing_subcommands_still_work():
    """`show` is what the cockpit shells out to. The new subcommand must not have displaced
    it, and must not have displaced its being the default either."""
    r = subprocess.run([sys.executable, "-m", "tools.lock_state", "show"],
                       cwd=REPO, capture_output=True, text=True, errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr
    assert isinstance(json.loads(r.stdout), dict)



def test_show_is_still_what_a_bare_call_does():
    """The panel calls it with the subcommand, but a bare call used to print the refusal and
    a new default would change what an operator at a prompt sees."""
    r = subprocess.run([sys.executable, "-m", "tools.lock_state"],
                       cwd=REPO, capture_output=True, text=True, errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr
    assert isinstance(json.loads(r.stdout), dict)


def test_an_unknown_subcommand_still_refuses():
    r = subprocess.run([sys.executable, "-m", "tools.lock_state", "nope"],
                       cwd=REPO, capture_output=True, text=True, errors="replace", timeout=60)
    assert r.returncode == 2, r.stdout
    assert "usage" in r.stdout
