# -*- coding: utf-8 -*-
"""tools/trace_view/origin_attribution: tell apart Claude vs the Copilot agent as the origin
of MCP traffic, from the user-agent already stored in mcp_inbound.jsonl. No real log is read;
the rows here are synthetic and mirror the four agents measured in the live log.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.trace_view.origin_attribution import (  # noqa: E402
    CLAUDE, COPILOT, CODEX, OTHER, UNKNOWN,
    attribute_by_origin, classify_origin, dominant_origin,
)

# The exact user-agent strings seen in the real mcp_inbound.jsonl (measured 2026-10-27).
UA_CLAUDE = "claude-code/2.1.308 (claude-desktop, agent-sdk/0.3.308)"
UA_HTTPX = "python-httpx2/2.13.1"
UA_URLLIB = "Python-urllib/3.12"
UA_CODEX = "codex-mcp-client/0.164.0"


def test_the_claude_desktop_agent_is_attributed_to_claude():
    assert classify_origin(UA_CLAUDE) == CLAUDE


def test_the_relay_http_clients_are_attributed_to_the_copilot_agent():
    # The connector-less relay reaches the server as httpx / urllib; that path IS the copilot
    # agent for this question. Both real strings must land on copilot, not on "other".
    assert classify_origin(UA_HTTPX) == COPILOT
    assert classify_origin(UA_URLLIB) == COPILOT


def test_codex_is_its_own_origin_not_folded_into_copilot_or_claude():
    assert classify_origin(UA_CODEX) == CODEX


def test_an_empty_user_agent_is_unknown_not_other():
    # A missing ua means the row carried none, which is a different fact from "some other
    # client"; conflating them would answer the attribution question wrongly.
    assert classify_origin("") == UNKNOWN
    assert classify_origin(None) == UNKNOWN


def test_an_unrecognised_but_present_user_agent_is_other():
    assert classify_origin("some-unknown-client/9.9") == OTHER


def test_classify_is_case_insensitive():
    assert classify_origin("CLAUDE-CODE/2.1.308 (CLAUDE-DESKTOP)") == CLAUDE
    assert classify_origin("PYTHON-HTTPX/1.0") == COPILOT


def _inbound(ua, session, method="tools/call", client_ip="127.0.0.1"):
    return {"ua": ua, "session": session, "method": method, "client_ip": client_ip}


def test_attribute_counts_rows_sessions_and_methods_per_origin():
    rows = [
        _inbound(UA_CLAUDE, "s-claude-1", "initialize"),
        _inbound(UA_CLAUDE, "s-claude-1", "tools/call"),
        _inbound(UA_CLAUDE, "s-claude-2", "tools/call"),
        _inbound(UA_HTTPX, "s-cop-1", "tools/call"),
        _inbound(UA_URLLIB, "s-cop-2", "ping"),
        _inbound(UA_CODEX, "s-codex-1", "tools/list"),
    ]
    summary = attribute_by_origin(rows)
    assert summary["total"] == 6
    claude = summary["origins"][CLAUDE]
    assert claude["rows"] == 3
    assert claude["sessions"] == 2            # s-claude-1 and s-claude-2
    assert claude["methods"] == {"initialize": 1, "tools/call": 2}
    copilot = summary["origins"][COPILOT]
    assert copilot["rows"] == 2               # httpx + urllib both copilot
    assert copilot["sessions"] == 2
    assert summary["origins"][CODEX]["rows"] == 1


def test_distinct_client_ips_are_counted_not_listed():
    rows = [
        _inbound(UA_CLAUDE, "s1", client_ip="10.0.0.1"),
        _inbound(UA_CLAUDE, "s2", client_ip="10.0.0.2"),
        _inbound(UA_CLAUDE, "s3", client_ip="10.0.0.1"),
    ]
    summary = attribute_by_origin(rows)
    assert summary["origins"][CLAUDE]["client_ips"] == 2


def test_the_summary_never_carries_a_raw_user_agent_session_or_ip():
    # This is the privacy contract: a shared origin report must not become a second place a
    # user-agent, session id or client IP string lives.
    rows = [
        _inbound(UA_CLAUDE, "secret-session-abc", client_ip="203.0.113.7"),
        _inbound(UA_HTTPX, "secret-session-def", client_ip="203.0.113.9"),
    ]
    blob = repr(attribute_by_origin(rows))
    assert UA_CLAUDE not in blob
    assert UA_HTTPX not in blob
    assert "secret-session-abc" not in blob
    assert "203.0.113.7" not in blob


def test_order_is_stable_and_only_lists_present_origins():
    rows = [_inbound(UA_HTTPX, "s1"), _inbound(UA_CLAUDE, "s2")]
    summary = attribute_by_origin(rows)
    # claude before copilot in the canonical order, regardless of arrival order.
    assert summary["order"] == [CLAUDE, COPILOT]


def test_dominant_origin_answers_whose_it_was_mostly():
    rows = [_inbound(UA_HTTPX, "c1"), _inbound(UA_HTTPX, "c2"), _inbound(UA_CLAUDE, "a1")]
    assert dominant_origin(attribute_by_origin(rows)) == COPILOT


def test_dominant_origin_is_unknown_when_there_is_nothing_to_attribute():
    assert dominant_origin(attribute_by_origin([])) == UNKNOWN


def test_a_tie_breaks_towards_claude_before_copilot():
    rows = [_inbound(UA_CLAUDE, "a1"), _inbound(UA_HTTPX, "c1")]
    assert dominant_origin(attribute_by_origin(rows)) == CLAUDE


def test_bad_input_never_raises():
    assert attribute_by_origin(None) == {"total": 0, "origins": {}, "order": []}
    assert attribute_by_origin(["not-a-dict", 123, None]) == {
        "total": 0, "origins": {}, "order": []}
