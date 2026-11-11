# -*- coding: utf-8 -*-
"""Every address form the browser shows for one agent resolves to the same agent id, and an
address that carries no id is refused with one plain sentence that says what to paste.

The ids below are invented (test fixtures), not captured from any tenant.
"""
import pytest

from relay import template_seed as TS

# Two invented guids. The agent id is `T_<guid>.<guid>`; the second guid is the agent.
G1 = "11111111-1111-4111-8111-111111111111"
G2 = "22222222-2222-4222-8222-222222222222"
AGENT_ID = "T_%s.%s" % (G1, G2)

# The two product hosts the address can be on.
M365 = "https://m365.cloud.microsoft"
COPILOT = "https://copilot.cloud.microsoft"


def _forms_that_resolve():
    """Every address shape that names this one agent, across both hosts and with/without a query."""
    for host in (M365, COPILOT):
        # /chat/agent/T_<g>.<g>
        yield "%s/chat/agent/%s" % (host, AGENT_ID)
        yield "%s/chat/agent/%s?titleId=T_%s" % (host, AGENT_ID, G1)
        yield "%s/chat/agent/%s?foo=bar&baz=1" % (host, AGENT_ID)
        # /agents/copilotstudio/T_<g>.<g> (the shape the browser redirects an agent address to)
        yield "%s/agents/copilotstudio/%s" % (host, AGENT_ID)
        yield "%s/agents/copilotstudio/%s?titleId=T_%s" % (host, AGENT_ID, G1)
        yield "%s/agents/copilotstudio/%s?tenantId=00000000-0000-0000-0000-000000000001" % (host, AGENT_ID)


@pytest.mark.parametrize("url", list(_forms_that_resolve()))
def test_every_form_resolves_to_the_same_agent_id(url):
    assert TS.agent_id_from_url(url) == AGENT_ID


def test_the_bare_id_is_accepted_as_itself():
    assert TS.agent_id_from_url(AGENT_ID) == AGENT_ID


def test_a_trailing_path_after_the_id_still_resolves():
    # a conversation segment appended to the agent base url must not hide the id
    url = "%s/chat/agent/%s/conversation/%s" % (M365, AGENT_ID, G1)
    assert TS.agent_id_from_url(url) == AGENT_ID


# ---- negative cases: an address with no usable id is refused, not guessed ----

GENERIC_CHAT = "%s/chat/?titleId=T_%s&auth=2" % (M365, G1)   # the generic Copilot chat page
SINGLE_GUID = "%s/chat/agent/T_%s" % (M365, G1)              # titleId only, one guid, no .<agent>
SINGLE_GUID_STUDIO = "%s/agents/copilotstudio/T_%s" % (COPILOT, G1)


@pytest.mark.parametrize("bad", [
    "",
    None,
    GENERIC_CHAT,
    SINGLE_GUID,
    SINGLE_GUID_STUDIO,
    "%s/chat" % M365,
    "T_not-a-guid.also-not",
    "%s/chat/agent/P_%s.%s" % (M365, G1, G2),   # a P_ address is not a T_ agent id
])
def test_an_address_with_no_t_agent_id_is_refused(bad):
    with pytest.raises(TS.SeedError):
        TS.agent_id_from_url(bad)


def test_the_refusal_sentence_says_what_to_paste_in_plain_words():
    with pytest.raises(TS.SeedError) as exc:
        TS.agent_id_from_url(GENERIC_CHAT)
    msg = str(exc.value)
    # names the problem and the exact fix, with no jargon token like T_<guid>.<guid>
    assert "no agent id" in msg
    assert "Microsoft 365 Copilot" in msg
    assert "/agent/" in msg
    assert "two ids joined by a dot" in msg
    assert "<guid>" not in msg and "T_<" not in msg
    # it is the one shared sentence
    assert msg == TS.NO_AGENT_ID_MESSAGE
