"""A finished turn must settle even when the block flickers through a placeholder.

Measured live on 2026-08-10: with the answer complete and the Stop button absent, the
last assistant block cycled answer -> "処理中です。" -> name-only -> answer about every
four seconds. _is_processing() is True for the middle two (an empty read counts as
processing), and each one CLEARED the stability counters -- so a turn that has already
finished keeps being told it has not.

Correction: this was first written up as the cause of an apparent 15-minute hang in the
interactive chat. It was not. The bridge answers in ~28s; the "hang" was a test client
reading the SSE stream to EOF on a keep-alive connection, so it never saw `event: done`.
The oscillation is real and was observed directly; that is what this change rests on.

A placeholder still must never be accepted AS the answer, and a real mid-stream partial
must still be rejected. Both are asserted here alongside the fix.
"""
from pathlib import Path

from relay import settle as S
from relay.copilot_autopilot_relay import _is_processing

SRC = Path(__file__).with_name("copilot_autopilot_relay.py").read_text(encoding="utf-8")


def test_an_empty_read_still_counts_as_processing():
    """前提。空の読み取りは「まだ生成中」として扱われる（この性質自体は変えていない）。"""
    assert _is_processing("") is True
    assert _is_processing("   ") is True


def test_the_placeholder_text_counts_as_processing():
    assert _is_processing("処理中です。") is True
    assert _is_processing("12") is False


# ---- 以下は振る舞いで固定する ------------------------------------------------------------------
#
# 元はループのソース文字列を切り出して `"if generating:"` の位置などを検査していた。
# 判定が `relay.settle` に移ったので、同じ4性質をソースではなく挙動で固定する --
# コメントで満たせないアサーションのほうが強い。ソース検査で残すのは1つだけ、
# 「正典サイトが本当に委譲しているか」で、それが無いと以下の挙動テストは
# 別モジュールの話になってしまう。


def test_the_canonical_site_delegates_to_the_one_rule():
    """これが落ちたら、下の挙動テストは relay の挙動を測っていない。"""
    assert "_settle.settle_step(" in SRC
    assert "state = _settle.SettleState()" in SRC


def _step(state, text, **kw):
    kw.setdefault("now", 0.0)
    kw.setdefault("dwell_s", 1.0)
    kw.setdefault("generating", False)
    kw.setdefault("is_processing", _is_processing(text))
    kw.setdefault("has_marker", True)
    kw.setdefault("samples", 2)
    return S.settle_step(state, text, **kw)


def test_a_placeholder_no_longer_clears_stability_once_generation_stopped():
    """生成が終わっているなら、プレースホルダは蓄積した安定を壊さない。"""
    state, _ = _step(S.SettleState(), "回答 DONE")
    before = state.stable_count
    state, outcome = _step(state, "処理中です。")
    assert outcome == S.SKIP
    assert state.stable_count == before and state.last == "回答 DONE"


def test_generation_in_progress_still_resets():
    """PRIMARY ゲートは不変。生成中は必ずカウンタを捨てる。"""
    state = S.SettleState(last="回答 DONE", stable_count=9, stable_since=0.0)
    state, outcome = _step(state, "処理中です。", generating=True)
    assert outcome == S.RESET and state == S.SettleState()


def test_a_placeholder_can_never_become_the_accepted_answer():
    """プレースホルダを最終回答として採用しないこと（本来の目的は維持）。"""
    state = S.SettleState()
    for _ in range(20):
        state, outcome = _step(state, "処理中です。", now=999.0, dwell_s=0.0)
        assert outcome == S.SKIP
    assert state.last is None, "処理中の文字列が last に入っている"


def test_markerless_answers_still_need_the_longer_settle():
    """マーカー無しの安定は依然として倍待つこと。"""
    assert S.requirements(dwell_s=3.0, has_marker=True, samples=3) == (3, 3.0)
    assert S.requirements(dwell_s=3.0, has_marker=False, samples=3) == (6, 6.0)
