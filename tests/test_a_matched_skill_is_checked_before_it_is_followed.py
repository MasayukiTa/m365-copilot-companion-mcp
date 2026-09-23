# -*- coding: utf-8 -*-
"""A CONFIDENT Skill match can still be the wrong TASK on the right topic.

Measured on an independent held-out set (see the CONTEXT this change was made under): a
lexical matcher's confident tier never picked a wrong Skill when a matching one existed, but
2 of 30 requests that should have matched NOTHING instead got a confident hit with the right
topic and the wrong task --

    "求人票の文章を考えてほしい、中途採用の募集をかけたい"  -> new-hire-onboarding (wrong: this
        is drafting a job posting, not onboarding a hire)
    "有給休暇は法律上何日まで繰り越せるのか教えて"          -> paid-leave-request (wrong: this
        is a legal question, not an application for leave)

A lexical matcher cannot see "same topic, different task"; the model that reads the Skill's
own description can. So every place that hands a CONFIDENT match to a model must say (1) the
Skill's one-line description and (2) an applicability check -- use it only for this task, and
if it does apply, follow it as written without re-deriving it. This file pins that sentence at
each of the three injection points: relay_fleet._with_matched_skill (pointer form and full-body
form), tools.skill_ops.skill_match's confident instruction, and the bridge's auto-match
injection in copilot_bridge.Handler._stream.

Each assertion checks for the SPECIFIC added wording, not merely "something changed" -- a
mutation that deleted the applicability sentence, or the description, while leaving everything
else intact must fail one of these.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# -------------------------------------------------------------------------------------------
# 1) relay_fleet._with_matched_skill -- both the pointer form (want_body=False) and the
#    full-body form (want_body=True), against a fake SkillStore so the test does not depend
#    on any real, locally-approved Skill.
# -------------------------------------------------------------------------------------------

GOAL = "至急、ウィジェットのラベル文言を直して"

FAKE_HIT = {
    "name": "widget-fixer",
    "score": 0.91,
    "description": "ウィジェットの表示崩れを直すときに使う手順。",
}


class _FakeStore:
    """Stands in for relay.skills.SkillStore: always a confident hit on FAKE_HIT."""

    def __init__(self, _root):
        pass

    def match(self, _text):
        return dict(FAKE_HIT)

    def render(self, _name, _arguments):
        return "WIDGET PROCEDURE BODY"


@pytest.fixture
def fake_store(monkeypatch):
    import relay.skills as skills_mod
    monkeypatch.setattr(skills_mod, "SkillStore", _FakeStore)


def test_pointer_form_carries_the_description_and_the_applicability_check(fake_store):
    from relay import relay_fleet as F
    got = F._with_matched_skill(GOAL, want_body=False)
    assert got != GOAL
    assert FAKE_HIT["description"] in got, "the one-line description must reach the model"
    assert "話題が同じでも依頼の作業内容がこの手順と異なる場合は使わず、" in got, (
        "the applicability check sentence is missing from the pointer form")
    assert "call_tool(name='skill_load'" in got, "the pointer to skill_load must survive"
    assert got.rstrip().endswith(GOAL.rstrip()), "the goal itself must still be the last word"


def test_full_body_form_also_carries_the_description_and_the_applicability_check(fake_store):
    from relay import relay_fleet as F
    got = F._with_matched_skill(GOAL, want_body=True)
    assert got != GOAL
    assert FAKE_HIT["description"] in got, "the one-line description must reach the model"
    assert "話題が同じでも依頼の作業内容がこの手順と異なる場合は使わず、" in got, (
        "the applicability check sentence is missing from the full-body form")
    assert "WIDGET PROCEDURE BODY" in got, "the procedure body itself must still be delivered"
    assert got.rstrip().endswith(GOAL.rstrip()), "the goal itself must still be the last word"


# -------------------------------------------------------------------------------------------
# 2) tools.skill_ops.skill_match's CONFIDENT tier.
# -------------------------------------------------------------------------------------------

@pytest.fixture
def skill_store_env(tmp_path, monkeypatch):
    """An isolated project root, state db and gate dir -- the same isolation
    tests/test_skills_business_mcp.py uses for tools.skill_ops, built locally so this file
    does not depend on that one's fixture."""
    from tools import skill_ops

    proj = tmp_path / "proj"
    (proj / "skills").mkdir(parents=True)
    monkeypatch.delenv("MCP_SKILLS_INCLUDE_PERSONAL", raising=False)
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(proj))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "state" / "skills.sqlite3"))
    monkeypatch.setenv("MCP_SKILLS_GATE_DIR", str(tmp_path / "gates"))
    monkeypatch.setattr(skill_ops, "_SKILL_USE_LOG", str(tmp_path / "skill_use.jsonl"))
    return skill_ops


def test_skill_match_confident_instruction_names_the_task_check(skill_store_env):
    skill_ops = skill_store_env
    store = skill_ops._store()
    store.create_local(
        "incident-report-drill",
        "サーバー障害が起きたときの障害報告書の書き方。発生日時、影響範囲、原因、"
        "復旧までの経緯をまとめる手順。",
        "# 障害報告書\n\n発生日時と影響範囲、原因、復旧までの経緯を書く。",
    )
    out = skill_ops.skill_match("サーバー障害が起きたので障害報告書を書きたい")
    hit = json.loads(out)
    assert hit["confidence"] == "confident", out
    assert hit["name"] == "incident-report-drill"
    instruction = hit["instruction"]
    assert "skill_load(name='incident-report-drill')" in instruction
    assert "follow that procedure as written" in instruction
    assert "but only if the request is for that exact task and not merely the same topic" in (
        instruction), "the confident tier must carry the applicability check too"


# -------------------------------------------------------------------------------------------
# 3) the bridge's auto-match injection in copilot_bridge.Handler._stream.
#
# _stream is callable in isolation the same way bridge/test_skills_bridge.py already does it:
# a DummyHandler standing in for the real BaseHTTPRequestHandler (it only needs _sse,
# _stream_text and the three response-header methods _stream calls before the skill check),
# and a real SkillStore rooted at tmp_path so the match is genuine rather than mocked.
# -------------------------------------------------------------------------------------------

class _DummyHandler:
    def __init__(self):
        self.events = []
        self.prompts = []

    def _sse(self, data, event=None):
        self.events.append((data, event))

    def _stream_text(self, prompt):
        self.prompts.append(prompt)

    def send_response(self, _code):
        pass

    def send_header(self, _name, _value):
        pass

    def end_headers(self):
        pass


def test_bridge_auto_match_injection_carries_the_applicability_check(tmp_path, monkeypatch):
    from bridge import copilot_bridge as bridge
    from relay.skills import SkillStore

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    store = SkillStore(tmp_path / "project", tmp_path / "skills.sqlite3", tmp_path / "gates")
    description = "Python model code review and validation"
    store.create_local("model-review", description, "Perform the model review.")
    monkeypatch.setattr(bridge, "SKILL_STORE", store)

    handler = _DummyHandler()
    bridge.Handler._stream(handler, "Python model code reviewを実施して")

    assert len(handler.prompts) == 1
    prompt = handler.prompts[0]
    assert "Trusted Skill: model-review" in prompt, "render()'s own body must still be delivered"
    assert description in prompt, "the one-line description must reach the model"
    assert (
        "Use this only if the request is for that exact task, not merely the same topic"
    ) in prompt, "the applicability check sentence is missing from the bridge injection"
    assert "Original user request" in prompt
