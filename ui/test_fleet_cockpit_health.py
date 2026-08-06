"""Source-level regression checks for FleetCockpit's sign-in health classifier."""
from pathlib import Path


SOURCE = (Path(__file__).with_name("FleetCockpit.cs")).read_text(encoding="utf-8")


def test_stale_login_tab_does_not_override_a_live_m365_chat_tab():
    assert "bool hasUsableM365Chat = false;" in SOURCE
    assert "bool needsSignin = onLoginWall && !hasUsableM365Chat;" in SOURCE
    assert "LooksLikeUsableM365Chat" in SOURCE


def test_usable_chat_classifier_rejects_login_walls():
    body = SOURCE[SOURCE.index("static bool LooksLikeUsableM365Chat"):]
    body = body[:body.index("\n    }")]
    assert "LooksLikeLoginWall(url)" in body
    assert 'u.Contains("/chat")' in body


def test_live_agent_is_green_before_first_reply_and_red_without_chat():
    assert "else if (!hasUsableM365Chat)" in SOURCE
    assert 'SetDot(4, HealthState.Red, T("hs_agent_bad"), now);' in SOURCE
    assert 'SetDot(4, HealthState.Green, T("hs_agent_ok"), now);          // run live, first reply pending' in SOURCE
