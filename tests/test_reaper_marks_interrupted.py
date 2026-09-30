"""A dead coordinator leaves its unfinished workers `interrupted`, never `cancelled`.

Incident 2026-09-30: the fleet coordinator died natively with the disk full. The supervisor's
reaper then rewrote EVERY worker that was not closed to `cancelled`, so five healthy children
were shown as stopped by somebody. What it wrote was wrong (its liveness check was right):

* `cancelled` means a person said stop. Nobody did. The work is resumable.
* a worker that had already finished (`done`, just not yet closed) was rewritten too.
* the run's only resume input (fleet_run_active.json) was deleted without a copy.

Hermetic: tmp_path stands in for `.fleet/`, liveness is injected, nothing touches a process.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from relay import fleet_reaper
from relay.fleet_reaper import reap_stale_run

DEAD = lambda pid: False  # noqa: E731


def _w(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _r(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _worker(name, status, outcome=None, closed=False, **extra):
    w = {"name": name, "goal": "goal " + name, "status": status, "outcome": outcome,
         "pill": "x", "color": "good", "reason": "", "closed": closed,
         "run_id": "r1a2b_a1", "phase_events": [{"ts": 10, "event": status, "label": status}]}
    w.update(extra)
    return w


def _incident(tmp_path, *, with_stop=False):
    """The 2026-09-30 shape: FANOUT parent (done), two DONE children, five running children."""
    d = str(tmp_path)
    workers = [_worker("w0", "done", "FANOUT", closed=True),
               _worker("w1", "done", "DONE", closed=False),
               _worker("w2", "done", "DONE", closed=False)]
    for i in range(3, 8):
        workers.append(_worker("w%d" % i, "waiting"))
    status = {"started": 1000.0, "updated": time.time(), "total": 8, "done_count": 3,
              "running": True, "paused": False, "workers": workers}
    _w(os.path.join(d, "status.json"), status)
    _w(os.path.join(d, "fleet_run_active.json"),
       {"pid": 21520, "pid_birth": 77, "start_ts": 1000.0, "argv": ["-g", "x"],
        "resume_argv": ["--goals-file", "-"]})
    _w(os.path.join(d, "history.json"),
       [{"key": "k%d" % i, "name": "w%d" % i, "status": s, "outcome": o}
        for i, (s, o) in enumerate([("done", "FANOUT"), ("done", "DONE"), ("done", "DONE"),
                                    ("waiting", None), ("waiting", None)])])
    with open(os.path.join(d, "coordinator_20260930_p21520.log"), "w") as f:
        f.write("last line\n")
    with open(os.path.join(d, "campaigns.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "campaign", "campaign_id": "c1", "goal": "big goal",
                            "n": 7, "cwd": "X"}) + "\n")
        for i in range(7):
            f.write(json.dumps({"campaign_id": "c1", "task_id": "t%d" % i,
                                "subtask_index": i, "text": "sub %d" % i}) + "\n")
    if with_stop:
        _w(os.path.join(d, "commands.json"), {"stop": True})
    return d


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


def test_incident_unfinished_children_are_interrupted_not_cancelled(tmp_path):
    d = _incident(tmp_path)
    res = reap_stale_run(d, alive=DEAD)
    assert res and res["reaped"] and res["interrupted"] and not res["stopped"]
    st = _r(os.path.join(d, "status.json"))
    by = {w["name"]: w for w in st["workers"]}
    for name in ("w3", "w4", "w5", "w6", "w7"):
        w = by[name]
        assert w["status"] == "interrupted"
        assert w["outcome"] == "INTERRUPTED"
        assert w["pill"] == "中断" and w["color"] == "warn"
        assert w["reason"].startswith("coordinator died")
        assert "21520" in w["reason"]
        assert w["closed"] is False
        assert w["resumable"] is True and w["prior_status"] == "waiting"
    assert not [w for w in st["workers"] if w["status"] == "cancelled"]
    assert st["running"] is False
    assert st["done_count"] == 3          # the finished ones only
    ev = st["interrupted"]
    assert ev["pid"] == 21520 and ev["pid_birth"] == 77 and ev["resumable"] is True
    assert ev["coordinator_log"] == "coordinator_20260930_p21520.log"
    assert isinstance(ev["last_coordinator_log_ts"], float)
    assert ev["exit_code"] is None and ev["exit_evidence"] == []


def test_finished_workers_are_left_byte_identical_even_when_not_closed(tmp_path):
    d = _incident(tmp_path)
    before = {w["name"]: w for w in _r(os.path.join(d, "status.json"))["workers"]}
    reap_stale_run(d, alive=DEAD)
    after = {w["name"]: w for w in _r(os.path.join(d, "status.json"))["workers"]}
    for name in ("w0", "w1", "w2"):        # FANOUT parent + two DONE (not closed)
        assert after[name] == before[name], name
    assert after["w1"]["closed"] is False  # not forced closed either


def test_every_terminal_status_is_left_alone(tmp_path):
    d = str(tmp_path)
    terminal = ["done", "stuck", "maxturns", "error", "cancelled", "content_refused"]
    ws = [_worker("t%d" % i, s, "X") for i, s in enumerate(terminal)]
    ws.append(_worker("live", "waiting"))
    _w(os.path.join(d, "status.json"), {"total": 7, "running": True, "workers": ws,
                                        "updated": time.time()})
    _w(os.path.join(d, "fleet_run_active.json"), {"pid": 5})
    reap_stale_run(d, alive=DEAD)
    got = _r(os.path.join(d, "status.json"))["workers"]
    for a, b in zip(ws[:-1], got[:-1]):
        assert a == b
    assert got[-1]["status"] == "interrupted"


def test_the_reaper_terminal_set_mirrors_the_fleet_terminal_set():
    from relay.relay_fleet import TERMINAL
    assert set(fleet_reaper.TERMINAL_STATUSES) == set(TERMINAL)
    assert "interrupted" not in fleet_reaper.TERMINAL_STATUSES


def test_a_pending_user_stop_still_yields_cancelled(tmp_path):
    d = _incident(tmp_path, with_stop=True)
    res = reap_stale_run(d, alive=DEAD)
    assert res["stopped"] and not res["interrupted"] and not res["snapshot"]
    st = _r(os.path.join(d, "status.json"))
    live = [w for w in st["workers"] if w["name"] in ("w3", "w4", "w5", "w6", "w7")]
    assert live and all(w["status"] == "cancelled" and w["outcome"] == "CANCELLED"
                        and w["closed"] is True for w in live)
    assert "interrupted" not in st
    assert not os.path.isdir(os.path.join(d, "interrupted"))
    hist = _r(os.path.join(d, "history.json"))
    assert [h["status"] for h in hist[3:]] == ["cancelled", "cancelled"]


def test_a_stop_in_commands_d_or_a_claimed_file_also_counts(tmp_path):
    for rel in (os.path.join("commands.d", "0001.json"), "commands.json.claim-9-1"):
        sub = tmp_path / rel.replace(os.sep, "_").replace(".", "_")
        sub.mkdir()
        d = _incident(sub)
        os.remove(os.path.join(d, "commands.json")) if os.path.exists(
            os.path.join(d, "commands.json")) else None
        _w(os.path.join(d, rel), {"stop": True})
        assert reap_stale_run(d, alive=DEAD)["stopped"], rel


def test_second_call_is_a_noop_and_changes_no_byte(tmp_path):
    d = _incident(tmp_path)
    assert reap_stale_run(d, alive=DEAD)
    files = [os.path.join(d, n) for n in ("status.json", "history.json")]
    snap = os.path.join(d, "interrupted")
    snaps = {n: _bytes(os.path.join(snap, n)) for n in os.listdir(snap)}
    before = [_bytes(p) for p in files]
    assert reap_stale_run(d, alive=DEAD) is None
    assert [_bytes(p) for p in files] == before
    assert {n: _bytes(os.path.join(snap, n)) for n in os.listdir(snap)} == snaps


def test_a_live_coordinator_is_never_touched(tmp_path):
    d = _incident(tmp_path)
    before = {n: _bytes(os.path.join(d, n)) for n in ("status.json", "history.json",
                                                      "fleet_run_active.json")}
    assert reap_stale_run(d, alive=lambda p: True) is None
    assert {n: _bytes(os.path.join(d, n)) for n in before} == before
    assert not os.path.isdir(os.path.join(d, "interrupted"))


def test_history_entries_are_interrupted_and_finished_ones_untouched(tmp_path):
    d = _incident(tmp_path)
    reap_stale_run(d, alive=DEAD)
    h = _r(os.path.join(d, "history.json"))
    assert [(e["status"], e["outcome"]) for e in h] == [
        ("done", "FANOUT"), ("done", "DONE"), ("done", "DONE"),
        ("interrupted", "INTERRUPTED"), ("interrupted", "INTERRUPTED")]
    assert all("closed" not in e for e in h)     # closed left as it was


def test_snapshot_content_the_plan_the_workers_and_the_marker_copy(tmp_path):
    d = _incident(tmp_path)
    res = reap_stale_run(d, alive=DEAD)
    path = res["snapshot"]
    assert path == os.path.join(d, "interrupted", "r1a2b_a1.json")
    snap = _r(path)
    assert snap["schema"] == 1 and snap["state"] == "pending" and snap["run_id"] == "r1a2b_a1"
    # the resume input (the marker) survives inside the snapshot
    assert snap["marker"]["pid"] == 21520 and snap["marker"]["resume_argv"] == ["--goals-file", "-"]
    # the last worker states are the states BEFORE the rewrite
    st = {w["name"]: w["status"] for w in snap["workers"]}
    assert st["w0"] == "done" and st["w3"] == "waiting" and len(st) == 8
    # the fan-out plan
    (plan,) = snap["campaigns"]
    assert plan["campaign_id"] == "c1" and plan["n"] == 7 and plan["merged"] is False
    assert [c["subtask_index"] for c in plan["children"]] == list(range(7))
    assert snap["interrupted"]["pid"] == 21520
    # the live marker is consumed only AFTER the copy exists
    assert not os.path.exists(os.path.join(d, "fleet_run_active.json"))
    assert fleet_reaper.read_interrupted_snapshot(d)[0]["run_id"] == "r1a2b_a1"


def test_snapshot_write_failure_changes_nothing_and_never_raises(tmp_path, monkeypatch):
    d = _incident(tmp_path)
    names = ("status.json", "history.json", "fleet_run_active.json")
    before = {n: _bytes(os.path.join(d, n)) for n in names}
    real = os.replace

    def boom(src, dst):
        if os.sep + "interrupted" + os.sep in dst:
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    assert reap_stale_run(d, alive=DEAD) is None       # fail closed, no raise
    assert {n: _bytes(os.path.join(d, n)) for n in names} == before
    monkeypatch.setattr(os, "replace", real)
    assert reap_stale_run(d, alive=DEAD)["interrupted"]  # the next cycle retries and succeeds


def test_snapshot_is_written_atomically_no_tmp_left(tmp_path):
    d = _incident(tmp_path)
    reap_stale_run(d, alive=DEAD)
    assert not [n for n in os.listdir(os.path.join(d, "interrupted")) if n.endswith(".tmp")]


def test_corrupt_files_never_raise(tmp_path):
    d = str(tmp_path)
    _w(os.path.join(d, "fleet_run_active.json"), {"pid": 5})
    for name in ("status.json", "history.json", "campaigns.jsonl"):
        with open(os.path.join(d, name), "w") as f:
            f.write("{ not json ][")
    reap_stale_run(d, alive=DEAD)
    # workers that are not dicts, a list where a dict belongs
    _w(os.path.join(d, "status.json"), {"workers": [1, None, "x", {"status": "waiting"}]})
    _w(os.path.join(d, "fleet_run_active.json"), {"pid": 5})
    reap_stale_run(d, alive=DEAD)
    _w(os.path.join(d, "status.json"), ["not", "a", "dict"])
    _w(os.path.join(d, "fleet_run_active.json"), {"pid": 5})
    reap_stale_run(d, alive=DEAD)


def test_all_workers_finished_leaves_nothing_to_resume(tmp_path):
    d = str(tmp_path)
    _w(os.path.join(d, "status.json"), {"total": 1, "running": True, "updated": time.time(),
                                        "workers": [_worker("a", "done", "DONE")]})
    _w(os.path.join(d, "fleet_run_active.json"), {"pid": 5})
    res = reap_stale_run(d, alive=DEAD)
    assert res and not res["interrupted"] and not res["snapshot"]
    assert _r(os.path.join(d, "status.json"))["running"] is False
    assert not os.path.isdir(os.path.join(d, "interrupted"))


def test_exit_code_and_evidence_are_recorded_when_the_caller_knows_them(tmp_path):
    d = _incident(tmp_path)
    reap_stale_run(d, alive=DEAD, exit_code=-1073741818, exit_evidence=["sqlite3.dll"])
    ev = _r(os.path.join(d, "status.json"))["interrupted"]
    assert ev["exit_code"] == -1073741818 and ev["exit_evidence"] == ["sqlite3.dll"]


def test_the_resumer_reads_the_snapshot_copy_when_the_marker_is_gone(tmp_path):
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "resume_interrupted_fleet", os.path.join(root, "scripts", "win", "resume_interrupted_fleet.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from relay.fleet_runner import should_auto_resume
    d = _incident(tmp_path)
    reap_stale_run(d, alive=DEAD)
    assert mod.read_marker(d) is None                   # the live marker is gone ...
    marker, snap_path = mod.read_resume_source(d)       # ... the copy is the resume input
    assert marker["pid"] == 21520 and snap_path
    assert should_auto_resume(True, False) is True      # dead pid + input present -> resumable
    assert mod.resume_command(marker)[-1] == "--resume"
    assert fleet_reaper.mark_snapshot_state(snap_path, "resumed")
    assert mod.read_resume_source(d) == (None, None)    # a resumed run is not resumed twice


def test_interrupted_is_not_a_terminal_or_finished_or_retryable_state():
    from relay import outcomes
    from relay.relay_fleet import TERMINAL
    assert "interrupted" not in TERMINAL
    assert outcomes.STATUS_OF["INTERRUPTED"] == "interrupted"
    assert "INTERRUPTED" not in outcomes.RETRYABLE
    assert "INTERRUPTED" in outcomes.NON_RETRYABLE
    assert "INTERRUPTED" not in outcomes.FINISHED
    assert outcomes.scoring_of("INTERRUPTED", 5) == "fail"
    assert outcomes.scoring_of("INTERRUPTED", 0) == "excluded"
    assert outcomes.scoring_of("INTERRUPTED") == "fail"
