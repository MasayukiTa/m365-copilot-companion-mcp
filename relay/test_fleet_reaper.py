"""Hermetic tests for relay/fleet_reaper.py.

All cases operate on a throwaway tmp_path directory standing in for `.fleet/` -- never
the real `.fleet` directory. Liveness is always injected via `alive=`, so these tests
never depend on psutil or on any real process/pid.
"""
from __future__ import annotations

import json
import os
import time

from relay.fleet_reaper import reap_stale_run


def _write(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _build_fixture(tmp_path, running=True, updated=None):
    fleet_dir = str(tmp_path)
    if updated is None:
        updated = time.time()

    _write(os.path.join(fleet_dir, "fleet_run_active.json"), {
        "pid": 999999,
        "start_ts": time.time() - 1000,
        "argv": ["-g", "do the thing"],
        "resume_argv": [],
    })

    status = {
        "started": time.time() - 1000,
        "updated": updated,
        "total": 2,
        "done_count": 0,
        "running": running,
        "paused": False,
        "workers": [
            {
                "name": "w-done",
                "goal": "already finished",
                "status": "done",
                "pill": "完了",
                "color": "done",
                "outcome": "DONE",
                "reason": "",
                "closed": True,
                "phase_events": [{"ts": 50, "event": "done", "label": "Finished"}],
            },
            {
                "name": "w-refuting",
                "goal": "still going",
                "status": "refuting",
                "pill": "反証中",
                "color": "good",
                "outcome": None,
                "reason": "",
                "closed": False,
                "phase_events": [{"ts": 100, "event": "refuting", "label": "Reviewing"}],
            },
        ],
    }
    _write(os.path.join(fleet_dir, "status.json"), status)

    history = [
        {"key": "1#w-refuting", "goal": "still going", "status": "refuting",
         "conv_title": "t", "outcome": None, "conv_url": "", "transcript": "",
         "name": "w-refuting", "turn": 3, "seq": 1, "ts": time.time()},
        {"key": "1#w-done", "goal": "already finished", "status": "done",
         "conv_title": "t2", "outcome": "DONE", "conv_url": "", "transcript": "",
         "name": "w-done", "turn": 5, "seq": 2, "ts": time.time()},
    ]
    _write(os.path.join(fleet_dir, "history.json"), history)

    return fleet_dir


def test_dead_pid_reaps_successfully(tmp_path):
    fleet_dir = _build_fixture(tmp_path)

    result = reap_stale_run(fleet_dir, alive=lambda p: False)

    assert isinstance(result, dict)
    assert result["reaped"] is True
    assert result["workers_closed"] == 1
    assert result["history_terminated"] == 1

    status = _read(os.path.join(fleet_dir, "status.json"))
    assert status["running"] is False
    assert status["paused"] is False

    done_worker = next(w for w in status["workers"] if w["name"] == "w-done")
    assert done_worker["status"] == "done"
    assert done_worker["outcome"] == "DONE"
    assert done_worker["closed"] is True
    assert len(done_worker["phase_events"]) == 1  # untouched

    refuting_worker = next(w for w in status["workers"] if w["name"] == "w-refuting")
    assert refuting_worker["closed"] is True
    assert refuting_worker["status"] == "cancelled"
    assert refuting_worker["outcome"] == "CANCELLED"
    assert refuting_worker["pill"] == "停止"
    assert refuting_worker["color"] == "muted"
    assert isinstance(refuting_worker["reason"], str) and refuting_worker["reason"]
    assert len(refuting_worker["phase_events"]) == 2
    assert refuting_worker["phase_events"][-1]["event"] == "cancelled"

    history = _read(os.path.join(fleet_dir, "history.json"))
    refuting_entry = next(e for e in history if e["name"] == "w-refuting")
    assert refuting_entry["status"] == "cancelled"
    done_entry = next(e for e in history if e["name"] == "w-done")
    assert done_entry["status"] == "done"

    assert not os.path.isfile(os.path.join(fleet_dir, "fleet_run_active.json"))


def test_live_pid_is_strict_noop(tmp_path):
    fleet_dir = _build_fixture(tmp_path)

    status_path = os.path.join(fleet_dir, "status.json")
    history_path = os.path.join(fleet_dir, "history.json")
    active_path = os.path.join(fleet_dir, "fleet_run_active.json")

    with open(status_path, "rb") as f:
        status_before = f.read()
    with open(history_path, "rb") as f:
        history_before = f.read()
    with open(active_path, "rb") as f:
        active_before = f.read()

    result = reap_stale_run(fleet_dir, alive=lambda p: True)

    assert result is None

    with open(status_path, "rb") as f:
        assert f.read() == status_before
    with open(history_path, "rb") as f:
        assert f.read() == history_before
    with open(active_path, "rb") as f:
        assert f.read() == active_before


def test_no_marker_fresh_status_is_noop(tmp_path):
    fleet_dir = str(tmp_path)
    status = {
        "started": time.time() - 10,
        "updated": time.time(),
        "total": 1,
        "done_count": 0,
        "running": True,
        "paused": False,
        "workers": [],
    }
    _write(os.path.join(fleet_dir, "status.json"), status)
    status_path = os.path.join(fleet_dir, "status.json")

    with open(status_path, "rb") as f:
        before = f.read()

    result = reap_stale_run(fleet_dir, alive=lambda p: False)

    assert result is None
    with open(status_path, "rb") as f:
        assert f.read() == before


def test_no_marker_stale_status_never_raises_and_stays_consistent(tmp_path):
    fleet_dir = str(tmp_path)
    status = {
        "started": time.time() - 2000,
        "updated": time.time() - 700,  # past the default 600s threshold
        "total": 1,
        "done_count": 0,
        "running": True,
        "paused": False,
        "workers": [{
            "name": "w1", "goal": "g", "status": "waiting", "pill": "実行中",
            "color": "good", "outcome": None, "reason": "", "closed": False,
            "phase_events": [],
        }],
    }
    _write(os.path.join(fleet_dir, "status.json"), status)

    # Must never raise regardless of outcome.
    result = reap_stale_run(fleet_dir, alive=lambda p: False)

    status_after = _read(os.path.join(fleet_dir, "status.json"))
    if result is not None:
        # This implementation treats staleness-without-a-marker as reapable.
        assert status_after["running"] is False
    else:
        # Conservative implementation: no marker means no action taken.
        assert status_after["running"] is True


def test_missing_and_corrupt_files_never_raise(tmp_path):
    # Completely empty directory.
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert reap_stale_run(str(empty_dir), alive=lambda p: False) is None

    # Corrupt marker (invalid JSON) -- treated as absent.
    corrupt_marker_dir = tmp_path / "corrupt_marker"
    corrupt_marker_dir.mkdir()
    with open(corrupt_marker_dir / "fleet_run_active.json", "w", encoding="utf-8") as f:
        f.write("{ this is not json ][")
    result = reap_stale_run(str(corrupt_marker_dir), alive=lambda p: False)
    assert result is None

    # Valid marker but corrupt status.json -- must not raise.
    corrupt_status_dir = tmp_path / "corrupt_status"
    corrupt_status_dir.mkdir()
    _write(str(corrupt_status_dir / "fleet_run_active.json"), {"pid": 999999})
    with open(corrupt_status_dir / "status.json", "w", encoding="utf-8") as f:
        f.write("not valid json at all {{{")
    # Should not raise; either None or a reap summary is acceptable.
    reap_stale_run(str(corrupt_status_dir), alive=lambda p: False)


def test_idempotent_second_call_is_noop(tmp_path):
    fleet_dir = _build_fixture(tmp_path)

    first = reap_stale_run(fleet_dir, alive=lambda p: False)
    assert first is not None

    second = reap_stale_run(fleet_dir, alive=lambda p: False)
    assert second is None
