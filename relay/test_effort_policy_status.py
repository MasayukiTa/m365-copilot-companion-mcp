# -*- coding: utf-8 -*-
"""status.json carries what the effort policy is REALLY set to, plus a per-worker badge.

Through the real `fleet_runner._snapshot`: top-level `effort_policy {mode, source, conflict}`
and per-worker effort_level / effort_source / effort_last_switch. Additive: with the policy off
the worker rows carry no effort keys, and status/outcome/pill are untouched.
"""
import io
import os
import sys
from types import SimpleNamespace

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import effort as effort_mod          # noqa: E402
from relay import effort_policy as ep           # noqa: E402
from relay import fleet_runner as FR            # noqa: E402
from tools import settings_path as SP           # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    f = tmp_path / "settings.txt"
    monkeypatch.setattr(SP, "NEW_PATH", str(f))
    monkeypatch.setenv("APPDATA", str(tmp_path / "noappdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))
    monkeypatch.delenv("MCP_EFFORT_POLICY", raising=False)
    ep._warned_conflict.clear()
    ep._settings_cache.clear()

    def put(text):
        with io.open(str(f), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        st = os.stat(str(f))
        os.utime(str(f), ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    return put


def _worker(level="max", goal=None):
    knobs = effort_mod.LEVELS[level]
    return SimpleNamespace(
        name="w0", goal="x", goal_record=goal or {"text": "x"}, status="running", outcome="", turn=1,
        max_turns=10, reason="", last_response="", tabs=1, phase_events=[], transcript="",
        refuter=knobs["refuter"], max_refute=knobs["max_refute"],
        max_research=knobs["max_research"], review_lenses=list(knobs["review_lenses"] or []))


def _snap(workers):
    return FR._snapshot(workers, 1726000000.0, len(workers), max_concurrent=6)


def test_top_level_block_follows_mode_info(env, monkeypatch):
    assert _snap([])["effort_policy"] == {"mode": "off", "source": "default", "conflict": False}
    env("effort_policy=shadow\n")
    assert _snap([])["effort_policy"] == {"mode": "shadow", "source": "settings", "conflict": False}
    monkeypatch.setenv("MCP_EFFORT_POLICY", "on")
    assert _snap([])["effort_policy"] == {"mode": "on", "source": "env", "conflict": True}


def test_snapshot_survives_a_broken_policy(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(ep, "mode_info", boom)
    assert "effort_policy" not in _snap([_worker()])


def test_off_adds_no_worker_keys_and_keeps_the_pill(env):
    row = _snap([_worker()])["workers"][0]
    assert not [k for k in row if k.startswith("effort_")]
    assert row["status"] == "running"
    assert row["pill"] == FR.STATUS_PILL.get("running", ("running", "muted"))[0]


def test_shadow_worker_badge_fields(env):
    env("effort_policy=shadow\n")
    w = _worker("max")
    row = _snap([w])["workers"][0]
    assert row["effort_level"] == "max" and row["effort_source"] == "run"
    assert "effort_last_switch" not in row


def test_goal_and_parent_sources(env):
    env("effort_policy=shadow\n")
    g = {"text": "x", "effort": "max"}
    assert _snap([_worker("max", g)])["workers"][0]["effort_source"] == "goal"
    g = {"text": "x", "metadata": {"effort": "min", "effort_source": "parent"}}
    assert _snap([_worker("min", g)])["workers"][0]["effort_source"] == "parent"


def test_last_switch_is_record_only_and_uses_the_state(env):
    env("effort_policy=shadow\n")
    w = _worker("auto")
    st = ep._shadow_state(w, "auto", "min")["state"]
    assert w.effort_state is st
    ep.apply(st, ep.Decision("down", "max", "first-pass upheld streak"), 4)
    row = _snap([w])["workers"][0]
    assert row["effort_source"] == "policy"
    assert row["effort_last_switch"] == {"turn": 4, "from": "auto", "to": "max",
                                         "reason": "first-pass upheld streak",
                                         "record_only": True}
    assert row["effort_level"] == "auto"      # the REAL level is unchanged by a virtual switch


def test_custom_knobs_get_no_badge(env):
    env("effort_policy=shadow\n")
    w = _worker("max")
    w.max_refute = 99
    row = _snap([w])["workers"][0]
    assert "effort_level" not in row
