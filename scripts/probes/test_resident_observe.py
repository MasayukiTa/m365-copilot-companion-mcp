# -*- coding: utf-8 -*-
"""The observation probe: masking, port refusal, shape-only output, and --check. No browser, no network."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resident_observe as RO  # noqa: E402

GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
OID_TID = GUID + "@" + "72f988bf-86f1-41af-91ab-2d7cd011db47"
SECRET = "SECRETVALUE-do-not-print-0000"


@pytest.mark.parametrize("port", [9222, 9223, 9224, 8000, 8765])
def test_live_ports_are_refused(port):
    with pytest.raises(SystemExit):
        RO.refuse_port(port)


def test_the_hidden_edge_port_is_allowed():
    RO.refuse_port(RO.DEFAULT_EDGE_PORT)


def test_main_refuses_a_live_port_before_reading_anything(tmp_path):
    with pytest.raises(SystemExit):
        RO.main(["observe", "--url-file", str(tmp_path / "does-not-exist"), "--port", "9222"])


def test_id_like_segments_are_masked_and_words_are_kept():
    for s in (GUID, OID_TID, "0123456789abcdef", "1234567", "AbC123xyzAbC123xyz9"):
        assert RO.mask_segment(s) == "<id>", s
    for s in ("conversations", "api", "v2", "history", "sydney"):
        assert RO.mask_segment(s) == s


def test_split_url_masks_ids_drops_values_keeps_query_names():
    url = "https://substrate.office.com/m365Copilot/Chathub/%s/conversations/%s?token=%s&top=20#frag" % (
        OID_TID, GUID, SECRET)
    host, path, names = RO.split_url(url)
    assert host == "substrate.office.com"
    assert path == "/m365Copilot/Chathub/<id>/conversations/<id>"
    assert names == ["token", "top"]
    for text in (host, path, " ".join(names)):
        assert SECRET not in text and GUID not in text


def test_field_names_have_no_values_and_mask_id_keys():
    body = {"items": [{"conversationId": GUID, "preview": SECRET, "owner": {"oid": GUID}}],
            GUID: {"agent": SECRET}}
    got = RO.field_names(body)
    assert "items[].conversationId" in got and "items[].owner.oid" in got
    assert "<id>.agent" in got
    joined = " ".join(got)
    assert SECRET not in joined and GUID not in joined


def test_field_names_are_bounded():
    wide = {("k%d" % i): 1 for i in range(1000)}
    assert len(RO.field_names(wide)) <= RO.MAX_FIELDS


def _jwt(aud):
    import base64
    import json
    b = base64.urlsafe_b64encode(json.dumps({"aud": aud}).encode()).decode().rstrip("=")
    return "Bearer h.%s.sig" % b


def test_the_report_carries_shapes_and_audience_but_no_value():
    events = [
        {"method": "get", "url": "https://substrate.office.com/api/conversations/%s?top=5&skip=%s" % (GUID, SECRET),
         "status": 200, "json": {"conversations": [{"id": GUID, "title": SECRET, "agentId": GUID}]},
         "authorization": _jwt("https://substrate.office.com/sydney")},
        {"method": "GET", "url": "https://substrate.office.com/api/conversations/%s?top=5&skip=%s" % (GUID, SECRET),
         "status": 200, "json": None, "authorization": ""},
        {"method": "GET", "url": "https://cdn.example.com/app.js", "status": 200, "json": None, "authorization": ""},
        {"method": "POST", "url": "https://substrate.office.com/telemetry", "status": 204, "json": None,
         "authorization": ""},
    ]
    rows = RO.summarize(RO.FakeTransport(events).collect())
    text = RO.format_report(rows)
    assert "GET substrate.office.com/api/conversations/<id> status=200 x2" in text
    assert "aud=https://substrate.office.com/sydney" in text
    assert "conversations[].agentId" in text and "query names: skip, top" in text
    assert "POST substrate.office.com/telemetry status=204 x1" in text
    assert "app.js" not in text                       # static assets are dropped
    assert SECRET not in text and GUID not in text and "Bearer" not in text


def test_a_body_is_only_read_for_history_like_paths():
    ev = [{"method": "GET", "url": "https://x.example.com/settings/flags", "status": 200,
           "json": {"flag": 1}, "authorization": ""}]
    assert RO.summarize(ev)[0]["fields"] == []


def test_check_mode_runs_the_guard_and_the_lock_and_passes():
    lines = []
    assert RO.run_check(out=lines.append) == 0
    text = "\n".join(lines)
    assert "owner_mismatch" in text and "already_executed" in text and "lock_held_by_other_pid" in text
    assert "UNEXPECTED" not in text


def test_main_check_flag(capsys):
    assert RO.main(["--check"]) == 0
    assert "all as expected" in capsys.readouterr().out
