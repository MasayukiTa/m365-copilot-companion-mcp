"""Unit tests for the page-level verdict logic and the URL redaction. No browser is started:
page_verdict and strip_url are pure functions."""

from __future__ import annotations

import json

import pytest

from tools import provider_page_probe as pp


def test_cdp_unreachable_wins_over_everything():
    v = pp.page_verdict(False, "signin.example", "Title", [])
    assert v == "cdp_unreachable"


def test_redirect_to_signin_host_is_signed_out():
    v = pp.page_verdict(True, "login.microsoftonline.com", "Sign in", ["x.example"])
    assert v == "signed_out"


def test_complete_page_has_title_and_nothing_pending():
    assert pp.page_verdict(True, "app.example", "Agent", []) == "page_complete"


def test_pending_requests_make_the_page_stalled():
    assert pp.page_verdict(True, "app.example", "Agent", ["slow.example"]) == "page_stalled"


def test_empty_title_without_pending_is_not_complete():
    assert pp.page_verdict(True, "app.example", "", []) == "page_stalled"


def test_whitespace_title_counts_as_empty():
    assert pp.page_verdict(True, "app.example", "   ", []) == "page_stalled"


def test_signin_host_check_uses_the_given_set():
    custom = frozenset({"sso.example"})
    assert pp.page_verdict(True, "sso.example", "x", [], signin_hosts=custom) == "signed_out"
    assert pp.page_verdict(True, "login.microsoftonline.com", "x", [], signin_hosts=custom) == "page_complete"


def test_exit_codes_are_distinct_per_verdict():
    codes = [pp.EXIT_CODES[v] for v in ("page_complete", "signed_out", "page_stalled", "cdp_unreachable")]
    assert len(set(codes)) == 4
    assert pp.EXIT_CODES["page_complete"] == 0


def test_strip_url_drops_query_and_fragment():
    host, path = pp.strip_url("https://api.example/v1/items?token=SECRET&x=1#frag")
    assert host == "api.example"
    assert path == "/v1/items"
    assert "SECRET" not in host + path


def test_pending_summary_lists_hosts_without_paths_or_duplicates():
    pending = {1: ("a.example", "/p"), 2: ("a.example", "/q"), 3: ("b.example", "/r")}
    assert pp.pending_summary(pending) == ["a.example", "b.example"]


def test_append_evidence_writes_one_json_line(tmp_path):
    target = tmp_path / ".fleet" / "provider_health.jsonl"
    pp.append_evidence({"verdict": "page_complete", "kind": "page_probe"}, target)
    pp.append_evidence({"verdict": "signed_out", "kind": "page_probe"}, target)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["verdict"] == "page_complete"


def test_main_without_agent_url_exits_with_stalled_code(monkeypatch, capsys):
    monkeypatch.delenv("MCP_CAPTURE_WORKER_AGENT", raising=False)
    code = pp.main([])
    assert code == pp.EXIT_CODES["page_stalled"]
    assert "no agent URL" in capsys.readouterr().err


@pytest.mark.parametrize("cdp_ok, final, title, pending, expected", [
    (False, "", "", [], "cdp_unreachable"),
    (True, "login.live.com", "", [], "signed_out"),
    (True, "app.example", "T", [], "page_complete"),
    (True, "app.example", "T", ["x.example"], "page_stalled"),
    (True, "app.example", "", [], "page_stalled"),
])
def test_verdict_table(cdp_ok, final, title, pending, expected):
    assert pp.page_verdict(cdp_ok, final, title, pending) == expected
