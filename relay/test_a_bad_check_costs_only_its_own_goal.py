# -*- coding: utf-8 -*-
"""Making a malformed check loud put it on a path that answers loudness with a browser reset.

`normalize_checks` raises `MalformedCheck` now instead of dropping a bad spec, which is right:
a dropped check is indistinguishable from no check, and that is why a merge gate had never run.
But raising puts the failure on whatever path happens to be underneath it, and the path under a
goal injected into a LIVE fleet was fleet_runner's generic recovery handler:

    except Exception as e:
        attempt += 1
        print("[recover] %s while connecting; hard reset + retry (attempt %d/%d)" ...)
        if not cdp_alive(args.cdp_url):
            hard_reset(port)

So one typo in one goal the cockpit added mid-run would be reported to the operator as a
CONNECTION failure, burn the whole recovery budget retrying what retrying cannot fix, and could
hard-reset the browser out from under every worker that was running fine. The goals already in
flight lost to a goal that never started.

A rejected injection must cost the injected goal and nothing else.

THE STARTUP PATH WAS ALREADY SAFE, and that is worth pinning too: `gtexts = [goal_fields(g)[0]
...]` runs long before Playwright, so a bad goals file fails with no browser open and nothing to
reset. What it lacked was legibility -- it named nothing, and the person reading it is looking
at their own goals file.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.relay_fleet as rf                        # noqa: E402
from relay.acceptance import MalformedCheck           # noqa: E402
from relay.relay_fleet import RelayWorker, TERMINAL, run_relay_fleet  # noqa: E402

BAD = {"text": "何かする", "checks": ["これをやったか確認して"]}   # a sentence, not a check spec
GOOD = {"text": "別の何かする"}


class _FakeContext:
    def cookies(self):
        return []


def _browserless(mp):
    mp.setattr(rf, "avail_phys_mb", lambda: 64000.0)
    mp.setattr(rf, "free_disk_gb", lambda path=None: 500.0)

    def fake_attach(self, context, agent_url):
        self.page = object()
        self.status = "waiting"
        return True

    def fake_poll(self):
        if self.status in TERMINAL:
            return True
        self.status, self.outcome = "done", "DONE"
        self.verified = True
        return True

    def fake_close(self):
        self.closed = True
        self.page = None
        self.drv = None

    mp.setattr(RelayWorker, "attach", fake_attach)
    mp.setattr(RelayWorker, "poll", fake_poll)
    mp.setattr(RelayWorker, "close", fake_close)


# ── the premise ───────────────────────────────────────────────────────────────────────────

def test_a_sentence_is_not_accepted_as_a_check():
    with pytest.raises(MalformedCheck):
        rf.goal_fields(BAD)


# ── the blast radius ──────────────────────────────────────────────────────────────────────

def test_a_bad_goal_injected_mid_run_does_not_kill_the_run(tmp_path, monkeypatch):
    """THE DEFECT. Unguarded, this raised out of run_relay_fleet into a handler that answers
    every exception with a browser hard reset and a retry budget."""
    _browserless(monkeypatch)
    box = []
    ticks = {"n": 0}

    def on_tick(workers):
        ticks["n"] += 1
        if ticks["n"] == 1:
            box.append(BAD)          # the cockpit adds a goal to the live fleet

    res = run_relay_fleet(_FakeContext(), [GOOD], "http://agent", max_concurrent=2,
                          poll_s=0, add_box=box, on_tick=on_tick,
                          notify=lambda *a, **k: None, fanout=False)

    assert len(res) == 1, (
        "不正な追加ゴール1件で、走行中のゴールまで失われている（結果 %d 件）" % len(res))
    assert res[0]["outcome"] == "DONE"


def test_the_rejection_is_reported_rather_than_silent(tmp_path, monkeypatch, capsys):
    """Refusing quietly would be the original defect wearing a different hat: the operator
    would see a goal they added simply never run."""
    _browserless(monkeypatch)
    box = []
    said = []
    ticks = {"n": 0}

    def on_tick(workers):
        ticks["n"] += 1
        if ticks["n"] == 1:
            box.append(BAD)

    run_relay_fleet(_FakeContext(), [GOOD], "http://agent", max_concurrent=2, poll_s=0,
                    add_box=box, on_tick=on_tick, notify=lambda m, *a, **k: said.append(m),
                    fanout=False)

    out = capsys.readouterr().out
    assert "refusing an injected goal" in out, out[-500:]
    assert any("受入検査" in m for m in said), said


def test_a_good_injected_goal_still_runs(tmp_path, monkeypatch):
    """The guard must not have swallowed the mechanism it guards."""
    _browserless(monkeypatch)
    box = []
    ticks = {"n": 0}

    def on_tick(workers):
        ticks["n"] += 1
        if ticks["n"] == 1:
            box.append({"text": "あとから足したまともなゴール"})

    res = run_relay_fleet(_FakeContext(), [GOOD], "http://agent", max_concurrent=2,
                          poll_s=0, add_box=box, on_tick=on_tick,
                          notify=lambda *a, **k: None, fanout=False)
    assert len(res) == 2


# ── the startup path, and the handler that must not see it ────────────────────────────────

def test_the_startup_path_names_the_goal_instead_of_unwinding():
    src = open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8").read()
    i = src.index("gtexts = [goal_fields(g)[0] for g in goals]")
    around = src[max(0, i - 700):i + 900]
    assert "except MalformedCheck" in around
    assert "has an unusable acceptance check" in around
    assert "sys.exit(2)" in around


def test_a_config_error_is_not_answered_with_a_browser_reset():
    """The rule is about the class, not the two routes that exist today: a configuration error
    is not a connection error whatever route it arrives by. The generic handler resets Edge and
    retries `max_recover` times -- for a deterministic bad spec that is a wrong diagnosis, a
    wasted budget, and a reset that costs every worker currently running."""
    src = open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8").read()
    specific = src.index("except MalformedCheck as e:")
    generic = src.index("except Exception as e:\n            attempt += 1")
    assert specific < generic, (
        "設定エラーの分岐が汎用ハンドラより後ろにある -- 先に汎用が捕まえてリセットする")
    branch = src[specific:generic]
    assert "hard_reset" not in branch and "attempt += 1" not in branch
    assert "SystemExit(2)" in branch
