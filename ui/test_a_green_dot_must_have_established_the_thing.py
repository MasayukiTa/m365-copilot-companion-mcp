# -*- coding: utf-8 -*-
"""Three of the six dots could show green without having established what they claimed.

An audit of the strip on 2026-09-16 found, ranked by how badly each misleads:

  DOT 4 (agent) called a cached request template a live binding with no maximum age, while
  relay/profile_token.py -- the module that WRITES that file -- refuses to return one older
  than TEMPLATE_MAX_AGE_S, and refuses one whose gpt_id is empty. A template 211.7 hours old
  was on this disk at the time, 8.8 days against a 24 hour cap. The dot would have reported a
  binding the route itself evicts on first use, and gone on reporting it, because eviction
  only happens when a capture runs.

  DOT 2 (edge) called any occurrence of "m365.cloud.microsoft" anywhere in the /json/list
  body "on the agent". That is satisfied by a tab on the DEFAULT Copilot -- the fallback this
  same file renders as a warning badge -- and by a title, a description or a favicon URL of
  any target, since nothing filters to pages. It is the tunnel dot's documented bug repeated:
  reaching A thing is not reaching THE thing.

  DOT 5 (tool) read _lastHealthBody, which was written on every successful poll and never
  aged. Once /health began failing, the last-known fleet_tool_ok kept softening the dot from
  red to amber for the lifetime of the process.

These are source assertions, which do not catch a runtime defect on their own -- they catch
the specific regression of these rules being deleted, which is what happened to the tab
sniffing rule this directory already has a test for.
"""
from __future__ import annotations

import io
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
COCKPIT = os.path.join(HERE, "FleetCockpit.cs")
PROFILE_TOKEN = os.path.join(REPO, "relay", "profile_token.py")


@pytest.fixture(scope="module")
def src():
    return io.open(COCKPIT, encoding="utf-8-sig").read()


def test_the_agent_dot_ages_the_template_it_calls_a_binding(src):
    assert "TEMPLATE_MAX_AGE_S" in src
    assert "ageS > TEMPLATE_MAX_AGE_S" in src, "the age is computed but not acted on"
    # And existence alone must no longer be the answer.
    assert 'IndexOf("gptId", StringComparison.Ordinal) >= 0' not in src, \
        "the substring test is back: it asks about the file's spelling, not about an agent"
    assert 'StringField(query, "gptId")' in src


def test_the_cockpits_age_limit_is_not_looser_than_the_routes():
    """A COPY, AND COPIES DRIFT. If the cockpit's cap grows past the route's, the dot goes
    back to reporting bindings the route refuses -- silently, because nothing else compares
    the two numbers."""
    cs = io.open(COCKPIT, encoding="utf-8-sig").read()
    py = io.open(PROFILE_TOKEN, encoding="utf-8").read()

    m_cs = re.search(r"const double TEMPLATE_MAX_AGE_S\s*=\s*([0-9]+)\s*\*\s*([0-9]+)\s*;", cs)
    assert m_cs, "the cockpit no longer declares TEMPLATE_MAX_AGE_S in the expected form"
    cockpit_s = int(m_cs.group(1)) * int(m_cs.group(2))

    m_py = re.search(r"TEMPLATE_MAX_AGE_S\s*=\s*float\(os\.environ\.get\([^,]+,\s*str\("
                     r"([0-9]+)\s*\*\s*([0-9]+)\)\)\)", py)
    assert m_py, "relay/profile_token.py no longer declares TEMPLATE_MAX_AGE_S in the "\
                 "expected form; the two constants can no longer be compared"
    route_s = int(m_py.group(1)) * int(m_py.group(2))

    assert cockpit_s <= route_s, (
        "the cockpit would call a template live that the route would evict "
        "(cockpit %ds > route %ds)" % (cockpit_s, route_s))


def test_the_edge_dot_asks_for_the_configured_agent_not_the_domain(src):
    assert "_agentMarkerId" in src
    # The marker check must be part of the green condition, not merely present in the file.
    m = re.search(r"bool onAgent = onDomain[\s\S]{0,400}?;", src)
    assert m, "onAgent is no longer derived from onDomain plus a marker check"
    assert "_agentMarkerId" in m.group(0), "the domain alone decides green again"


def test_the_tool_dot_stops_believing_a_health_body_that_stopped_arriving(src):
    assert "_lastHealthBodyAt" in src
    assert "HEALTH_BODY_MAX_AGE_S" in src
    m = re.search(r"string fleetTool = [\s\S]{0,400}?;", src)
    assert m, "the fleet_tool_ok read no longer looks the way this test can check"
    assert "HEALTH_BODY_MAX_AGE_S" in m.group(0), \
        "fleet_tool_ok is read straight from the cached body again"


def test_the_signin_window_is_taken_from_the_record_not_from_a_constant(src):
    """A flat 1800s against a ~3,848s token made the dot amber through half of every healthy
    cycle. One token lifetime is one capture cycle, which is the question being asked."""
    assert "double tokenLifeS = (expiresAt > 0 && capTs > 0) ? (expiresAt - capTs) : 0;" in src
    assert "capAgeS <= freshWindowS" in src
    # The flat constant survives as the fallback for a FAILED capture, which has no expiry.
    assert "SIGNIN_EVIDENCE_MAX_AGE_S" in src


def test_evidence_that_is_expected_and_missing_is_amber_on_both_dots(src):
    """The file states this rule for the sign-in dot and broke it for the agent dot, which
    went straight to red -- and red there drives a reconnect."""
    m = re.search(r"SetDot\(4, live \? HealthState\.(\w+)", src)
    assert m, "the no-capture branch for dot 4 no longer looks the way this test can check"
    assert m.group(1) == "Yellow", "absent evidence is red again on the agent dot"


def test_an_unreadable_route_record_is_not_an_open_route(src):
    """RouteIsClosed's own comment named this silent zero and then returned false on every
    error path -- the answer that skips the amber branch and asks the binding check a
    question nobody had established was the right one."""
    assert "ROUTE_UNKNOWN" in src and "ROUTE_OPEN" in src and "ROUTE_CLOSED" in src
    body = src[src.index("int RouteState()"):]
    body = body[:body.index("\n    DateTime RunStartedLocal()")]
    # A file that does not exist IS an answer: nothing has ever closed the route here.
    assert "if (!File.Exists(path)) return ROUTE_OPEN;" in body
    # Everything else that goes wrong is not.
    assert "return ROUTE_UNKNOWN;" in body
    assert "return false;" not in body, "an error path answers 'open' again"
    # And route lines none of the parse could read are the case the comment warned about.
    assert "routeLines > 0 && parsed == 0" in body


@pytest.mark.parametrize("key", [
    "hs_agent_unknown_live", "hs_edge_detail_norun",
    "hs_signin_gray_old", "hs_signin_gray_expired", "hs_agent_route_unknown",
])
def test_every_new_message_exists_in_both_languages(key, src):
    """A T() key with no definition renders as the key, which is worse than the wrong
    sentence it replaced."""
    m = re.search(r'if \(k == "%s"\) return ja \? "([^"]+)" : "([^"]+)";' % key, src)
    assert m, "%s is used but never defined" % key
    assert m.group(1).strip() and m.group(2).strip()


def test_no_two_of_the_reworded_messages_say_the_same_thing(src):
    """They were split BECAUSE one sentence was covering several situations."""
    keys = ["hs_signin_gray", "hs_signin_gray_old", "hs_signin_gray_expired",
            "hs_edge_detail_notabs", "hs_edge_detail_norun"]
    seen = {}
    for key in keys:
        m = re.search(r'if \(k == "%s"\) return ja \? "([^"]+)" : "([^"]+)";' % key, src)
        assert m, key
        for text in m.groups():
            assert text not in seen, "%s and %s carry the same sentence" % (key, seen[text])
            seen[text] = key
