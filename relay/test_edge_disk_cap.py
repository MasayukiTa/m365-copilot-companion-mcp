"""The ceiling has to hold in the cases that actually occurred.

Every test here corresponds to something the live system did wrong: a cap that read RAM, a
trim whose name list missed the directory holding the bytes, and a "cleared" report from a
browser that had not given the bytes back.
"""
import json
import os

import pytest

from relay import edge_disk_cap as C


def _profile(tmp_path, name, sizes):
    """A profile directory with `sizes` = {relative dir: bytes}."""
    root = tmp_path / name
    for rel, n in sizes.items():
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "blob.bin").write_bytes(b"\0" * n)
    (root / "Default").mkdir(parents=True, exist_ok=True)
    (root / "Default" / "Cookies").write_bytes(b"\0" * 4096)
    return root


def test_the_cap_is_two_megabytes_per_worker():
    assert C.cap_bytes(10) == 10 * 2 * 1024 * 1024
    assert C.cap_bytes(1) == 2 * 1024 * 1024


def test_no_fleet_still_has_a_ceiling():
    # A profile with no workers on it is still a browser holding a page. Zero would mean
    # re-fetching every asset of every navigation, and a missing count must tighten the cap
    # rather than remove it.
    assert C.cap_bytes(0) == 2 * 1024 * 1024
    assert C.cap_bytes(None) == 2 * 1024 * 1024
    assert C.cap_bytes("nonsense") == 2 * 1024 * 1024


def test_the_shader_cache_is_counted(tmp_path):
    # THE ORIGINAL BUG. The trim's name list said "shadercache" and Edge writes
    # "GrShaderCache", so the eval profile was trimmed, reported success, and freed zero of
    # the 12 MB it was holding. If this directory stops being counted, that returns.
    root = _profile(tmp_path, "copilot-eval-edge", {"GrShaderCache": 5_000_000})
    assert C.cache_bytes("copilot-eval-edge", base=str(tmp_path)) >= 5_000_000
    assert "grshadercache" in C.CACHE_DIR_NAMES


def test_the_measure_and_the_deletion_agree_about_what_a_cache_is():
    # Two lists naming the same directories is how the shader cache went missing on one side
    # only. The cap must read the same set the trim deletes, or it reports a number nothing
    # can act on.
    from relay.edge_recover import _CACHE_DIR_NAMES
    assert C.CACHE_DIR_NAMES is _CACHE_DIR_NAMES


def test_the_sign_in_is_not_counted_as_cache(tmp_path):
    # Cookies are what make these profiles persistent. Counting them would inflate the reading
    # and, worse, invite a deletion that costs a manual Entra sign-in.
    root = _profile(tmp_path, "copilot-bridge-edge", {"Default/Cache": 1_000_000})
    got = C.cache_bytes("copilot-bridge-edge", base=str(tmp_path))
    assert got == 1_000_000, got


def test_a_profile_under_the_cap_is_left_alone(tmp_path):
    _profile(tmp_path, "copilot-eval-edge", {"Default/Cache": 1000})
    rep = C.enforce(["copilot-eval-edge"], workers=1, base=str(tmp_path))
    assert rep["profiles"][0]["state"] == "under cap"


def test_a_stopped_profile_over_the_cap_is_trimmed(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "profile_is_running", lambda m: False)
    _profile(tmp_path, "copilot-eval-edge", {"Default/Cache": 9_000_000})
    rep = C.enforce(["copilot-eval-edge"], workers=1, base=str(tmp_path))
    e = rep["profiles"][0]
    assert e["state"] == "trimmed on disk"
    assert e["after_mb"] < e["before_mb"]


def test_a_running_profile_is_never_deleted_from_underneath(tmp_path, monkeypatch):
    # The project's standing rule: do not reach into the files of a process this did not
    # start. A running profile must go through the browser or not at all.
    monkeypatch.setattr(C, "profile_is_running", lambda m: True)
    calls = []
    monkeypatch.setattr(C, "trim_profile_caches",
                        lambda *a, **k: calls.append(a) or (0, []))
    monkeypatch.setattr(C, "clear_over_cdp", lambda m, **k: (True, "stub"))
    _profile(tmp_path, "copilot-eval-edge", {"Default/Cache": 9_000_000})
    C.enforce(["copilot-eval-edge"], workers=1, base=str(tmp_path))
    assert calls == [], "trim ran against a running profile"


def test_a_failed_clear_is_reported_as_failed(tmp_path, monkeypatch):
    # The bridge cleared 646 MB down to 358 MB and stopped. A caller that only sees a
    # megabyte total cannot tell that from "nothing needed doing", so the state has to say so.
    monkeypatch.setattr(C, "profile_is_running", lambda m: True)
    monkeypatch.setattr(C, "clear_over_cdp", lambda m, **k: (False, "cdp unreachable"))
    _profile(tmp_path, "copilot-eval-edge", {"Default/Cache": 9_000_000})
    rep = C.enforce(["copilot-eval-edge"], workers=1, base=str(tmp_path))
    assert rep["profiles"][0]["state"] == "running, clear failed"


def test_an_unmanaged_profile_is_not_touched(tmp_path, monkeypatch):
    # The user's own Edge is not in scope and never will be.
    monkeypatch.setattr(C, "profile_is_running", lambda m: False)
    _profile(tmp_path, "User Data", {"Default/Cache": 9_000_000})
    rep = C.enforce(["User Data"], workers=1, base=str(tmp_path))
    after = C.cache_bytes("User Data", base=str(tmp_path))
    assert after == 9_000_000, "an unmanaged profile lost bytes"


def test_the_worker_count_comes_from_the_live_fleet(tmp_path):
    (tmp_path / "status.json").write_text(
        json.dumps({"workers": [{"name": "w%d" % i} for i in range(7)]}), encoding="utf-8")
    assert C.worker_count(str(tmp_path)) == 7
    assert C.cap_bytes(C.worker_count(str(tmp_path))) == 7 * 2 * 1024 * 1024


def test_an_unreadable_status_tightens_rather_than_lifts_the_cap(tmp_path):
    # Failing open here would mean "cannot tell how many workers -> allow anything", which is
    # how the 1500 MB recycle number came to bound nothing at all.
    assert C.worker_count(str(tmp_path / "nope")) == 1
    (tmp_path / "status.json").write_text("{ not json", encoding="utf-8")
    assert C.worker_count(str(tmp_path)) == 1
