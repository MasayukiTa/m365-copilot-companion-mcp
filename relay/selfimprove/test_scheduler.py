"""Scheduled runs: the feature is small, the refusals are the substance.

An unattended loop is exactly the arrangement where a quiet defect compounds, so these tests
are almost entirely about the conditions under which a scheduled campaign declines to start
-- and about the one thing it must never do, which is retry past a failed precondition.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

import pytest

from relay.selfimprove import frozen as F
from relay.selfimprove import scheduler as S


def _lock():
    return os.path.join(tempfile.mkdtemp(prefix="sched_"), "campaign.lock")


def _ok_frozen(monkeypatch):
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (True, []))


def _states(**counts):
    out = []
    for state, n in counts.items():
        out += [{"state": state}] * n
    return out


# ---- the preconditions --------------------------------------------------------------------

def test_a_healthy_setup_may_run(monkeypatch):
    _ok_frozen(monkeypatch)
    assert S.preconditions(lock_path=_lock(), budget_candidates=5) == []


def test_a_changed_judge_blocks_the_run(monkeypatch):
    """判定者が変わった走行の数字は、無人だと他の行と見分けが付かないまま archive に入る。"""
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (False, ["guards.py"]))
    reasons = S.preconditions(lock_path=_lock(), budget_candidates=5)
    assert any("judge changed" in r for r in reasons)


def test_an_unreadable_frozen_check_blocks_rather_than_passes(monkeypatch):
    """無人経路は、ここで寛大になってよい最後の場所。"""
    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(F, "frozen_intact", boom)
    assert S.preconditions(lock_path=_lock(), budget_candidates=5)


def test_a_campaign_in_flight_blocks_the_next(monkeypatch):
    """archive と active manifest を共有した2つは候補を交互に書き、
    2つめのベースラインは1つめの途中状態になる。"""
    _ok_frozen(monkeypatch)
    path = _lock()
    S.take_lock(path)
    reasons = S.preconditions(lock_path=path, budget_candidates=5)
    assert any("in flight" in r for r in reasons)


def test_a_stale_lock_does_not_block_forever(monkeypatch):
    """クラッシュが翌日の走行まで止めてはいけない。"""
    _ok_frozen(monkeypatch)
    path = _lock()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"started_at": time.time() - S.STALE_LOCK_S - 60}, fh)
    assert S.preconditions(lock_path=path, budget_candidates=5) == []


def test_activation_needs_an_operator_who_approved_it(monkeypatch):
    """自分の勝者を自分で導入する定期実行は、誰も見ていない間に系が変わること。"""
    _ok_frozen(monkeypatch)
    reasons = S.preconditions(lock_path=_lock(), budget_candidates=5, activate=True)
    assert any("nobody is watching" in r for r in reasons)
    assert S.preconditions(lock_path=_lock(), budget_candidates=5, activate=True,
                           operator_approved_activation=True) == []


def test_no_budget_blocks(monkeypatch):
    _ok_frozen(monkeypatch)
    assert any("budget" in r for r in
               S.preconditions(lock_path=_lock(), budget_candidates=0))


def test_an_unwell_harness_blocks_and_reuses_the_existing_judgement(monkeypatch):
    """同じ判断を2箇所に書くと、片方だけ直る日が来る。"""
    _ok_frozen(monkeypatch)
    reasons = S.preconditions(lock_path=_lock(), budget_candidates=5,
                              recent_decisions=_states(INFRA_ABORT=5, REJECT=5))
    assert any("unwell" in r for r in reasons)


def test_every_reason_is_reported_at_once(monkeypatch):
    """1つ直して翌晩また次に気づく、を繰り返すと一週間走らない。"""
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (False, ["x"]))
    reasons = S.preconditions(lock_path=_lock(), budget_candidates=0, activate=True)
    assert len(reasons) >= 3


# ---- the run ----------------------------------------------------------------------------

def test_a_blocked_run_does_not_call_the_campaign(monkeypatch):
    monkeypatch.setattr(F, "frozen_intact", lambda *a, **k: (False, ["x"]))
    called = []
    out = S.scheduled_run(lambda budget: called.append(budget), lock_path=_lock())
    assert out["ran"] is False and called == []
    assert out["blocked_by"]


def test_a_permitted_run_calls_it_once_and_releases(monkeypatch):
    _ok_frozen(monkeypatch)
    path = _lock()
    calls = []
    out = S.scheduled_run(lambda budget: calls.append(budget) or "done",
                          budget_candidates=3, lock_path=path)
    assert out["ran"] is True and out["result"] == "done" and calls == [3]
    assert S.lock_held(path) == "", "ロックが解放されていない"


def test_the_lock_is_released_even_when_the_campaign_raises(monkeypatch):
    """例外で握ったままだと、次の晩も、その次も走らない。"""
    _ok_frozen(monkeypatch)
    path = _lock()

    def boom(_budget):
        raise RuntimeError("campaign exploded")

    with pytest.raises(RuntimeError):
        S.scheduled_run(boom, lock_path=path)
    assert S.lock_held(path) == ""


def test_it_never_retries():
    """前提条件の失敗は情報。踏み越えて再試行すると、
    『環境が壊れている』が『環境が壊れていて4時間溶かした』になる。"""
    import inspect
    src = inspect.getsource(S.scheduled_run)
    assert "while" not in src and "for _" not in src
    assert "retry" not in src.lower() or "never retries" in src.lower()


# ---- round 8: the lock was check-then-write, and nothing called any of this -------------

def test_two_schedulers_cannot_both_take_the_lock(monkeypatch):
    """lock_held してから open するのは2操作。両方が確認してから両方が書けば、
    2つとも進む -- ロックが防ぐはずの状況が、最も気づきにくい形で起きる。"""
    _ok_frozen(monkeypatch)
    path = _lock()
    S.take_lock(path)
    with pytest.raises(S.Blocked):
        S.take_lock(path)


def test_an_abandoned_lock_can_still_be_taken_over(monkeypatch):
    _ok_frozen(monkeypatch)
    path = _lock()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"started_at": time.time() - S.STALE_LOCK_S - 60}, fh)
    assert S.take_lock(path) == path


def test_there_is_an_entry_point_a_person_can_run():
    """テストからしか到達できない段階は、動く機能ではなく設計文書。"""
    assert callable(getattr(S, "nightly", None))
    import inspect
    src = inspect.getsource(S)
    assert '__main__' in src, "コマンドとして起動できない"


def test_the_nightly_run_refuses_rather_than_inventing_an_evaluator():
    """評価器を捏造して返す夜間実行は、測っていない数字を archive に書く。"""
    with pytest.raises(Exception) as exc:
        S._refuse()
    assert "will not invent one" in str(exc.value)


def test_a_lock_in_delete_pending_is_blocked_not_a_crash(tmp_path, monkeypatch):
    """Windows reports a lock being released as PermissionError, not FileExistsError.

    A file whose last handle has just closed with an unlink outstanding is in delete-pending,
    and O_CREAT|O_EXCL on it returns ERROR_ACCESS_DENIED. take_lock has exactly two intended
    outcomes -- it takes the lock, or it raises Blocked -- and catching FileExistsError alone
    added a third: a scheduler starting just as another released the lock died with
    PermissionError. The same gap was measured in the identical loop in relay/task_router.py
    at 2 failures in 8 runs with 24 concurrent writers.
    """
    import os as _os
    path = str(tmp_path / "campaign.lock")
    real_open = _os.open
    calls = {"n": 0}

    def flaky(p, flags, *a, **k):
        if str(p) == path:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "Permission denied")
        return real_open(p, flags, *a, **k)

    monkeypatch.setattr(_os, "open", flaky)
    # Nothing holds it, so the delete-pending refusal must resolve to taking the lock.
    assert S.take_lock(path, note="after a delete-pending refusal") == path
    assert calls["n"] >= 2, "the refusal was not actually exercised"
    assert os.path.isfile(path)
