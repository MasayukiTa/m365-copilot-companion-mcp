# -*- coding: utf-8 -*-
"""A template for a new agent is derived from one that works, by replacing three strings and nothing else.

The donor below is a small invented template (a request shape plus an agent id), not a captured one.
"""
import copy
import json
import os

import pytest

from relay import profile_token as PT
from relay import template_seed as TS
from relay.chathub import RequestTemplate

GUID_A = "11111111-1111-4111-8111-111111111111"
GUID_B = "22222222-2222-4222-8222-222222222222"
GUID_C = "33333333-3333-4333-8333-333333333333"
DONOR_ID = "T_%s.%s" % (GUID_A, GUID_B)
NEW_ID = "T_%s.%s" % (GUID_C, GUID_C.replace("3", "4"))
DONOR_URL = "https://m365.example.invalid/chat/agent/%s?titleId=T_%s" % (DONOR_ID, GUID_A)
NEW_URL = "https://m365.example.invalid/chat/agent/%s?titleId=T_%s" % (NEW_ID, GUID_C)


def _donor():
    query = {"gptId": DONOR_ID + "@MOS3", "source": "m365", "scenario": "chat"}
    frame = {
        "source": "chat",
        "gpts": [{"id": DONOR_ID, "source": "MOS3", "version": "1.0.0"}],
        "threadLevelGptId": {"id": DONOR_ID, "source": "MOS3"},
        "optionsSets": ["a", "b"],
        "message": {"author": "user", "text": ""},
    }
    return RequestTemplate(query, frame)


def test_the_agent_id_is_read_from_a_chat_address_or_given_bare():
    assert TS.agent_id_from_url(NEW_URL) == NEW_ID
    assert TS.agent_id_from_url(NEW_ID) == NEW_ID
    for bad in ("", "https://m365.example.invalid/chat", "T_not-a-guid.also-not", None):
        with pytest.raises(TS.SeedError):
            TS.agent_id_from_url(bad)


def test_only_the_three_agent_strings_change():
    donor = _donor()
    before_q, before_f = copy.deepcopy(donor.query), copy.deepcopy(donor.frame)
    query, frame = TS.derive(donor, NEW_URL)
    assert query["gptId"] == NEW_ID + "@MOS3"            # the platform suffix is kept
    assert frame["gpts"][0]["id"] == NEW_ID
    assert frame["threadLevelGptId"]["id"] == NEW_ID
    # everything else is the donor's, untouched
    expect_q, expect_f = copy.deepcopy(before_q), copy.deepcopy(before_f)
    expect_q["gptId"] = NEW_ID + "@MOS3"
    expect_f["gpts"][0]["id"] = NEW_ID
    expect_f["threadLevelGptId"]["id"] = NEW_ID
    assert query == expect_q and frame == expect_f
    # the donor object itself is not modified
    assert donor.query == before_q and donor.frame == before_f


def test_a_donor_of_another_kind_or_without_an_agent_is_refused():
    other = _donor()
    other.frame["gpts"][0]["id"] = "P_%s.name" % GUID_A
    with pytest.raises(TS.SeedError):
        TS.derive(other, NEW_URL)
    nameless = _donor()
    nameless.frame["gpts"] = [{}]
    with pytest.raises(TS.SeedError):
        TS.derive(nameless, NEW_URL)


def test_seed_writes_a_template_that_names_the_new_agent_and_leaves_the_donor_alone(tmp_path):
    d = str(tmp_path)
    assert PT.save_template(_donor(), DONOR_URL, d)
    donor_file = PT.template_path(DONOR_URL, d)
    donor_bytes = open(donor_file, "rb").read()

    written = TS.seed(DONOR_URL, NEW_URL, d)

    assert written == PT.template_path(NEW_URL, d) and written != donor_file
    assert open(donor_file, "rb").read() == donor_bytes          # the donor file is untouched
    loaded = PT.load_template(NEW_URL, d)
    assert loaded is not None and loaded.gpt_id == NEW_ID + "@MOS3" or loaded.gpt_id == NEW_ID
    on_disk = json.load(open(written, encoding="utf-8"))
    assert NEW_ID in json.dumps(on_disk) and DONOR_ID not in json.dumps(on_disk)
    assert "access_token" not in json.dumps(on_disk)               # nothing credential-shaped is written


def test_seed_without_a_cached_donor_is_an_error_not_a_default_agent(tmp_path):
    with pytest.raises(TS.SeedError):
        TS.seed(DONOR_URL, NEW_URL, str(tmp_path))
    assert not os.path.exists(PT.template_path(NEW_URL, str(tmp_path)))
