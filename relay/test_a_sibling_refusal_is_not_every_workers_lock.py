# -*- coding: utf-8 -*-
"""A refusal belongs to the worker that produced it, not to every worker with a turn open.

MEASURED on a 5.6 h, 439-worker run: 220 of 226 `classified_locked` events had 3-15 candidates
and 58 of 120 refusal records injected more than one worker. The prose and fallback branches of
`_looks_locked` read the refusal ledger without identity, so each bystander was sent an unlock
steer and spent its own `_unlock_attempts` budget (cap 4); workers that had already written
their files and replied DONE were finally filed STUCK ("unlock sent 4x, success not confirmed")
by their siblings' refusals, each holding a slot for 837-2,903 s.

These tests drive the real functions with a stand-in refusal ledger and a temp mechanism ledger.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import mechanism_telemetry as MT  # noqa: E402
from relay import relay_fleet as F  # noqa: E402
from relay import turn_windows as TW  # noqa: E402

PW = "pw-placeholder-not-a-credential"
PARAPHRASE = "unlock パスワード欠如で確定。STUCK: unlock パスワード未提供。"


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    TW.reset()
    monkeypatch.setattr(MT, "LOG", str(tmp_path / "mechanisms.jsonl"), raising=False)
    monkeypatch.setattr(F, "_unlock_password", lambda: PW)
    monkeypatch.setattr(F, "_worker_recently_granted", lambda *_a, **_k: False)
    yield
    TW.reset()


def _refusal(ts):
    return {"ts": ts, "detail": "[locked: no valid unlock token for 'x'] ...",
            "session": "s1", "event": "refused"}


@pytest.fixture
def refusals(monkeypatch):
    def _use(records):
        import tools.lock_state as ls
        monkeypatch.setattr(ls, "matching_records", lambda since, now=None: list(records))
    return _use


def _rows(tmp_path):
    path = tmp_path / "mechanisms.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _crowd(n, start=100.0):
    for i in range(n):
        TW.open_turn("w%d" % i, start)


def _worker(name="w0"):
    w = F.RelayWorker("write a file", name)
    w._turn_sent_at = 100.0
    return w


def test_five_open_turns_and_no_exclusive_match_inject_nobody(refusals, tmp_path):
    _crowd(5)
    refusals([_refusal(105.0)])
    seen = []
    assert not F._looks_locked("ordinary reply", since=100.0, worker="w0",
                               on_unattributed=seen.append)
    assert seen == [5]
    w = _worker("w0")
    w._decide("ファイルを書きました。次に進みます。" + PARAPHRASE)
    assert w._unlock_attempts == 0, "a bystander's budget was spent"
    assert PW not in (w.job or "")
    rows = [r for r in _rows(tmp_path) if r["mechanism"] == "unlock_refusal_unattributed"]
    assert rows and rows[0]["extra"]["candidates"] == 5
    assert rows[0]["instance"] == "w0" and "run_id" in rows[0]


def test_paraphrase_branch_also_declines_when_ambiguous(refusals):
    _crowd(3)
    refusals([_refusal(105.0)])
    seen = []
    assert not F._looks_locked(PARAPHRASE, since=100.0, worker="w1",
                               on_unattributed=seen.append)
    assert seen and seen[0] == 3


def test_exclusive_match_injects_that_worker_only(refusals):
    TW.open_turn("w0", 100.0)
    refusals([_refusal(105.0)])
    w = _worker("w0")
    w._decide("長い関係のない返答です。" * 3)
    assert w._unlock_attempts == 1
    assert PW in (w.job or "")


def test_a_bystander_next_to_an_exclusive_owner_is_untouched(refusals):
    """Refusal at a moment only w0 was in flight; w1 opened later and sees it in its window."""
    TW.open_turn("w0", 100.0)
    TW.open_turn("w1", 200.0)
    refusals([_refusal(105.0)])
    w1 = _worker("w1")
    w1._turn_sent_at = 100.0
    w1._decide("作業を続けます。" + PARAPHRASE)
    assert w1._unlock_attempts == 0


def test_no_windows_keeps_the_historical_behaviour(refusals):
    """Single-shot callers that never opened a window are not regressed."""
    refusals([_refusal(105.0)])
    assert F._looks_locked(PARAPHRASE, since=100.0, worker="w0")


def test_a_done_reply_is_never_injected(monkeypatch):
    TW.open_turn("w0", 100.0)
    # Even if classification says locked (e.g. an exclusive ledger match), a DONE reply that
    # quotes no refusal must not be steered.
    monkeypatch.setattr(F, "_looks_locked", lambda *a, **k: True)
    w = _worker("w0")
    w._decide("ファイルを書き終えました。\nDONE")
    assert w._unlock_attempts == 0
    assert PW not in (w.job or "")


def test_a_stuck_reply_claiming_the_lock_is_still_injected(monkeypatch):
    monkeypatch.setattr(F, "_looks_locked", lambda *a, **k: True)
    w = _worker("w0")
    w._decide("書込が拒否されました。\nSTUCK: unlock が必要")
    assert w._unlock_attempts == 1


def test_a_done_reply_quoting_the_server_refusal_is_not_exempt():
    reply = "[locked: no valid unlock token for 'x'] でしたが解錠しました。\nDONE"
    assert not F._reply_completed_without_claiming_lock(reply, 100.0, "w0")
    assert F._reply_completed_without_claiming_lock("終わりました。\nDONE", 100.0, "w0")
    assert not F._reply_completed_without_claiming_lock("STUCK: unlock", 100.0, "w0")


def test_the_budget_cap_still_ends_a_truly_stuck_worker(monkeypatch):
    monkeypatch.setattr(F, "_looks_locked", lambda *a, **k: True)
    w = _worker("w0")
    for _ in range(F.MAX_UNLOCK_ATTEMPTS):
        w._decide("[locked: no valid unlock token for 'x']")
        assert w.status == "ready"
    w._decide("[locked: no valid unlock token for 'x']")
    assert w._unlock_attempts == F.MAX_UNLOCK_ATTEMPTS
    assert w.status == "stuck" or "unlock" in (w.reason or "")


def test_the_new_mechanism_is_registered():
    assert "unlock_refusal_unattributed" in MT.MECHANISMS
