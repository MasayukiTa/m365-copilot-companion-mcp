# -*- coding: utf-8 -*-
"""承認して実施したカードは閉じる。文言の書き方に依存してはならない。

**発見の経緯がそのまま設計の理由。** `_resolve_pending_for` は (files, reason) の組で
カードを特定していた。docstring は「理由を書き換えたら一致せずカードは開いたまま残るが、
古い『待機中』は目に見える不便で、誤って閉じたカードは嘘だから、こちらが安全側」と
書いていた。その判断は正しい。**ただし 2026-09-19 にダッシュボードの再署名ボタンが
通常経路になり、ボタンは常に自分の文言（"re-signed on the dashboard: ..."）を書く。**
カードが積まれたときの文言（"the frozen set no longer matches its baseline (...)"）とは
決して一致しないので、安全側が**唯一の側**になった。

結果: ダッシュボードで承認して実施したカードが、画面に「エージェントの実行待ち」として
永久に残る。キュー自体が終わらせるために導入された虚報の、1段先の姿。

だから鍵はファイルにした。**「承認した変更が、いま受理された」は文言ではなくファイルで
決まる。**
"""
from __future__ import annotations

import pytest

from relay.selfimprove import frozen as F
from relay.selfimprove import pending as P


@pytest.fixture(autouse=True)
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "QUEUE_PATH", str(tmp_path / "pending_decisions.jsonl"))


class _Args:
    reason = "any words at all"
    authorization = "the operator said so"


def _approved(files, reason):
    pid = P.add(list(files), reason)
    P.resolve(pid, authorization="yes", status=P.APPROVED, kind="typed")
    return pid


def test_a_card_closes_even_though_the_reason_was_reworded():
    """THE DEFECT. Same files, different words -- which is every dashboard re-signing."""
    pid = _approved(["relay/selfimprove/frozen.py"],
                    "the frozen set no longer matches its baseline (relay/selfimprove/frozen.py)")
    F._resolve_pending_for(["relay/selfimprove/frozen.py"], _Args())
    assert P.status_of(pid) == P.DONE, "the card stayed open because the wording differed"


def test_a_card_about_a_subset_of_what_was_signed_also_closes():
    """A re-signing may accept more files than one card asked about."""
    pid = _approved(["a.py"], "fix a")
    F._resolve_pending_for(["a.py", "b.py"], _Args())
    assert P.status_of(pid) == P.DONE


def test_a_card_naming_a_file_that_was_NOT_signed_stays_open():
    """The half that stops this being a rubber stamp."""
    pid = _approved(["c.py"], "fix c")
    F._resolve_pending_for(["a.py", "b.py"], _Args())
    assert P.status_of(pid) == P.APPROVED


def test_a_card_only_partly_covered_stays_open():
    """Two files approved together, one signed: the decision has not been carried out."""
    pid = _approved(["a.py", "c.py"], "fix both")
    F._resolve_pending_for(["a.py"], _Args())
    assert P.status_of(pid) == P.APPROVED


def test_an_OPEN_card_is_never_closed_by_this():
    """Nobody has decided it. Closing one here would be this process answering for the
    operator -- which is worse than a card that lingers."""
    pid = P.add(["a.py"], "fix a")
    assert P.status_of(pid) == P.OPEN
    F._resolve_pending_for(["a.py"], _Args())
    assert P.status_of(pid) == P.OPEN


def test_signing_nothing_closes_nothing():
    """A no-op re-signing (`--force` with an unchanged tree) must not sweep the queue."""
    pid = _approved(["a.py"], "fix a")
    F._resolve_pending_for([], _Args())
    assert P.status_of(pid) == P.APPROVED


def test_two_cards_about_the_same_files_both_close():
    """Both were approved and both are satisfied by the same acceptance. Closing one and
    leaving the other would be the arbitrary choice the old key was trying to avoid."""
    a = _approved(["a.py"], "one reason")
    b = _approved(["a.py"], "a different reason")
    assert a != b
    F._resolve_pending_for(["a.py"], _Args())
    assert P.status_of(a) == P.DONE and P.status_of(b) == P.DONE


def test_it_never_raises_into_the_re_signing():
    """The re-signing is the act; the queue is bookkeeping. A broken queue must not turn a
    successful acceptance into a traceback."""
    class _Boom:
        APPROVED = P.APPROVED

        @staticmethod
        def items():
            raise RuntimeError("queue is on fire")

    import relay.selfimprove.pending as _real
    import sys
    sys.modules["relay.selfimprove.pending"] = _Boom
    try:
        F._resolve_pending_for(["a.py"], _Args())     # must not raise
    finally:
        sys.modules["relay.selfimprove.pending"] = _real
