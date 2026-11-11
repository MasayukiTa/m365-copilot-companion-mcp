# -*- coding: utf-8 -*-
"""The bridge refuses a pasted agent address that has no agent id with the one shared plain
sentence, before it opens a tab on it. The ids below are invented test fixtures, not captured
from any tenant.
"""
import pytest

from bridge import copilot_bridge as CB
from relay import template_seed as TS

G1 = "11111111-1111-4111-8111-111111111111"
G2 = "22222222-2222-4222-8222-222222222222"
AGENT_ID = "T_%s.%s" % (G1, G2)
M365 = "https://m365.cloud.microsoft"
COPILOT = "https://copilot.cloud.microsoft"


@pytest.mark.parametrize("url", [
    AGENT_ID,
    "%s/chat/agent/%s" % (M365, AGENT_ID),
    "%s/chat/agent/%s?titleId=T_%s" % (COPILOT, AGENT_ID, G1),
    "%s/agents/copilotstudio/%s" % (COPILOT, AGENT_ID),
    "",            # empty is left alone: startup falls back to any open agent tab
    "   ",
])
def test_a_usable_or_empty_address_is_allowed_through(url):
    assert CB.validate_impl_agent_url(url) is None


@pytest.mark.parametrize("bad", [
    "%s/chat/?titleId=T_%s&auth=2" % (M365, G1),   # generic Copilot chat page, no /agent/ id
    "%s/chat/agent/T_%s" % (M365, G1),             # one guid only, no .<agent>
    "%s/agents/copilotstudio/T_%s" % (COPILOT, G1),
    "T_not-a-guid.also-not",
])
def test_an_address_with_no_agent_id_exits_with_the_shared_sentence(bad):
    with pytest.raises(SystemExit) as exc:
        CB.validate_impl_agent_url(bad)
    # the bridge surface prints the SAME one plain sentence as configure_env, quickstart and doctor
    assert str(exc.value) == TS.NO_AGENT_ID_MESSAGE
    assert "no agent id" in str(exc.value)
    assert "Microsoft 365 Copilot" in str(exc.value)
    assert "/agent/" in str(exc.value)
    assert "two ids joined by a dot" in str(exc.value)
