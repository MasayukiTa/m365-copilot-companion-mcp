# -*- coding: utf-8 -*-
"""前提が消えたカードは、消えたと言う。ただし閉じない。

カードは「名指ししたファイルがベースラインからずれた」ときに積まれる。
**ずれは、カードが答えられないまま終わることがある** — 別の理由で誰かが再署名するか、
コードが戻されるか。そのときカードの問いは主語を失い、**既に存在しない状態についての
決定を要求し続ける**。

実測 2026-09-22: `931997c4df88` は `relay/selfimprove/frozen.py` について尋ねているが、
そのファイルは同日の再署名以来ベースラインと一致している。

## 閉じないことが要点

`resolve` は今も APPROVED しか触らない。**開いているカードを閉じるのは、この処理が
オペレータの代わりに答えること** — `frozen.py` が自分の resolver の隣にそう書いている。
「尋ねられた当のことが、もう成り立っていない」は**答えではなく事実**なので、
印字して、判断は本人に残す。
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from relay.selfimprove import pending as P  # noqa: E402


@pytest.fixture(autouse=True)
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "QUEUE_PATH", str(tmp_path / "pending_decisions.jsonl"))


def _row(status=P.OPEN, files=("a.py",)):
    return {"id": "x", "status": status, "files": list(files), "reason": "drifted"}


def _frozen(monkeypatch, baseline, current):
    from relay.selfimprove import frozen as F

    monkeypatch.setattr(F, "load_baseline", lambda *a, **k: {"checksums": baseline})
    monkeypatch.setattr(F, "compute_checksums", lambda *a, **k: current)


def test_a_card_whose_files_no_longer_drift_is_marked(monkeypatch):
    """**欠陥そのもの。** 解決済みの状態について決定を求め続けていた。"""
    _frozen(monkeypatch, {"a.py": "h1"}, {"a.py": "h1"})
    note = P._premise_gone(_row())
    assert note and "any more" in note


def test_a_card_that_still_drifts_says_nothing(monkeypatch):
    """**まだ本当なら、口を出さない。** 注記が常に出るなら注記ではない。"""
    _frozen(monkeypatch, {"a.py": "h1"}, {"a.py": "CHANGED"})
    assert P._premise_gone(_row()) == ""


def test_only_one_of_several_files_still_drifting_is_enough(monkeypatch):
    """1つでもずれていれば、問いは生きている。"""
    _frozen(monkeypatch, {"a.py": "h1", "b.py": "h2"}, {"a.py": "h1", "b.py": "CHANGED"})
    assert P._premise_gone(_row(files=("a.py", "b.py"))) == ""


def test_an_approved_card_is_not_annotated(monkeypatch):
    """承認済みは別の経路（`_resolve_pending_for`）が閉じる。二重に語らない。"""
    _frozen(monkeypatch, {"a.py": "h1"}, {"a.py": "h1"})
    assert P._premise_gone(_row(status=P.APPROVED)) == ""


def test_it_never_closes_anything(monkeypatch):
    """**この検査がこのファイルの主旨。** 注記は状態を変えない。"""
    _frozen(monkeypatch, {"a.py": "h1"}, {"a.py": "h1"})
    pid = P.add(["a.py"], "drifted")
    assert P.status_of(pid) == P.OPEN
    P._premise_gone(_row())
    assert P.status_of(pid) == P.OPEN, "the listing decided something on the operator's behalf"


def test_an_unreadable_baseline_does_not_break_the_listing(monkeypatch):
    """台帳の一覧が、ベースラインを読めないせいで落ちてはならない。"""
    from relay.selfimprove import frozen as F

    monkeypatch.setattr(F, "load_baseline",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    assert P._premise_gone(_row()) == ""
