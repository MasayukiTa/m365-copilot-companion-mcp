# -*- coding: utf-8 -*-
"""The Skill tools exactly as an agent receives them through the MCP server.

main.py registers tools.skill_ops.skill_list / skill_match / skill_load / skill_read_resource /
skill_request_approval as-is, and each builds its store from MCP_SKILLS_PROJECT_ROOT and the
default state db / gate dir. Here all three point into tmp_path, so these are the server's code
paths against a throwaway library. The question each test asks is whether the TEXT is enough for
an agent to take the next correct step: which Skill, how to load it, how to get it approved, and
why a broken one is broken.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from relay.skills import SkillStore
from tools import skill_ops

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skills_business"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "skills").mkdir(parents=True)
    monkeypatch.delenv("MCP_SKILLS_INCLUDE_PERSONAL", raising=False)
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(proj))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "state" / "skills.sqlite3"))
    monkeypatch.setenv("MCP_SKILLS_GATE_DIR", str(tmp_path / "gates"))
    monkeypatch.setattr(skill_ops, "_SKILL_USE_LOG", str(tmp_path / "skill_use.jsonl"))

    def add(name, trusted=True):
        shutil.copytree(FIXTURES / name, proj / "skills" / name)
        if trusted:
            store = skill_ops._store()
            review = store.request_approval(name)
            store.confirm_approval(name, review["token"])

    def raw(folder, text):
        d = proj / "skills" / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")

    return {"tmp": tmp_path, "proj": proj, "add": add, "raw": raw,
            "gates": tmp_path / "gates", "log": tmp_path / "skill_use.jsonl"}


def test_the_store_the_tools_use_is_the_isolated_one(lib):
    store = skill_ops._store()
    assert store.project_root == lib["proj"].resolve()
    assert store.db_path == (lib["tmp"] / "state" / "skills.sqlite3").resolve()
    assert store.gate_dir == lib["gates"].resolve()


# ------------------------------------------------------------------------------- skill_list

def test_skill_list_names_everything_but_hides_unapproved_text(lib):
    lib["add"]("invoice-check")
    lib["add"]("incident-report", trusted=False)
    rows = {r["name"]: r for r in json.loads(skill_ops.skill_list())}
    assert rows["invoice-check"]["trust"] == "trusted"
    assert "発注書・納品書との突合" in rows["invoice-check"]["description"]
    assert sorted(rows["invoice-check"]["files"]) == ["SKILL.md", "references/checklist.md"]
    assert rows["incident-report"]["trust"] == "untrusted"
    assert rows["incident-report"]["description"] == "(hidden until human approval)"
    assert "body" not in rows["invoice-check"]


def test_skill_list_explains_a_broken_bundle(lib):
    lib["add"]("invoice-check")
    lib["raw"]("expense-memo", "---\nname: expense-memo\ndescription: 注意: 締め日は25日\n---\n\n# x\n")
    payload = json.loads(skill_ops.skill_list())
    assert [r["name"] for r in payload["skills"]] == ["invoice-check"]
    bad = {r["folder"]: r for r in payload["invalid"]}["expense-memo"]
    assert "YAML" in bad["error"]
    assert "引用符で囲んでください" in bad["hint"]


def test_skill_list_hint_for_missing_frontmatter_says_add_it(lib):
    # Was a strict xfail: the message contains "YAML", and the YAML-quoting hint was chosen on
    # that word before the missing-frontmatter one was considered.
    lib["raw"]("plain-notes", "# 手順\n\n1. 書く\n")
    payload = json.loads(skill_ops.skill_list())
    bad = {r["folder"]: r for r in payload["invalid"]}["plain-notes"]
    assert "---" in bad["hint"] and "引用符" not in bad["hint"]


# ------------------------------------------------------------------------------ skill_match

def test_a_confident_match_names_the_skill_to_load(lib):
    for name in ("invoice-check", "incident-report", "inventory-stocktake"):
        lib["add"](name)
    out = skill_ops.skill_match("昨日のサーバ停止の障害報告書を書きたい")
    hit = json.loads(out)
    assert hit["name"] == "incident-report" and hit["trust"] == "trusted"
    assert 0 < hit["score"]
    assert "時系列" in hit["description"]


def test_an_agent_can_go_from_request_to_procedure(lib):
    """match -> load with arguments -> read a bundled resource, using only what the tools say."""
    for name in ("expense-reimbursement", "meeting-room-booking", "contract-review-points",
                 "customer-complaint-first-response"):
        lib["add"](name)
    cases = [
        ("接待で使ったお金の経費申請ってどうやるの", "9月分", "対象: 9月分", "references/account-codes.md"),
        ("明日の14時から会議室を予約したい", "", "施設予約システム", None),
        ("業務委託契約書のレビューをお願いしたいです", "", "損害賠償", None),
        ("苦情を受けたときの初動対応を知りたいです", "", "5W1H", "references/escalation.md"),
    ]
    for request, args, expect, resource in cases:
        name = json.loads(skill_ops.skill_match(request))["name"]
        body = skill_ops.skill_load(name, args)
        assert body.startswith("[Trusted Skill: %s" % name), body[:80]
        assert expect in body
        if resource:
            assert not skill_ops.skill_read_resource(name, resource).startswith("[")


def test_a_near_miss_on_an_unapproved_skill_asks_a_person_once(lib):
    lib["add"]("invoice-check", trusted=False)
    request = "請求書の金額が発注書と合っているかチェックしたい"
    out = skill_ops.skill_match(request)
    assert "/invoice-check looks like the right procedure" in out
    assert "has never been approved by a human" in out
    assert "承認待ちとして登録しました" in out
    assert "do not invent your own procedure" in out
    gates = list(lib["gates"].glob("*.json"))
    assert len(gates) == 1
    assert json.loads(gates[0].read_text(encoding="utf-8"))["answered"] is False
    skill_ops.skill_match(request)
    assert len(list(lib["gates"].glob("*.json"))) == 1, "asking again is not a second question"


def test_a_near_miss_on_an_edited_skill_says_re_approval(lib):
    lib["add"]("invoice-check")
    md = lib["proj"] / "skills" / "invoice-check" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "\n7. 追記\n", encoding="utf-8")
    out = skill_ops.skill_match("請求書の金額が発注書と合っているかチェックしたい")
    assert "has changed since it was approved and is waiting for human re-approval" in out


def test_no_match_lists_what_is_waiting_for_approval(lib):
    lib["add"]("invoice-check")
    lib["add"]("incident-report", trusted=False)
    out = skill_ops.skill_match("今日の天気は？")
    assert "waiting for human approval" in out and "incident-report" in out
    assert "skill_request_approval" in out


def test_no_match_in_a_fully_approved_library_is_plain(lib):
    lib["add"]("invoice-check")
    assert skill_ops.skill_match("今日の天気は？") == "(no confident Skill match)"


def test_every_consultation_is_logged_without_the_request_text(lib):
    lib["add"]("incident-report")
    secret_request = "取引先A社の山田様の件で昨日のサーバ停止の障害報告書を書きたい"
    skill_ops.skill_match(secret_request)
    skill_ops.skill_load("incident-report")
    lines = [json.loads(x) for x in lib["log"].read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in lines] == ["match", "load"]
    assert lines[0]["query_len"] == len(secret_request)
    assert "山田" not in lib["log"].read_text(encoding="utf-8")


# ------------------------------------------------------------------------------- skill_load

def test_skill_load_refusals_say_what_is_missing(lib):
    lib["add"]("incident-report", trusted=False)
    out = skill_ops.skill_load("incident-report")
    assert out.startswith("[skill_load refused:")
    assert "untrusted" in out and "a human must approve" in out
    out = skill_ops.skill_load("no-such-skill")
    assert out == "[skill_load refused: unknown Skill: no-such-skill]"
    lib["raw"]("broken-one", "---\nname: broken-one\n---\n\n# x\n")
    out = skill_ops.skill_load("broken-one")
    assert "exists on disk but its SKILL.md could not be loaded" in out
    assert "description is required" in out


def test_skill_load_renders_arguments(lib):
    lib["add"]("weekly-report-template")
    out = skill_ops.skill_load("weekly-report-template", "佐藤花子 2026-09-21")
    assert "# 週報（佐藤花子 / 2026-09-21 の週）" in out


def test_skill_read_resource_messages(lib):
    lib["add"]("quality-defect-8d-report")
    ok = skill_ops.skill_read_resource("quality-defect-8d-report", "references/8d-steps.md")
    assert "なぜなぜ分析" in ok
    bad = skill_ops.skill_read_resource("quality-defect-8d-report", "../../secret.txt")
    assert bad == ("[skill_read_resource refused: resource path must stay inside the Skill "
                   "directory]")
    bad = skill_ops.skill_read_resource("quality-defect-8d-report", "SKILL.md")
    assert "scripts/, references/, or assets/" in bad


# ------------------------------------------------------------------ skill_request_approval

def test_request_approval_for_everything_waiting(lib):
    lib["add"]("invoice-check")
    lib["add"]("incident-report", trusted=False)
    lib["add"]("inventory-stocktake", trusted=False)
    out = skill_ops.skill_request_approval()
    assert "承認待ちとして登録しました" in out
    assert "/incident-report" in out and "/inventory-stocktake" in out
    assert "/invoice-check" not in out.split("\n")[0]
    assert "承認は人の操作です" in out
    pending = [g for g in lib["gates"].glob("*.json")
               if not json.loads(g.read_text(encoding="utf-8"))["answered"]]
    assert len(pending) == 2
    # The agent can ask; it cannot approve. Nothing became trusted.
    assert all(r["trust"] != "trusted" for r in json.loads(skill_ops.skill_list())
               if r["name"] != "invoice-check")


def test_request_approval_when_nothing_is_waiting(lib):
    lib["add"]("invoice-check")
    assert skill_ops.skill_request_approval() == \
        "(every Skill is already approved -- nothing to request)"
    assert "すでに承認済み: /invoice-check" in skill_ops.skill_request_approval("invoice-check")


def test_request_approval_for_an_unknown_name_is_reported(lib):
    out = skill_ops.skill_request_approval("no-such-skill")
    assert "要求できませんでした: no-such-skill" in out


def test_request_approval_says_why_it_could_not_ask(lib):
    # Was a strict xfail: the failure was reported as "(SkillError)" with the message dropped.
    lib["raw"]("broken-one", "---\nname: broken-one\n---\n\n# x\n")
    out = skill_ops.skill_request_approval("broken-one")
    assert "description is required" in out


def test_an_approval_granted_in_the_cockpit_is_visible_to_the_next_tool_call(lib):
    lib["add"]("incident-report", trusted=False)
    skill_ops.skill_request_approval("incident-report")
    gate = next(lib["gates"].glob("*.json"))
    payload = json.loads(gate.read_text(encoding="utf-8"))
    payload.update({"answered": True, "answer": "approved"})
    gate.write_text(json.dumps(payload), encoding="utf-8")
    hit = json.loads(skill_ops.skill_match("昨日のサーバ停止の障害報告書を書きたい"))
    assert hit["name"] == "incident-report" and hit["trust"] == "trusted"
    assert SkillStore(lib["proj"]).get("incident-report").trust == "trusted"
