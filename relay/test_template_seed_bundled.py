# -*- coding: utf-8 -*-
"""A new PC gets its first request template from the donor that ships with the product.

WHY. The socket route needs a request template per agent, a capture never sends a Copilot turn (owner
rule), and a new PC has no other PC to copy from -- so without a bundled donor every goal waits for a
token for ever. The donor must carry nothing that belongs to a user, a tenant or a machine; this file
fails if one slips in.
"""
import json
import os
import re

import pytest

from relay import profile_token as PT
from relay import template_seed as TS

GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
JWT = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}")

G1 = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
G2 = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
NEW_ID = "T_%s.%s" % (G1, G2)
NEW_URL = "https://m365.example.invalid/chat/agent/%s?titleId=T_%s" % (NEW_ID, G1)
OTHER_URL = "https://m365.example.invalid/chat/agent/T_%s.%s" % (G2, G1)


def _donor_json():
    with open(TS.donor_path("T"), encoding="utf-8") as fh:
        return json.load(fh)


def _walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, path + "." + k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _walk(v, path + "[%d]" % i)
    else:
        yield path, o


def test_the_bundled_donor_holds_no_user_tenant_or_machine_value():
    data = _donor_json()
    leaves = list(_walk(data))
    assert leaves
    for path, value in leaves:
        text = value if isinstance(value, str) else json.dumps(value)
        assert not EMAIL.search(text), "an e-mail address at %s" % path
        assert not JWT.search(text), "a token at %s" % path
        for g in GUID.findall(text):
            assert g == TS.PLACEHOLDER_GUID, "a real GUID at %s" % path
    assert "access_token" not in json.dumps(data)


def test_the_placeholder_agent_id_is_in_exactly_the_three_agent_places():
    data = _donor_json()
    where = sorted(p for p, v in _walk(data) if isinstance(v, str) and TS.PLACEHOLDER_AGENT_ID in v)
    assert where == [".frame.gpts[0].id", ".frame.threadLevelGptId.id", ".query.gptId"]


def test_the_donor_has_the_shape_of_a_captured_t_template():
    data = _donor_json()
    assert sorted(data["frame"].keys()) == sorted([
        "allowedMessageTypes", "clientCorrelationId", "clientInfo", "disconnectBehavior",
        "extraExtensionParameters", "gpts", "isSbsSupported", "isStartOfSession", "message", "options",
        "optionsSets", "renderReferencesBehindEOS", "requestId", "sessionId", "sliceIds", "source",
        "streamingMode", "threadLevelGptId", "tone", "traceId"])
    assert sorted(data["query"].keys()) == sorted([
        "agent", "agentHost", "gptId", "isEdu", "licenseType", "product", "scenario", "source", "variants"])
    assert TS.bundled_donor("T").gpt_id == TS.PLACEHOLDER_AGENT_ID


def test_seed_from_bundled_names_the_new_agent_and_nothing_else_of_the_donor(tmp_path):
    d = str(tmp_path)
    written = TS.seed_from_bundled(NEW_URL, d)
    assert written == PT.template_path(NEW_URL, d)
    loaded = PT.load_template(NEW_URL, d)
    assert loaded is not None and loaded.gpt_id == NEW_ID
    raw = open(written, encoding="utf-8").read()
    assert TS.PLACEHOLDER_AGENT_ID not in raw
    assert raw.count(NEW_ID) == 3
    assert "access_token" not in raw
    msg = loaded.frame["message"]
    assert isinstance(msg["locationInfo"]["timeZoneOffset"], int) and msg["locationInfo"]["timeZone"]
    assert msg["locale"] and msg["locale"] == msg["locale"].lower()
    # the bundled donor file itself is not changed by seeding
    assert TS.PLACEHOLDER_AGENT_ID in open(TS.donor_path("T"), encoding="utf-8").read()


def test_the_time_zone_is_a_valid_iana_name_for_this_machine_offset():
    loc = TS.machine_location()
    off = loc["timeZoneOffset"]
    if off == 0:
        assert loc["timeZone"] == "UTC"
    elif loc["timeZone"].startswith("Etc/GMT"):
        assert loc["timeZone"] == "Etc/GMT%+d" % (-off)


@pytest.mark.parametrize("url", ["", "https://m365.example.invalid/chat",
                                 "https://m365.example.invalid/chat/agent/P_%s.dr_work" % G1])
def test_an_address_that_is_not_a_t_agent_is_refused_and_writes_nothing(tmp_path, url):
    with pytest.raises(TS.SeedError):
        TS.seed_from_bundled(url, str(tmp_path))
    assert os.listdir(str(tmp_path)) == []


def test_ensure_seeds_what_is_missing_and_never_overwrites_what_exists(tmp_path):
    d = str(tmp_path)
    first = TS.ensure({"MCP_IMPL_AGENT_URL": NEW_URL, "MCP_FLEET_AGENT_URL": OTHER_URL}, d)
    assert [s for _, s, _ in first] == ["seeded", "seeded"]
    path = PT.template_path(NEW_URL, d)
    before = open(path, "rb").read()
    again = TS.ensure({"MCP_IMPL_AGENT_URL": NEW_URL}, d)
    assert [s for _, s, _ in again] == ["present"]
    assert open(path, "rb").read() == before
    skipped = TS.ensure({"MCP_IMPL_AGENT_URL": "https://m365.example.invalid/chat/agent/P_%s.x" % G1}, d)
    assert [s for _, s, _ in skipped] == ["skipped"]


def test_ensure_does_not_seed_the_same_address_twice(tmp_path):
    res = TS.ensure({"MCP_IMPL_AGENT_URL": NEW_URL, "MCP_FLEET_AGENT_URL": NEW_URL}, str(tmp_path))
    assert [s for _, s, _ in res] == ["seeded", "seeded"]
    assert len(os.listdir(str(tmp_path))) == 1


def test_a_capture_with_no_template_seeds_one_then_reads_the_token_without_a_turn(tmp_path, monkeypatch):
    PT.forget_memo()
    seen = []
    monkeypatch.setattr(PT, "token_via_light_page", lambda ctx, url, **k: seen.append(url) or "tok")
    token, template = PT.capture_via_profile(object(), NEW_URL, log=lambda _m: None, directory=str(tmp_path))
    assert token == "tok" and template.gpt_id == NEW_ID and seen == [NEW_URL]
    assert os.path.exists(PT.template_path(NEW_URL, str(tmp_path)))
    PT.forget_memo()


def test_a_capture_for_an_address_that_cannot_be_seeded_still_refuses_and_says_what_to_do(tmp_path):
    PT.forget_memo()
    with pytest.raises(PT.NoTemplateWithoutTurn) as err:
        PT.capture_via_profile(object(), "https://agent.invalid/", log=lambda _m: None,
                               directory=str(tmp_path))
    assert "template_seed ensure" in str(err.value) and "start_all" in str(err.value)


def test_the_command_line_reports_each_state_and_the_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(PT, "TEMPLATE_DIR", str(tmp_path))
    monkeypatch.setattr(TS, "configured_agent_urls", lambda env=None: {"MCP_IMPL_AGENT_URL": NEW_URL})
    assert TS.main(["check"]) == 1
    assert "NO request template" in capsys.readouterr().out
    assert TS.main(["ensure"]) == 0
    assert "created from the bundled donor" in capsys.readouterr().out
    assert TS.main(["check"]) == 0
    assert "present" in capsys.readouterr().out
    monkeypatch.setattr(TS, "configured_agent_urls", lambda env=None: {})
    assert TS.main(["ensure"]) == 0
    assert TS.main(["bogus"]) == 2
