# -*- coding: utf-8 -*-
"""A stalled card must say WHY it is not moving: queue rank, workers ahead, or a resource wait.

## The requirement (owner, 2026-10-27 16:16)

When many tasks are queued the owner wants, per task, a one-line answer to "why isn't this
progressing": its place in the admission queue, how many goals are ahead of it, and whether a
resource (the concurrency cap, the cloud send rate, the disk floor, or a missing socket token)
is holding admission. The algorithm reads the SAME facts the admission sweep uses
(relay/relay_fleet.py `while pending:`), so it is implemented as a pure function and pinned
here at its boundaries -- no running fleet required.

## What this asserts

These import relay.stall_reason directly and exercise `stall_reason` / `resource_wait` /
`Stall.headline` at the boundaries that matter: admitted vs waiting, head vs behind, and each
of the four resource gates in the sweep's own order.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.stall_reason import Stall, resource_wait, stall_reason  # noqa: E402


# ── a goal that has already been admitted is NOT queue-blocked ──────────────────────────────

def test_a_goal_taking_turns_is_not_stalled():
    """turn > 0 => admitted; the queue cannot be its reason, so stalled is False."""
    s = stall_reason(status="waiting", turn=3, ahead=5, queue_len=20)
    assert s.stalled is False
    assert s.headline() == ""


def test_a_terminal_or_working_status_is_not_stalled():
    for st in ("verifying", "researching", "refuting", "awaiting_gate",
               "done", "stuck", "maxturns", "error"):
        s = stall_reason(status=st, turn=0, ahead=9, queue_len=30)
        assert s.stalled is False, st
        assert s.headline() == "", st


# ── a goal waiting behind others is explained by its rank ───────────────────────────────────

def test_a_goal_behind_others_reports_rank_and_count_ahead():
    s = stall_reason(status="pending", turn=0, ahead=7, queue_len=40)
    assert s.stalled is True
    assert s.rank == 8          # 7 ahead => 8th in line
    assert s.ahead == 7
    assert s.at_head is False
    assert s.resource == ""    # behind others => the rank is the blocker, not a resource
    h = s.headline()
    assert "待ち行列 8 番目" in h
    assert "前に 7 件" in h


def test_rank_is_one_based_and_head_is_rank_one():
    s = stall_reason(status="ready", turn=0, ahead=0, queue_len=5,
                     admits_another=False, active=12, cap=12)
    assert s.rank == 1
    assert s.at_head is True


# ── at the head, the resource gates decide, in the sweep's order ────────────────────────────

def test_resource_is_ignored_when_not_at_head():
    """A goal that is NOT at the head is held by its rank; a full cap does not change that."""
    assert resource_wait(at_head=False, admits_another=False, disk_ok=False,
                         rate_ok=False, token_ok=False) == ""


def test_resource_order_matches_the_sweep():
    # token first, then capacity, then disk, then rate -- the order of the sweep's gates.
    assert resource_wait(at_head=True, token_ok=False, admits_another=False,
                         disk_ok=False, rate_ok=False) == "token"
    assert resource_wait(at_head=True, token_ok=True, admits_another=False,
                         disk_ok=False, rate_ok=False) == "capacity"
    assert resource_wait(at_head=True, token_ok=True, admits_another=True,
                         disk_ok=False, rate_ok=False) == "disk"
    assert resource_wait(at_head=True, token_ok=True, admits_another=True,
                         disk_ok=True, rate_ok=False) == "rate"
    assert resource_wait(at_head=True, token_ok=True, admits_another=True,
                         disk_ok=True, rate_ok=True) == ""


def test_capacity_wait_shows_the_live_numbers():
    s = stall_reason(status="ready", turn=0, ahead=0, queue_len=3,
                     admits_another=False, active=12, cap=12)
    assert s.stalled is True
    assert s.resource == "capacity"
    h = s.headline()
    assert "同時実行の空き待ち" in h
    assert "実行 12/12" in h


def test_rate_wait_at_head():
    s = stall_reason(status="ready", turn=0, ahead=0, queue_len=2, rate_ok=False)
    assert s.stalled is True
    assert s.resource == "rate"
    assert "送信レート上限待ち" in s.headline()


def test_disk_wait_at_head():
    s = stall_reason(status="pending", turn=0, ahead=0, queue_len=2, disk_ok=False)
    assert s.resource == "disk"
    assert "ディスク空き待ち" in s.headline()


def test_token_wait_at_head():
    s = stall_reason(status="pending", turn=0, ahead=0, queue_len=2, token_ok=False)
    assert s.resource == "token"
    assert "ソケットトークン待ち" in s.headline()


# ── a lone goal at the head with every gate open is about to go, not stalled ────────────────

def test_a_clear_head_is_not_stalled():
    s = stall_reason(status="ready", turn=0, ahead=0, queue_len=1)
    assert s.stalled is False
    assert s.at_head is True
    assert s.headline() == ""


# ── as_dict carries the headline for the UI ─────────────────────────────────────────────────

def test_as_dict_includes_the_headline():
    s = stall_reason(status="pending", turn=0, ahead=2, queue_len=10)
    d = s.as_dict()
    assert d["stalled"] is True
    assert d["rank"] == 3
    assert d["headline"] == s.headline()
    assert "待ち行列 3 番目" in d["headline"]


def test_negative_and_none_inputs_do_not_raise():
    s = stall_reason(status=None, turn=None, ahead=-4, queue_len=0)
    assert s.ahead == 0
    assert s.rank == 1
    # at head, every gate open => not stalled
    assert s.stalled is False


def test_stall_is_immutable():
    s = Stall(stalled=True, rank=2, ahead=1)
    raised = False
    try:
        s.rank = 9  # frozen dataclass
    except Exception:
        raised = True
    assert raised is True
