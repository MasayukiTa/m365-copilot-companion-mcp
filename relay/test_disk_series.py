# -*- coding: utf-8 -*-
"""Tests for relay/disk_series.py. The real C: is never scanned: every sample, free-space and
clock reading is injected, so these run on a Linux CI runner identically to Windows."""
import io
import json
import os

from relay import disk_series as ds


# ---- parse_processes / top_writers ---------------------------------------


def test_parse_processes_accepts_list_and_single_object():
    one = json.dumps({"ProcessId": 10, "Name": "a.exe", "WriteTransferCount": 5,
                      "ParentProcessId": 1, "CommandLine": "a.exe --x"})
    assert ds.parse_processes(one) == {10: ("a.exe", 5, "a.exe --x")}
    many = json.dumps([{"ProcessId": 1, "Name": "p", "WriteTransferCount": 0,
                        "ParentProcessId": 0, "CommandLine": None}])
    assert many_pid(many, 1)[2] == "ppid=0"


def many_pid(stdout, pid):
    return ds.parse_processes(stdout)[pid]


def test_parse_processes_never_raises_on_garbage():
    assert ds.parse_processes("") == {}
    assert ds.parse_processes("not json") == {}
    assert ds.parse_processes("null") == {}


def test_top_writers_charges_only_forward_deltas_and_sorts():
    prev = {1: ("a", 100, "a"), 2: ("b", 100, "b"), 3: ("c", 100, "c")}
    cur = {1: ("a", 100 + 10_000_000, "a"),   # +10 MB
           2: ("b", 50, "b"),                  # counter went backwards -> dropped
           3: ("c", 100 + 2_000_000, "c")}     # +2 MB
    top, total = ds.top_writers(prev, cur, dt=1.0)
    assert [w["name"] for w in top] == ["a", "c"]
    assert total == 12_000_000
    assert top[0]["mbps"] == 10.0


def test_top_writers_clamps_tiny_dt():
    prev = {1: ("a", 0, "a")}
    cur = {1: ("a", 1_000_000, "a")}
    top, _ = ds.top_writers(prev, cur, dt=0.0)   # clamped to 0.5 s
    assert top[0]["mbps"] == 2.0


# ---- is_swing ------------------------------------------------------------


def _series(points):
    """points: list of (ts_epoch, free_gb) -> series-shaped dicts."""
    return [{"ts_epoch": t, "free_gb": g, "top": []} for t, g in points]


def test_is_swing_detects_a_move_spread_over_several_samples():
    # 6 GB fall over 10 s (three 5 s samples), no single delta crossing 300 MB alone early.
    w = _series([(0, 8.0), (5, 5.0), (10, 2.0)])
    b = ds.is_swing(w, swing_threshold_mb=300.0)
    assert b is not None
    lo, hi = b
    assert lo["free_gb"] == 8.0 and hi["free_gb"] == 2.0


def test_is_swing_ignores_moves_outside_the_window():
    # 6 GB apart but 30 s apart -> not within a 10 s window.
    w = _series([(0, 8.0), (30, 2.0)])
    assert ds.is_swing(w, swing_threshold_mb=300.0) is None


def test_is_swing_ignores_small_moves():
    w = _series([(0, 8.0), (5, 7.9)])  # 100 MB
    assert ds.is_swing(w, swing_threshold_mb=300.0) is None


# ---- compute_hs_disk -----------------------------------------------------


def test_hs_disk_reports_free_slope_and_swing_count():
    series = _series([(0, 8.0), (300, 6.0), (600, 4.0)])
    swings = [{"ts_epoch": 540}]
    hs = ds.compute_hs_disk(series, swings, now_epoch=600, warn_threshold_gb=5.0)
    assert hs["free_gb"] == 4.0
    assert hs["swings_10min"] == 1
    # free fell 4 GB over 600 s -> -4.0 GB per 10 min.
    assert hs["slope_gb_per_10min"] == -4.0
    assert hs["warn"] is True          # 4.0 <= 5.0
    assert hs["warn_gb"] == 5.0


def test_hs_disk_no_warning_above_threshold():
    hs = ds.compute_hs_disk(_series([(0, 9.0)]), [], now_epoch=0, warn_threshold_gb=5.0)
    assert hs["warn"] is False


def test_hs_disk_distinguishes_measured_nothing():
    hs = ds.compute_hs_disk([], [], now_epoch=0, warn_threshold_gb=5.0)
    assert hs["checked_age_s"] == -1.0
    assert hs["free_gb"] is None


# ---- merge_health_strip --------------------------------------------------


def test_merge_preserves_cockpit_keys():
    existing = json.dumps({"ts": 1.0, "dots": [{"key": "x"}], "queued_count": 3})
    out = json.loads(ds.merge_health_strip(existing, {"free_gb": 4.0}))
    assert out["queued_count"] == 3
    assert out["dots"] == [{"key": "x"}]
    assert out["hs_disk"] == {"free_gb": 4.0}


def test_merge_survives_unreadable_existing():
    out = json.loads(ds.merge_health_strip("{not json", {"free_gb": 1.0}))
    assert out == {"hs_disk": {"free_gb": 1.0}}


# ---- cap_jsonl keeps the file under the ceiling --------------------------


def test_cap_jsonl_holds_the_file_under_the_ceiling(tmp_path):
    p = str(tmp_path / "disk_series.jsonl")
    # Write well past a tiny ceiling.
    with io.open(p, "w", encoding="utf-8") as f:
        for i in range(5000):
            f.write(json.dumps({"i": i, "pad": "x" * 200}) + "\n")
    before = os.path.getsize(p)
    max_mb = 0.1  # 100 KB ceiling
    freed = ds.cap_jsonl(p, max_mb=max_mb)
    after = os.path.getsize(p)
    assert before > 0.1 * 1024 * 1024
    assert after <= int(0.1 * 1024 * 1024)
    assert freed == before - after
    # The tail is intact: every surviving line parses and the LAST original line is kept.
    lines = io.open(p, encoding="utf-8").read().splitlines()
    assert lines, "cap must keep a non-empty tail"
    for ln in lines:
        json.loads(ln)  # no half-line at the head
    assert json.loads(lines[-1])["i"] == 4999


def test_append_jsonl_never_exceeds_cap(tmp_path):
    p = str(tmp_path / "disk_series.jsonl")
    max_mb = 0.05  # 50 KB
    for i in range(3000):
        ds.append_jsonl(p, {"i": i, "pad": "y" * 100}, max_mb=max_mb)
        assert os.path.getsize(p) <= int(max_mb * 1024 * 1024)


# ---- the resident loop, fully injected (no real C:) ----------------------


class _Clock:
    def __init__(self, start=0.0, step=5.0):
        self.t = start
        self.step = step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


def test_run_writes_series_swings_and_hs_disk(tmp_path, monkeypatch):
    state = tmp_path / ".fleet"
    state.mkdir()
    # Free space: steady, then a 6 GB fall across two samples -> a swing.
    frees = [8.0e9, 8.0e9, 5.0e9, 2.0e9, 2.0e9]
    free_iter = iter(frees)

    # One writer that grows its WriteTransferCount every sample.
    counter = {"w": 0}

    def sample_fn():
        counter["w"] += 50_000_000
        return {4242: ("greedy.exe", counter["w"], "greedy.exe --write")}

    def free_fn():
        return next(free_iter)

    monkeypatch.setattr(ds, "swing_mb", lambda: 300.0)
    monkeypatch.setattr(ds, "warn_gb", lambda: 5.0)
    # Avoid real sleeping; stop after the frees are consumed.
    ds.run(state_dir=str(state), sample_fn=sample_fn, free_fn=free_fn,
           now_fn=_Clock(step=5.0), stop_after=len(frees), interval_s=0.0001)

    series_p = state / "disk_series.jsonl"
    swings_p = state / "disk_swings.jsonl"
    strip_p = state / "health_strip.json"
    assert series_p.exists()
    series = [json.loads(l) for l in series_p.read_text(encoding="utf-8").splitlines()]
    assert series, "a series line per sample after the first"
    assert series[-1]["top"][0]["name"] == "greedy.exe"

    assert swings_p.exists(), "the 6 GB fall must be recorded as a swing"
    swings = [json.loads(l) for l in swings_p.read_text(encoding="utf-8").splitlines()]
    assert len(swings) == 1, "one swing, not one per sample"
    sw = swings[0]
    assert sw["direction"] == "down"
    assert sw["leaders"][0]["name"] == "greedy.exe"
    assert sw["leaders"][0]["pid"] == 4242

    strip = json.loads(strip_p.read_text(encoding="utf-8"))
    assert "hs_disk" in strip
    assert strip["hs_disk"]["free_gb"] == 2.0
    assert strip["hs_disk"]["warn"] is True


def test_run_merges_into_an_existing_strip_without_losing_keys(tmp_path, monkeypatch):
    state = tmp_path / ".fleet"
    state.mkdir()
    (state / "health_strip.json").write_text(
        json.dumps({"ts": 1.0, "dots": [{"key": "server"}], "queued_count": 7}),
        encoding="utf-8")
    frees = [8.0e9, 8.0e9, 8.0e9]
    it = iter(frees)
    monkeypatch.setattr(ds, "swing_mb", lambda: 300.0)
    monkeypatch.setattr(ds, "warn_gb", lambda: 5.0)
    ds.run(state_dir=str(state), sample_fn=lambda: {1: ("p", 0, "p")},
           free_fn=lambda: next(it), now_fn=_Clock(step=5.0),
           stop_after=len(frees), interval_s=0.0001)
    strip = json.loads((state / "health_strip.json").read_text(encoding="utf-8"))
    assert strip["queued_count"] == 7          # cockpit key preserved
    assert strip["dots"] == [{"key": "server"}]
    assert strip["hs_disk"]["warn"] is False    # 8 GB > 5 GB
