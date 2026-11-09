# -*- coding: utf-8 -*-
"""Stage B: the on_tick pass must stop paying for finished workers EVERY tick, without
changing what any consumer produces.

Background (measured): in a run with ~540 workers the coordinator's periodic pass over
all workers took 430-470 s. Stage A moved the per-row sqlite transcript writes off the
loop. Stage B is the on_tick callback in relay/fleet_runner.py handing the WHOLE worker
list to several consumers on every tick -- finished and terminally-stuck workers included.

The optimisation keeps a set of terminal worker ids and hands the live-only consumers a
shorter list. The hard requirement is that NOTHING a consumer outputs may change:

  * The status snapshot must still LIST terminal workers. _snapshot is always called with
    the full worker list, so this file asserts the snapshot is byte-for-byte identical
    whether or not the skip set exists (test_snapshot_lists_terminal_workers_*).
  * The conversation registry is the one live-only consumer. merge_conv_rows updates a
    terminal worker's row in place until its url and transcript are recorded, after which
    it returns changed=False for that row -- so dropping it from later ticks leaves
    conversations.json identical. test_registry_identical_with_and_without_skip pins that.

Importable without a browser or server: the helpers under test are pure.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

# importable without a browser / server
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import fleet_runner as fr  # noqa: E402
from relay.relay_fleet import TERMINAL  # noqa: E402


def _w(name, status="waiting", goal="g", url="", transcript=""):
    """A worker stub carrying only the attributes _snapshot / merge_conv_rows read.

    _snapshot reads name/goal/status/outcome/turn/max_turns/reason directly and everything
    else through getattr-with-default, so this minimal shape is enough. conv_url /
    transcript feed _register_convs -> merge_conv_rows.
    """
    outcome = "DONE" if status == "done" else None
    return SimpleNamespace(
        name=name, goal=goal, status=status, outcome=outcome,
        turn=1, max_turns=10, reason="",
        task_envelope=None, conv_url=url, transcript=transcript,
        last_response="",
    )


# ---- worker_is_terminal ----------------------------------------------------------------

def test_worker_is_terminal_matches_the_TERMINAL_tuple():
    for st in TERMINAL:
        assert fr.worker_is_terminal(_w("x", st)) is True, st
    # not yet run, or still running -> not terminal (the sweep loop treats these the same)
    for st in ("pending", "awaiting_execution", "waiting", "verifying", "refuting", "ready"):
        assert fr.worker_is_terminal(_w("x", st)) is False, st
    # 'interrupted' is deliberately NOT in TERMINAL (only a dead coordinator's reaper writes
    # it); a live coordinator must not treat it as finished.
    assert "interrupted" not in TERMINAL
    assert fr.worker_is_terminal(_w("x", "interrupted")) is False


# ---- update_terminal_ids ----------------------------------------------------------------

def test_a_worker_joins_the_set_when_it_goes_terminal():
    ids = set()
    workers = [_w("w0", "waiting"), _w("w1", "done"), _w("w2", "stuck")]
    out = fr.update_terminal_ids(workers, ids)
    assert out is ids  # updated in place, returned for convenience
    assert ids == {"w1", "w2"}


def test_a_state_change_removes_a_worker_from_the_set():
    ids = set()
    w0 = _w("w0", "done")
    fr.update_terminal_ids([w0], ids)
    assert ids == {"w0"}
    # an event touched it: it is no longer terminal (e.g. re-attached / revived)
    w0.status = "waiting"
    fr.update_terminal_ids([w0], ids)
    assert ids == set()


def test_resubmission_onto_the_same_name_removes_it_from_the_set():
    # w0 finished, then a new goal was submitted onto a fresh worker reusing the name w0.
    ids = set()
    fr.update_terminal_ids([_w("w0", "done")], ids)
    assert ids == {"w0"}
    fr.update_terminal_ids([_w("w0", "waiting")], ids)  # same name, live again
    assert ids == set()


def test_a_vanished_worker_name_is_dropped_from_the_set():
    # reconnect chunks can rebuild the worker list; a name no longer present must not linger.
    ids = set()
    fr.update_terminal_ids([_w("w0", "done"), _w("w1", "done")], ids)
    assert ids == {"w0", "w1"}
    fr.update_terminal_ids([_w("w1", "done")], ids)  # w0 gone from the list
    assert ids == {"w1"}


# ---- live_only --------------------------------------------------------------------------

def test_live_only_removes_terminal_workers_and_preserves_order():
    workers = [_w("w0", "waiting"), _w("w1", "done"), _w("w2", "verifying"),
               _w("w3", "stuck")]
    ids = set()
    fr.update_terminal_ids(workers, ids)
    live = fr.live_only(workers, ids)
    assert [w.name for w in live] == ["w0", "w2"]


def test_live_only_with_empty_set_returns_everything():
    workers = [_w("w0", "waiting"), _w("w1", "verifying")]
    assert fr.live_only(workers, set()) == workers
    assert fr.live_only(workers, None) == workers


# ---- EQUALITY: the status snapshot still lists terminal workers ------------------------

def _mixed():
    return [_w("w0", "done"), _w("w1", "waiting"), _w("w2", "stuck"),
            _w("w3", "verifying"), _w("w4", "maxturns")]


def _snap_names(snap):
    return [row["name"] for row in snap["workers"]]


def _stable(snap):
    """Drop the fields _snapshot stamps from the wall clock / live machine so two snapshots
    taken a few microseconds apart can be compared for structural equality."""
    import copy
    s = copy.deepcopy(snap)
    for k in ("updated", "avail_mb", "free_disk_gb", "disk", "quota", "token_wait"):
        s.pop(k, None)
    return s


def test_snapshot_lists_terminal_workers():
    # The snapshot is ALWAYS built from the full list -- that is the contract the skip
    # optimisation must not break. All five workers, terminal or not, appear.
    workers = _mixed()
    snap = fr._snapshot(workers, 0.0, len(workers))
    assert _snap_names(snap) == ["w0", "w1", "w2", "w3", "w4"]
    assert snap["done_count"] == 3  # w0 done, w2 stuck, w4 maxturns are all TERMINAL


def test_snapshot_identical_with_and_without_the_skip_set():
    # Build the snapshot the way on_tick does (full list), and the way a NAIVE skip would
    # if it were (wrongly) applied to the snapshot too. The optimisation keeps the snapshot
    # on the full list, so the correct snapshot must equal the full-list one and must NOT
    # equal the skipped one -- proving the snapshot output is preserved only because the
    # full list is used.
    workers = _mixed()
    ids = set()
    fr.update_terminal_ids(workers, ids)

    snap_full = fr._snapshot(workers, 0.0, len(workers))              # what on_tick does
    snap_full_again = fr._snapshot(workers, 0.0, len(workers))        # skip set exists, snapshot still full
    assert _stable(snap_full) == _stable(snap_full_again)

    # Sanity: had the skip been (incorrectly) applied to the snapshot, terminal rows would
    # vanish -- so the equality above is meaningful, not vacuous.
    snap_skipped = fr._snapshot(fr.live_only(workers, ids), 0.0, len(workers))
    assert _snap_names(snap_skipped) == ["w1", "w3"]
    assert _snap_names(snap_full) != _snap_names(snap_skipped)


# ---- EQUALITY: the conversation registry is identical with and without the skip ---------

def _register(existing, workers):
    """Reproduce the pointer-row shape _register_convs feeds merge_conv_rows, then merge.

    _register_convs also computes a display title, but merge_conv_rows deliberately never
    recomputes a title on an existing row and stamps ts only when a pointer field moves,
    so the registry's convergence depends only on url / transcript / name / goal -- the
    fields reproduced here. 'now' is pinned so ts is deterministic."""
    entries = []
    for w in workers:
        u = getattr(w, "conv_url", "") or ""
        tr = getattr(w, "transcript", "") or ""
        if not u and not tr:
            continue
        entries.append({"url": u, "title": (w.goal or "")[:60], "source": "fleet",
                        "transcript": tr, "goal": w.goal or "",
                        "name": getattr(w, "name", ""), "ts": 1000.0})
    merged, _changed = fr.merge_conv_rows(existing, entries, now=1000.0)
    return merged


def test_registry_identical_with_and_without_skip():
    # w0/w2 finish; w1/w3 stay live. Each worker already has a url+transcript recorded.
    workers = [_w("w0", "done", url="u0", transcript="t0"),
               _w("w1", "waiting", url="u1", transcript="t1"),
               _w("w2", "stuck", url="u2", transcript="t2"),
               _w("w3", "verifying", url="u3", transcript="t3")]

    # Tick 1: every worker is registered (none are in the skip set yet). Same for both paths.
    reg_full = _register([], workers)
    reg_skip = _register([], workers)
    assert reg_full == reg_skip

    ids = set()
    fr.update_terminal_ids(workers, ids)
    assert ids == {"w0", "w2"}

    # Ticks 2..N: the FULL path keeps registering every worker; the SKIP path registers only
    # the live ones. Because w0/w2's rows already carry their url+transcript, merge_conv_rows
    # produces no change for them, so the two registries must stay identical.
    for _ in range(5):
        reg_full = _register(reg_full, workers)
        reg_skip = _register(reg_skip, fr.live_only(workers, ids))
    assert reg_full == reg_skip, "skipping settled terminal rows changed the registry"

    # And the terminal rows are STILL in the registry (skipping them does not delete them).
    assert {r["name"] for r in reg_skip} == {"w0", "w1", "w2", "w3"}


def test_registry_skip_matches_full_even_when_a_live_worker_moves():
    # A live worker's transcript pointer changes on a later tick (url arrives / transcript
    # path updated). The skip path must still converge to the same registry as the full path,
    # because only terminal rows are skipped and those are the ones that no longer move.
    workers = [_w("w0", "done", url="u0", transcript="t0"),
               _w("w1", "waiting", url="", transcript="t1")]  # w1 url not captured yet
    ids = set()
    fr.update_terminal_ids(workers, ids)

    reg_full = _register([], workers)
    reg_skip = _register([], workers)

    # later tick: w1's url is now known
    workers[1].conv_url = "u1"
    reg_full = _register(reg_full, workers)
    reg_skip = _register(reg_skip, fr.live_only(workers, ids))
    assert reg_full == reg_skip
    row_w1 = [r for r in reg_skip if r["name"] == "w1"][0]
    assert row_w1["url"] == "u1"
