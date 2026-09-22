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
    # THE REAL AUTHORITY LEDGER IS NOT A FIXTURE. `_resolve_pending_for` reads it to add up
    # the files accepted since each card was queued, and a test that let it reach the live
    # file would pass or fail on this machine's re-signing history. Empty by default means
    # "no re-signing but the one being simulated"; the tests that need history pass their
    # own rows in.
    from relay.selfimprove import authority_ledger as _led
    monkeypatch.setattr(_led, "read", lambda *a, **k: [])


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


# ─────────────────────────────────────────── 分割払いで実施された決定（実測で見つかった穴）

#: 署名直後のベースライン。この中のファイルは定義上すべてベースラインと一致している。
def _ledger(*acts):
    """REBLESS rows in the shape `authority_ledger.append` writes them.

    Each act is (ts, files-it-accepted). Only `event`, `ts` and the KEYS of `changed` are
    read, so the values are left empty rather than faked into looking like checksums.
    """
    return [{"event": "rebless", "ts": float(ts), "changed": {f: {} for f in files}}
            for ts, files in acts]


def _last_authorization(pid):
    """閉じたカードの承認文言。**`items()` は使えない** — 生きている行しか返さないので、
    DONE にした瞬間に消える。前にこれを `items()[0]` で読もうとして IndexError を
    「閉じなかった」と読み違えている。キューは追記専用なので、素直に全行読む。"""
    import io
    import json

    rows = []
    with io.open(P.QUEUE_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    mine = [r for r in rows if r.get("id") == pid]
    assert mine, "no row at all for %s" % pid
    return str(mine[-1].get("authorization") or "")


def test_a_decision_carried_out_across_TWO_re_signings_closes():
    """**本番で10日間開いたままだったカードがこれ。** 2026-09-19 実測: 09-09 に承認された
    `bench/swe_grade_swebench.py` + `tools/security.py` のカードが「エージェントの実行待ち」
    のまま。両方ともベースラインと一致していて、ドリフトはとっくに消えている — ただし
    再署名が2回に分かれたので、どちらの回も両方の名前を含まなかった。

    最初の修正（`files <= signed`）はこれを閉じられない。**虚報を終わらせるために入れた
    仕組みが、1段深いところで同じ虚報を出していた。**"""
    import time

    t0 = time.time()
    pid = _approved(["a.py", "c.py"], "both drifted")
    # c.py はこのカードが積まれた後の別の再署名で受理済み。今回の署名は a.py だけ。
    F._resolve_pending_for(["a.py"], _Args(), ledger_rows=_ledger((t0 + 1, ["c.py"])))
    assert P.status_of(pid) == P.DONE


def test_a_re_signing_from_BEFORE_the_card_does_not_count():
    """分割払いは**カードが積まれて以降**の支払いだけ。カードより前の再署名を数えると、
    まだ答えられていない問いを、それより前の行為で閉じることになる。"""
    import time

    t0 = time.time()
    pid = _approved(["a.py", "c.py"], "both drifted")
    F._resolve_pending_for(["a.py"], _Args(), ledger_rows=_ledger((t0 - 600, ["c.py"])))
    assert P.status_of(pid) == P.APPROVED


def test_an_unrelated_re_signing_does_not_close_a_card():
    """**最初の修正がここで壊れていた。** ベースラインの現在値を条件にすると、
    `snapshot_baseline` が**全ファイルを現在の内容で書き直す**ので、再署名の直後は
    定義上どのファイルもドリフトしていない。条件が空振りし、**無関係な再署名1回で
    承認済みカードが全部閉じた**。受理された履歴を足す形はこれを起こせない。"""
    import time

    t0 = time.time()
    pid = _approved(["c.py"], "c drifted and nobody has fixed it")
    F._resolve_pending_for(["d.py"], _Args(), ledger_rows=_ledger((t0 + 1, ["d.py"])))
    assert P.status_of(pid) == P.APPROVED, \
        "an act that never accepted c.py closed a card about c.py"


def test_a_card_this_act_did_not_touch_closes_as_SUPERSEDED_not_as_carried_out():
    """**2つの真な文を、1つの都合のいい文にまとめない。**

    `c.py` はもうベースラインと一致していて、このカードの言うドリフトは存在しない。
    だが実施したのはこの再署名ではない。「carried out: the re-signing succeeded」と
    書けば台帳に起きていないことが入る。かといって開いたまま残せば、それがまさに
    この仕組みが終わらせるために入った虚報（実測: 09-09 から10日間「待機中」）。

    だから閉じる。**ただし別の文言で。**"""
    import time

    t0 = time.time()
    pid = _approved(["c.py"], "about c only")
    F._resolve_pending_for(["d.py"], _Args(), ledger_rows=_ledger((t0 + 1, ["c.py"])))
    assert P.status_of(pid) == P.DONE
    note = _last_authorization(pid)
    assert "superseded" in note and "carried out" not in note


def test_a_card_this_act_DID_carry_out_says_so():
    pid = _approved(["a.py"], "about a")
    F._resolve_pending_for(["a.py"], _Args())
    assert "carried out" in _last_authorization(pid)


def test_a_card_naming_something_never_accepted_is_never_swept():
    """満たされていないカードは、どちらの文言でも閉じない。"""
    import time

    t0 = time.time()
    pid = _approved(["z.py"], "z never settled")
    F._resolve_pending_for(["d.py"], _Args(), ledger_rows=_ledger((t0 + 1, ["d.py"])))
    assert P.status_of(pid) == P.APPROVED


def test_without_a_readable_ledger_it_falls_back_to_the_narrower_rule():
    """台帳が読めない呼び出しには、広い規則ではなく狭い規則を渡す。分割払いは数えられ
    なくなるが、それは「閉じ損ねる」側の誤りで、起きていない実施を記録する側ではない。"""
    pid = _approved(["a.py", "c.py"], "both")
    F._resolve_pending_for(["a.py"], _Args(), ledger_rows=[])
    assert P.status_of(pid) == P.APPROVED
    F._resolve_pending_for(["a.py", "c.py"], _Args(), ledger_rows=[])
    assert P.status_of(pid) == P.DONE


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
