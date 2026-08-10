"""Work IQ's consent is a CHAIN of cards, and the obvious stop condition does not work.

Two things were measured on 2026-08-10, and the second only showed up when the code was
run against a real page rather than asserted about.

  * Approving the connection surfaces SEVEN cards in sequence -- Work IQ User, Copilot,
    Teams, SharePoint, OneDrive, Mail, Calendar -- each appearing only once its
    predecessor is approved. Clicking the first Allow and returning left six pending.

  * "Keep clicking until no Allow button remains" does NOT terminate. An APPROVED card
    keeps its 許可/キャンセル buttons rendered in the transcript, so against a page holding
    two already-approved cards the loop burned all 12 rounds and 72 SECONDS, approved
    nothing, and still reported success.

What the chain actually does is GROW: the visible-Allow count went 2 -> 3 -> 4 -> 5 -> 6
-> 7 and then stayed at 7. Growth is the signal that a click landed on a pending card;
no growth means there is nothing left. Same page, same two stale cards, after the fix:
5.8 seconds instead of 72.2.

Note also that the chain is per browser PROFILE, not per conversation -- verified by
opening a brand-new conversation afterwards and getting no cards at all. It is an
onboarding cost for a fresh profile, not a per-run cost.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RECONNECT = (REPO / "relay" / "edge_reconnect.py").read_text(encoding="utf-8")
BRIDGE = (REPO / "bridge" / "copilot_bridge.py").read_text(encoding="utf-8")
FLEET = (REPO / "relay" / "relay_fleet.py").read_text(encoding="utf-8")


def _chain() -> str:
    i = RECONNECT.index("def _click_consent_chain(page) -> bool:")
    return RECONNECT[i:i + 2600]


def test_the_chain_clicker_exists_in_exactly_one_place():
    assert "def _click_consent_chain(page) -> bool:" in RECONNECT
    for name, src in (("bridge", BRIDGE), ("fleet", FLEET)):
        assert "_click_consent_chain" in src, "%s が共有実装を使っていない" % name


def test_it_stops_when_the_chain_stops_growing():
    """承認済みカードもボタンを残すので「無くなるまで」では止まらない。"""
    c = _chain()
    assert "after <= before" in c, "増加を停止条件にしていない"
    assert "break" in c.split("after <= before")[1][:80]


def test_it_is_still_bounded():
    c = _chain()
    assert "for _ in range(CONSENT_CHAIN_MAX):" in c
    assert "while True" not in c


def test_it_clicks_the_newest_card():
    c = _chain()
    assert ".last.click()" in c, "先頭（承認済み）を押している"
    assert ".first.click()" not in c


def test_it_only_counts_visible_buttons():
    assert 'locator("visible=true")' in RECONNECT


def test_it_reports_success_only_when_it_clicked():
    c = _chain()
    assert "clicked = False" in c and "return clicked" in c


def test_the_cap_is_a_named_env_overridable_constant():
    assert re.search(r'CONSENT_CHAIN_MAX = int\(os\.environ\.get\("MCP_CONSENT_CHAIN_MAX"',
                     RECONNECT)
