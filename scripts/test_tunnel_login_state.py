# -*- coding: utf-8 -*-
"""The pure login/hop deciders that let the probe avoid the devtunnel CLI.

Login state is decided from the token FILE's size and mtime (measured 2026-11-11: 4182 bytes
valid, 0 bytes logged out), and the failing HOP is classified from the real public/local
handshake result plus the login state -- never from ``devtunnel user show`` / ``devtunnel
show``. These functions are pure (no I/O, no clock, no process calls), so they run in Linux
CI on any OS, exactly like decide_hang_restart / decide_child_strike.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tunnel_login_state as T  # noqa: E402


# ---- decide_login_state -------------------------------------------------------------------

def test_zero_byte_token_is_logged_out():
    # The measured logged-out shape: the token file exists but is 0 bytes.
    assert T.decide_login_state(0) == T.LOGIN_NONE


def test_missing_token_file_is_logged_out():
    # None == file does not exist == no login (same operational fact as 0 bytes).
    assert T.decide_login_state(None) == T.LOGIN_NONE


def test_nonempty_token_is_logged_in():
    # The measured valid shape: 4182 bytes.
    assert T.decide_login_state(4182) == T.LOGIN_VALID


def test_unreadable_size_is_unknown_not_a_login():
    # A size the caller could not turn into an int must never read as a valid login.
    assert T.decide_login_state("not-a-number") == T.LOGIN_UNKNOWN


def test_future_mtime_downgrades_a_nonempty_token_to_unknown():
    # A corrupt timestamp far in the future cannot be trusted on size alone.
    assert T.decide_login_state(4182, token_mtime=10_000, now=0, max_future_skew_s=300) \
        == T.LOGIN_UNKNOWN


def test_old_mtime_never_downgrades_a_nonempty_token():
    # An old but non-empty token is still a token; the public probe answers host liveness.
    assert T.decide_login_state(4182, token_mtime=0, now=10_000, max_future_skew_s=300) \
        == T.LOGIN_VALID


def test_no_now_skips_the_skew_check():
    assert T.decide_login_state(4182, token_mtime=10_000, now=None) == T.LOGIN_VALID


# ---- classify_public_hop ------------------------------------------------------------------

def test_public_404_while_logged_out_is_no_host_naming_the_login():
    hop, cause = T.classify_public_hop(404, 55.0, T.LOGIN_NONE)
    assert hop == T.HOP_PUBLIC_NO_HOST
    assert "not logged in" in cause


def test_public_404_while_logged_in_is_no_host_without_blaming_login():
    hop, cause = T.classify_public_hop(404, 2.0, T.LOGIN_VALID)
    assert hop == T.HOP_PUBLIC_NO_HOST
    assert "not logged in" not in cause


def test_public_401_is_auth():
    hop, _ = T.classify_public_hop(401, 1.0, T.LOGIN_VALID)
    assert hop == T.HOP_AUTH


def test_public_timeout_while_logged_out_is_no_host():
    hop, cause = T.classify_public_hop("ERR", 90.0, T.LOGIN_NONE)
    assert hop == T.HOP_PUBLIC_NO_HOST
    assert "not logged in" in cause


def test_public_timeout_while_logged_in_is_no_host_unreachable():
    hop, cause = T.classify_public_hop("ERR", 90.0, T.LOGIN_VALID)
    assert hop == T.HOP_PUBLIC_NO_HOST
    assert "unreachable" in cause


def test_public_slow_200_is_a_failure():
    hop, _ = T.classify_public_hop(200, 45.0, T.LOGIN_VALID, slow_s=30)
    assert hop == T.HOP_PUBLIC_SLOW


def test_public_fast_200_is_ok():
    hop, cause = T.classify_public_hop(200, 1.2, T.LOGIN_VALID, slow_s=30)
    assert hop == T.HOP_OK
    assert cause == ""


def test_public_500_is_other():
    hop, cause = T.classify_public_hop(500, 1.0, T.LOGIN_VALID)
    assert hop == T.HOP_PUBLIC_OTHER
    assert "500" in cause


# ---- classify_local_hop -------------------------------------------------------------------

def test_local_timeout_is_down():
    hop, _ = T.classify_local_hop("ERR", 90.0)
    assert hop == T.HOP_LOCAL_DOWN


def test_local_non200_is_down():
    hop, cause = T.classify_local_hop(503, 1.0)
    assert hop == T.HOP_LOCAL_DOWN
    assert "503" in cause


def test_local_401_is_auth():
    hop, _ = T.classify_local_hop(401, 1.0)
    assert hop == T.HOP_AUTH


def test_local_slow_200_is_a_failure():
    hop, _ = T.classify_local_hop(200, 45.0, slow_s=30)
    assert hop == T.HOP_LOCAL_SLOW


def test_local_fast_200_is_ok():
    hop, cause = T.classify_local_hop(200, 0.5, slow_s=30)
    assert hop == T.HOP_OK
    assert cause == ""
