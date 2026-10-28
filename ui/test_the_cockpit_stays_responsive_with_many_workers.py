# -*- coding: utf-8 -*-
"""The cockpit must stay operable when the fleet is large.

The owner reported (2026-10-27) that with a large number of tasks the Fleet Cockpit
became too heavy to minimize / maximize / interact with. The measured cause was the
700 ms refresh tick doing two expensive things on EVERY fire, on the UI thread:

  1. re-parsing the whole status.json even when it had not changed, and
  2. building EVERY history entry as a realized UIElement (the list is deliberately
     NOT virtualized), so N archived tasks became N measured/arranged cards per render.

The fix (ui/FleetCockpit.Tick.cs, .Rows.cs, .History.cs, .cs) added:
  - a parse cache keyed on (mtime, size) so an unchanged status.json is not re-parsed,
  - a history row cap (HISTORY_DEFAULT_ROWS = 50) with a "さらに表示" row for the rest,
    while the header / left-rail count stays the FULL count, and
  - an adaptive tick interval clamp(max(base, ~3x last cost), base, max), bounds
    [700 ms, 5 s], so heavy ticks do not fire back-to-back.

These tests exercise Python models and source wiring, not the WPF runtime
or its performance. Inputs are synthetic.
"""

import json
import os
import re
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
# The constant the C# default cap is named with, and the source that defines it.
HISTORY_DEFAULT_ROWS = 50
SRC_MAIN = os.path.join(HERE, "FleetCockpit.cs")
SRC_TICK = os.path.join(HERE, "FleetCockpit.Tick.cs")
SRC_ROWS = os.path.join(HERE, "FleetCockpit.Rows.cs")
SRC_HISTORY = os.path.join(HERE, "FleetCockpit.History.cs")

TICK_BASE_MS = 700.0
TICK_MAX_MS = 5000.0


# ---------------------------------------------------------------------------
# Synthetic fleet and history; no production data or checked-in fixture.
# ---------------------------------------------------------------------------
@pytest.fixture
def big_status():
    return {"workers": [{"name": "worker-%d" % i, "goal": "synthetic" * 100}
                        for i in range(283)]}


@pytest.fixture
def big_history():
    return [{"goal": "synthetic task %d" % i, "outcome": "done"} for i in range(244)]


# ---------------------------------------------------------------------------
# (a) Parse cache: an unchanged status.json must NOT be re-parsed.
# Mirrors ReadStatus(): cache keyed on (mtime, size); a real change re-parses once.
# ---------------------------------------------------------------------------
class StatusReader:
    """Python model of the C# ReadStatus() parse cache (mtime+size keyed)."""

    def __init__(self, path):
        self.path = path
        self.parses = 0
        self._valid = False
        self._mtime = None
        self._size = -1
        self._root = None
        self._read_at = 0

    def read(self, mtime, size, text, now=0):
        # unchanged (valid cache, same mtime AND size) -> serve cached, no parse
        if (self._valid and mtime == self._mtime and size == self._size
                and 0 <= now - self._read_at < 5):
            return self._root
        self._root = json.loads(text)      # the one expensive step
        self.parses += 1
        self._mtime, self._size, self._valid = mtime, size, True
        self._read_at = now
        return self._root


def test_unchanged_status_is_parsed_once_not_every_tick(big_status):
    text = json.dumps(big_status)
    size = len(text)
    r = StatusReader(None)
    # 20 ticks with the file never changing
    for _ in range(20):
        r.read(mtime=1000, size=size, text=text)
    assert r.parses == 1, "unchanged status.json must be parsed exactly once, not per tick"


def test_a_real_status_change_reparses_exactly_once(big_status):
    text = json.dumps(big_status)
    size = len(text)
    r = StatusReader(None)
    for _ in range(5):
        r.read(mtime=1000, size=size, text=text)
    assert r.parses == 1
    # the runner rewrites the file -> mtime changes -> exactly one more parse
    for _ in range(5):
        r.read(mtime=1001, size=size, text=text)
    assert r.parses == 2, "a changed status.json must re-parse once and then cache again"


def test_same_metadata_is_rechecked_after_cache_expiry(big_status):
    text = json.dumps(big_status)
    reader = StatusReader(None)
    reader.read(1000, len(text), text, now=0)
    reader.read(1000, len(text), text, now=5)
    assert reader.parses == 2


# ---------------------------------------------------------------------------
# (b)/(c) History row cap: build at most HISTORY_DEFAULT_ROWS rows, but the
# COUNT the owner sees stays the full ledger length.
# ---------------------------------------------------------------------------
def build_history_rows(history, show_all):
    """Python model of AppendHistoryRows: cap the DRAWN rows, keep the header count full.

    Returns (drawn_row_count, header_count, has_show_more).
    """
    total = len(history)
    capped = (not show_all) and total > HISTORY_DEFAULT_ROWS
    shown = HISTORY_DEFAULT_ROWS if capped else total
    drawn = 0
    # newest-first, one row per visible entry (mirrors the C# for-loop up to shown)
    for _ in range(shown):
        drawn += 1
    if capped:
        drawn += 1          # the "さらに表示 (残り M 件)" row (Kind 11)
    header_count = total    # HistoryHeader uses _history.Count -> always the full count
    return drawn, header_count, capped


def test_history_is_capped_to_the_default_rows_by_default(big_history):
    assert len(big_history) > HISTORY_DEFAULT_ROWS, "fixture must exceed the cap to be meaningful"
    drawn, header_count, has_more = build_history_rows(big_history, show_all=False)
    # drawn = 50 entry rows + 1 "show more" row
    assert drawn == HISTORY_DEFAULT_ROWS + 1
    assert has_more is True
    # the count the owner sees is the WHOLE ledger, not the capped number
    assert header_count == len(big_history)


def test_show_all_lifts_the_cap_and_count_is_unchanged(big_history):
    drawn, header_count, has_more = build_history_rows(big_history, show_all=True)
    assert drawn == len(big_history), "show-all must draw every entry"
    assert has_more is False, "no 'show more' row once everything is drawn"
    assert header_count == len(big_history), "the count is the same whether capped or not"


def test_the_cap_cuts_the_drawn_rows_far_below_the_total(big_history):
    capped_drawn, _, _ = build_history_rows(big_history, show_all=False)
    full_drawn, _, _ = build_history_rows(big_history, show_all=True)
    # the whole point: far fewer realized rows by default
    assert capped_drawn < full_drawn / 2


# ---------------------------------------------------------------------------
# Adaptive tick interval: clamp(max(base, ~3x last cost), base, max).
# ---------------------------------------------------------------------------
def next_interval_ms(cost_ms, base=TICK_BASE_MS, mx=TICK_MAX_MS):
    nxt = cost_ms * 3.0
    if nxt < base:
        nxt = base
    if nxt > mx:
        nxt = mx
    return nxt


@pytest.mark.parametrize("cost,expected", [
    (0.0, TICK_BASE_MS),      # a free tick stays at the base
    (10.0, TICK_BASE_MS),     # cheap tick: 3x = 30 ms, still clamped up to base
    (300.0, 900.0),           # heavy: 3x = 900 ms, between base and max
    (1000.0, 3000.0),         # ~1 s tick -> next is at least a few seconds (3 s)
    (5000.0, TICK_MAX_MS),    # very heavy: clamped to the 5 s ceiling
])
def test_adaptive_interval_backs_off_then_snaps_back(cost, expected):
    assert next_interval_ms(cost) == expected


def test_interval_never_leaves_its_bounds():
    for cost in (0, 1, 50, 233, 700, 1234, 9999, 100000):
        v = next_interval_ms(float(cost))
        assert TICK_BASE_MS <= v <= TICK_MAX_MS


def test_a_one_second_tick_does_not_fire_every_700ms():
    # the owner's words: a tick that costs ~1 s must leave at least a few seconds before the next
    assert next_interval_ms(1000.0) >= 3000.0


# ---------------------------------------------------------------------------
# Pin the constants in the C# sources so a later edit cannot silently drop a fix.
# (Reading source text only -- no build, no run.)
# ---------------------------------------------------------------------------
def _src(path):
    if not os.path.isfile(path):
        pytest.skip("source missing: %s" % os.path.relpath(path, HERE))
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def test_history_default_rows_constant_is_fifty_in_source():
    m = re.search(r"HISTORY_DEFAULT_ROWS\s*=\s*(\d+)", _src(SRC_MAIN))
    assert m, "HISTORY_DEFAULT_ROWS must be defined in FleetCockpit.cs"
    assert int(m.group(1)) == HISTORY_DEFAULT_ROWS


def test_tick_bounds_constants_are_700_and_5000_in_source():
    src = _src(SRC_MAIN)
    base = re.search(r"TickBaseMs\s*=\s*([0-9.]+)", src)
    mx = re.search(r"TickMaxMs\s*=\s*([0-9.]+)", src)
    assert base and mx, "TickBaseMs/TickMaxMs must be defined in FleetCockpit.cs"
    assert float(base.group(1)) == TICK_BASE_MS
    assert float(mx.group(1)) == TICK_MAX_MS


def test_parse_cache_is_wired_in_readstatus():
    src = _src(SRC_TICK)
    assert "_statusCacheValid" in src and "GetLastWriteTimeUtc" in src and "age < 5" in src, \
        "ReadStatus must cache on (mtime,size) and guard the first read"


def test_minimized_tick_is_skipped():
    assert "WindowState.Minimized" in _src(SRC_TICK), \
        "a minimized window must skip the tick body"


def test_show_more_row_and_cap_are_wired_in_rows():
    src = _src(SRC_ROWS)
    assert "HISTORY_DEFAULT_ROWS" in src, "the row builder must consult the cap"
    assert "_histShowAll" in src, "the row builder must honour the show-all opt-in"


def test_show_all_resets_on_a_new_search():
    assert "_histShowAll = false" in _src(SRC_HISTORY), \
        "a new history query must return to the capped view"


# ---------------------------------------------------------------------------
# A tiny timing smoke (NOT an assertion on absolute ms -- just that capping
# assembles strictly less work than the full build). Hermetic and machine-independent.
# ---------------------------------------------------------------------------
def _assemble(history, show_all):
    total = len(history)
    capped = (not show_all) and total > HISTORY_DEFAULT_ROWS
    shown = HISTORY_DEFAULT_ROWS if capped else total
    work = 0
    for e in list(reversed(history))[:shown]:
        hay = (str(e.get("goal_summary", "")) + " " + str(e.get("goal", "")) + " "
               + str(e.get("display_result", "")) + " " + str(e.get("last", "")) + " "
               + str(e.get("outcome", ""))).lower()
        work += len(hay)
    return work


def test_capped_assembly_does_strictly_less_work(big_history):
    capped = _assemble(big_history, show_all=False)
    full = _assemble(big_history, show_all=True)
    assert capped < full, "capping must assemble less row work than the full build"
