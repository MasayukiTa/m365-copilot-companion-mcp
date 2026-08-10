"""Work IQ's consent is a CHAIN of cards. One click is not enough.

Measured 2026-08-10 on a fresh conversation: approving the connection surfaced seven cards
in sequence -- Work IQ User, Copilot, Teams, SharePoint, OneDrive, Mail, Calendar -- each
appearing only once its predecessor was approved. _bridge_auto_consent's tier 0 clicked the
first Allow and returned, so the rest stayed pending.

The consequence is a failed consent, and an unresolved card left sitting where later reads
expect an answer.

Correction, because the original version of this file claimed more: this was first written
up as the cause of an apparent 15-minute hang in the interactive chat. It was not. The
bridge answers in ~28s; the "hang" was a test client reading the SSE stream to EOF on a
keep-alive connection, so it never saw `event: done`. The seven-card chain below was
observed directly and is worth fixing on its own.
"""
import re
from pathlib import Path

SRC = Path(__file__).with_name("copilot_bridge.py").read_text(encoding="utf-8")


def _tier0() -> str:
    i = SRC.index("def _bridge_auto_consent() -> bool:")
    j = SRC.index("# Tier 1:", i)
    return SRC[i:j]


def test_the_chain_cap_is_a_named_constant_not_a_bare_number():
    assert "_CONSENT_CHAIN_MAX" in SRC
    assert re.search(r"_CONSENT_CHAIN_MAX = int\(os\.environ\.get\(", SRC)


def test_tier0_keeps_clicking_until_no_card_remains():
    t0 = _tier0()
    assert "for _ in range(_CONSENT_CHAIN_MAX):" in t0, "1枚で打ち切っている"
    # 1回クリックして即 return する形に戻っていないこと
    assert not re.search(r"btn\.first\.click\(\)\s*\n\s*pg\.wait_for_timeout\(\d+\)\s*\n\s*return True", t0)


def test_tier0_clicks_the_newest_card_not_the_oldest():
    """カードは履歴に積み上がる。承認待ちは常に一番下。"""
    t0 = _tier0()
    assert "btn.last.click()" in t0, "先頭のカード（承認済み）を押し続けている"
    assert "btn.first.click()" not in t0


def test_tier0_only_considers_visible_buttons():
    t0 = _tier0()
    assert 'locator("visible=true")' in t0


def test_tier0_is_bounded():
    """解決せず再描画し続けるカードでも無限ループしないこと。"""
    t0 = _tier0()
    assert "while True" not in t0
    assert "if not hit:" in t0 and "break" in t0


def test_tier0_reports_success_only_when_it_actually_clicked():
    t0 = _tier0()
    assert "clicked_any" in t0
    assert "if clicked_any:" in t0
