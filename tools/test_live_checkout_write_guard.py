# -*- coding: utf-8 -*-
"""A fleet worker must not write the production checkout's tracked source.

WHAT BROKE. 2026-10-27 14:10-14:27 Fleet Cockpit crashed three times on launch with an
unhandled NullReferenceException. The cause was a fleet worker (ui_perf) writing an UNFINISHED
edit DIRECTLY into the production checkout's tracked UI source (ui/FleetCockpit.*.cs, +112
lines) instead of into a clone; rebuild_ui.ps1 then compiled the half-written source into the
live binary, which died on its first tick.

WHAT THESE TESTS PIN. tools/live_write_guard.py sits on the one write chokepoint
(file_ops._validate_path, for_write only) and, for a write that lands on TRACKED SOURCE under
the production checkout:

  * off   -> does nothing.
  * warn  -> records the write to the ledger and LETS IT PROCEED (default: measure before you
             close).
  * block -> refuses with PermissionError carrying the 'work in a clone' instruction, AND
             records it. Blocking returns the refusal to the worker so it can redo the write
             against a clone -- it does not stop the job.

And the three things that must NEVER be refused: a write OUTSIDE the checkout, a write to an
UNTRACKED path under it (.fleet runtime state, scratch files -- workarea_guard's concern), and
a write by OPERATIONS (the per-session env marker, or an allow-listed scope). The guard fails
open on any error deciding the verdict: a guard that cannot read its inputs must never be the
reason a working install stops writing.

CHECKOUT_ROOT and _tracked_tops are both derived from the real tree, so every test anchors the
guard at a tmp_path 'checkout' and fixes the tracked tops by hand -- the behaviour under test
is the verdict and the mode, not git's view of this repository.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import live_write_guard as G  # noqa: E402


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A fake production checkout rooted at tmp_path, with ui/relay/tools tracked.

    Anchors CHECKOUT_ROOT and the ledger under tmp_path, fixes the tracked tops so no git call
    is made, clears every cache and the two env overrides, and resets again on the way out so
    one test's env never leaks into the next.
    """
    root = tmp_path / "resonac-mcp"
    (root / "ui").mkdir(parents=True)
    (root / ".fleet").mkdir(parents=True)
    monkeypatch.setattr(G, "CHECKOUT_ROOT", root)
    monkeypatch.setattr(G, "LEDGER_PATH", root / ".fleet" / "live_write_refused.jsonl")
    monkeypatch.setattr(G, "_tracked_tops", lambda: frozenset({"ui", "relay", "tools", "bridge", "scripts"}))
    monkeypatch.delenv(G.MODE_ENV, raising=False)
    monkeypatch.delenv(G.OPERATOR_ENV, raising=False)
    G._reset_for_test()
    yield root
    G._reset_for_test()


def _set_mode(monkeypatch, mode):
    """Drive the mode through the env override -- the file path is exercised elsewhere."""
    monkeypatch.setenv(G.MODE_ENV, mode)


def _ledger_rows(root):
    path = root / ".fleet" / "live_write_refused.jsonl"
    if not path.exists():
        return []
    out = []
    for ln in io.open(path, encoding="utf-8").read().splitlines():
        ln = ln.strip()
        if ln:
            out.append(json.loads(ln))
    return out


# --------------------------------------------------------------------- classify
def test_a_tracked_top_under_the_checkout_is_tracked(checkout):
    verdict, _reason, seg = G.classify(str(checkout / "ui" / "FleetCockpit.Rows.cs"))
    assert verdict == "tracked"
    assert seg == "ui"


def test_a_path_outside_the_checkout_is_outside(checkout, tmp_path):
    clone = tmp_path / "m365-work" / "safety_rails" / "ui" / "FleetCockpit.Rows.cs"
    verdict, _reason, _seg = G.classify(str(clone))
    assert verdict == "outside"


def test_an_untracked_top_under_the_checkout_is_untracked(checkout):
    # .fleet is runtime state, not tracked source -- workarea_guard's concern, not this one.
    verdict, _reason, _seg = G.classify(str(checkout / ".fleet" / "status.json"))
    assert verdict == "untracked"


def test_the_checkout_root_itself_is_not_tracked_source(checkout):
    verdict, _reason, _seg = G.classify(str(checkout))
    assert verdict == "outside"


# --------------------------------------------------------------------- warn (default)
def test_warn_records_a_worker_write_and_lets_it_proceed(checkout, monkeypatch):
    _set_mode(monkeypatch, "warn")
    # warn must NOT raise -- the write proceeds; the ledger is the only evidence.
    G.check_write(str(checkout / "ui" / "FleetCockpit.Rows.cs"))
    rows = _ledger_rows(checkout)
    assert len(rows) == 1
    assert rows[0]["action"] == "warned"
    assert rows[0]["top"] == "ui"


def test_warn_is_the_default_when_no_mode_is_set(checkout, monkeypatch):
    # No env, no settings file value -> the guard's own DEFAULT_MODE decides.
    monkeypatch.delenv(G.MODE_ENV, raising=False)
    monkeypatch.setattr(G, "_settings_values", lambda: {})
    assert G.mode() == "warn"
    assert G.DEFAULT_MODE == "warn"


# --------------------------------------------------------------------- block
def test_block_refuses_a_worker_write_to_tracked_source(checkout, monkeypatch):
    _set_mode(monkeypatch, "block")
    with pytest.raises(PermissionError) as ei:
        G.check_write(str(checkout / "ui" / "FleetCockpit.Rows.cs"))
    msg = str(ei.value)
    assert "clone" in msg.lower()           # the refusal names the way out
    assert str(checkout) in msg              # identifies the protected checkout
    rows = _ledger_rows(checkout)
    assert len(rows) == 1 and rows[0]["action"] == "refused"


def test_block_leaves_a_write_into_a_clone_alone(checkout, monkeypatch, tmp_path):
    _set_mode(monkeypatch, "block")
    clone = tmp_path / "m365-work" / "safety_rails"
    (clone / "ui").mkdir(parents=True)
    # A clone is OUTSIDE the production checkout -> never this guard's concern, even in block.
    G.check_write(str(clone / "ui" / "FleetCockpit.Rows.cs"))
    assert _ledger_rows(checkout) == []


def test_block_leaves_an_untracked_path_under_the_checkout_alone(checkout, monkeypatch):
    _set_mode(monkeypatch, "block")
    # Writing runtime state (.fleet) from a worker is normal and must not be refused.
    G.check_write(str(checkout / ".fleet" / "status.json"))
    assert _ledger_rows(checkout) == []


# --------------------------------------------------------------------- off
def test_off_does_nothing_and_records_nothing(checkout, monkeypatch):
    _set_mode(monkeypatch, "off")
    G.check_write(str(checkout / "ui" / "FleetCockpit.Rows.cs"))
    assert _ledger_rows(checkout) == []


# --------------------------------------------------------------------- operator opt-out
def test_an_operator_env_marker_is_never_refused(checkout, monkeypatch):
    _set_mode(monkeypatch, "block")
    monkeypatch.setenv(G.OPERATOR_ENV, "1")
    # Operations deliberately driving a write tool at the checkout opts out per-session.
    G.check_write(str(checkout / "ui" / "FleetCockpit.Rows.cs"))
    assert _ledger_rows(checkout) == []


# --------------------------------------------------------------------- fail open
def test_the_guard_fails_open_when_the_verdict_cannot_be_decided(checkout, monkeypatch):
    _set_mode(monkeypatch, "block")

    def _boom(_path):
        raise RuntimeError("cannot classify")

    monkeypatch.setattr(G, "classify", _boom)
    # A guard that cannot read its own inputs must never be the reason a write stops.
    G.check_write(str(checkout / "ui" / "FleetCockpit.Rows.cs"))   # must not raise
    assert _ledger_rows(checkout) == []
