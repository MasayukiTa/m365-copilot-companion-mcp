"""A finished turn must settle even when the block flickers through a placeholder.

Measured live on 2026-08-10: with the answer complete and the Stop button absent, the
last assistant block cycled answer -> "処理中です。" -> name-only -> answer about every
four seconds. _is_processing() is True for the middle two (an empty read counts as
processing), and each one CLEARED the stability counters, so the turn never settled and
/stream ran to wait_for_idle's 1800s deadline. Three measurements -- 954s, 949s and >328s
-- all with the correct answer already on screen.

A placeholder still must never be accepted AS the answer, and a real mid-stream partial
must still be rejected. Both are asserted here alongside the fix.
"""
import re
from pathlib import Path

from relay.copilot_autopilot_relay import _is_processing

SRC = Path(__file__).with_name("copilot_autopilot_relay.py").read_text(encoding="utf-8")


def _settle_loop() -> str:
    i = SRC.index("        last, stable_count, stable_since = None, 0, None\n        while time.time() < deadline:")
    return SRC[i:i + 4000]


def test_an_empty_read_still_counts_as_processing():
    """前提。空の読み取りは「まだ生成中」として扱われる（この性質自体は変えていない）。"""
    assert _is_processing("") is True
    assert _is_processing("   ") is True


def test_the_placeholder_text_counts_as_processing():
    assert _is_processing("処理中です。") is True
    assert _is_processing("12") is False


def test_a_placeholder_no_longer_clears_stability_once_generation_stopped():
    loop = _settle_loop()
    i = loop.index("if _is_processing(t):")
    j = loop.index("elif t == last:", i)
    branch = loop[i:j]
    assert "if generating:" in branch, "生成終了後もカウンタを消している"
    reset = "last, stable_count, stable_since = None, 0, None"
    assert branch.count(reset) == 1, "リセットが無条件に残っている"
    assert branch.index("if generating:") < branch.index(reset), "リセットが条件の外にある"


def test_generation_in_progress_still_resets():
    """PRIMARY ゲートは不変。生成中は必ずカウンタを捨てる。"""
    loop = _settle_loop()
    i = loop.index("if generating:")
    assert "last, stable_count, stable_since = None, 0, None" in loop[i:i + 200]


def test_a_placeholder_can_never_become_the_accepted_answer():
    """プレースホルダを最終回答として採用しないこと（本来の目的は維持）。"""
    loop = _settle_loop()
    branch = loop[loop.index("if _is_processing(t):"):loop.index("elif t == last:")]
    assert "_accept_new_reply" not in branch
    assert re.search(r"\blast\s*=\s*t\b", branch) is None, "処理中の文字列を last に入れている"


def test_markerless_answers_still_need_the_longer_settle():
    """マーカー無しの安定は依然として倍待つこと。"""
    loop = _settle_loop()
    assert "REPLY_SETTLE_SAMPLES * 2" in loop
    assert "dwell_s * 2.0" in loop
