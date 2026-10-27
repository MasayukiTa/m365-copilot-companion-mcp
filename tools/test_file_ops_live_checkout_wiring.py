# -*- coding: utf-8 -*-
"""The live-checkout guard is actually WIRED into the one write chokepoint.

The guard's own verdict is unit-tested in test_live_checkout_write_guard.py. This file pins the
wiring: that file_ops._validate_path(for_write=True) -- the single function every write tool
(write_file, create_directory, copy_path, and the file writes run_python / shell /
edit_and_verify perform) funnels through -- calls the guard, re-raises its PermissionError in
block, and leaves a legitimate write untouched. A guard that is correct but not called is the
same crash it was written to stop.

Reads must never be touched: _validate_path(for_write=False) does not consult the guard at all,
so a worker can still READ the production checkout it must not WRITE.

Every test anchors the guard at a tmp_path 'checkout' with ui/ tracked by hand, and sets
MCP_ALLOWED_BASE='*' so the base-policy arm of _validate_path (unrelated to this guard) never
refuses the tmp path for its own reasons. workarea_guard is forced off for the same reason --
this file is about the live-checkout arm, not the placement arm beside it.
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

from tools import file_ops as F  # noqa: E402
from tools import live_write_guard as G  # noqa: E402


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "resonac-mcp"
    (root / "ui").mkdir(parents=True)
    (root / ".fleet").mkdir(parents=True)
    monkeypatch.setattr(G, "CHECKOUT_ROOT", root)
    monkeypatch.setattr(G, "LEDGER_PATH", root / ".fleet" / "live_write_refused.jsonl")
    monkeypatch.setattr(G, "_tracked_tops", lambda: frozenset({"ui", "relay", "tools"}))
    # The base-policy arm and the placement arm are not under test here; neutralise both so the
    # only thing that can refuse is the live-checkout guard.
    monkeypatch.setenv("MCP_ALLOWED_BASE", "*")
    monkeypatch.setattr(F, "ALLOWED_BASES", None)
    try:
        from tools import workarea_guard as W
        monkeypatch.setattr(W, "check_write", lambda _p: None)
    except Exception:
        pass
    monkeypatch.delenv(G.OPERATOR_ENV, raising=False)
    G._reset_for_test()
    yield root
    G._reset_for_test()


def _ledger(root):
    path = root / ".fleet" / "live_write_refused.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in io.open(path, encoding="utf-8").read().splitlines() if x.strip()]


def test_block_refuses_a_for_write_validate_on_tracked_source(checkout, monkeypatch):
    monkeypatch.setenv(G.MODE_ENV, "block")
    with pytest.raises(PermissionError) as ei:
        F._validate_path(str(checkout / "ui" / "FleetCockpit.Rows.cs"), for_write=True)
    assert "clone" in str(ei.value).lower()
    assert len(_ledger(checkout)) == 1


def test_a_read_validate_is_never_refused_even_in_block(checkout, monkeypatch):
    monkeypatch.setenv(G.MODE_ENV, "block")
    # for_write defaults to False: a worker may READ the checkout it may not WRITE.
    F._validate_path(str(checkout / "ui" / "FleetCockpit.Rows.cs"))   # must not raise
    assert _ledger(checkout) == []


def test_warn_records_but_lets_a_for_write_validate_through(checkout, monkeypatch):
    monkeypatch.setenv(G.MODE_ENV, "warn")
    F._validate_path(str(checkout / "ui" / "FleetCockpit.Rows.cs"), for_write=True)  # no raise
    rows = _ledger(checkout)
    assert len(rows) == 1 and rows[0]["action"] == "warned"


def test_a_write_into_a_clone_passes_validation(checkout, monkeypatch, tmp_path):
    monkeypatch.setenv(G.MODE_ENV, "block")
    clone = tmp_path / "m365-work" / "safety_rails"
    (clone / "ui").mkdir(parents=True)
    # Outside the production checkout -> the guard leaves it alone, validation returns the path.
    out = F._validate_path(str(clone / "ui" / "FleetCockpit.Rows.cs"), for_write=True)
    assert str(out).replace("\\", "/").endswith("safety_rails/ui/FleetCockpit.Rows.cs")
    assert _ledger(checkout) == []


def test_an_untracked_path_under_the_checkout_passes_validation(checkout, monkeypatch):
    monkeypatch.setenv(G.MODE_ENV, "block")
    # .fleet runtime state is not tracked source; a worker writes it normally.
    out = F._validate_path(str(checkout / ".fleet" / "status.json"), for_write=True)
    assert str(out).replace("\\", "/").endswith("resonac-mcp/.fleet/status.json")
    assert _ledger(checkout) == []
