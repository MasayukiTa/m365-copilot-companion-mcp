# -*- coding: utf-8 -*-
"""A worker may not be DONE when the matched skill's own required tools were never called.

MEASURED 2026-10-15 (Documents\\フリート設計見直し_20261014\\DB未使用の原因.md). Two goals were
settled from a different source than the one the skill library prescribes. The harness had no way
to compare what the worker DID with what the matched skill SAID, and it must not learn any
domain to do so: the skill declares ``requires_tools`` in its own frontmatter, the harness reads
the list. Every fixture here is synthetic and domain-neutral for the same reason.

Table-driven: one row per situation, the same assertions for all.
"""
import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import skill_procedure as SP  # noqa: E402
from relay import relay_fleet as F  # noqa: E402
from relay import stuck_cause as SC  # noqa: E402
from relay.skills import SkillError, SkillStore, load_bundle, requires_tools_of  # noqa: E402

T0 = 1_000_000.0


def call(tool, ts=T0 + 5, worker="", session="s1"):
    return {"event": "call", "tool": tool, "ts": ts, "worker": worker, "session": session}


# ------------------------------------------------------------------ the pure check, as a table
CHECK_CASES = [
    # id, rows, required, expected state, attributed
    ("own call of a listed tool", [call("alpha_run", worker="w1")], ["alpha_run"], SP.FOLLOWED, True),
    ("own call matches a declared glob", [call("alpha_query", worker="w1")], ["alpha_*"], SP.FOLLOWED, True),
    ("own calls, none listed", [call("read_file", worker="w1"), call("grep", worker="w1")],
     ["alpha_run"], SP.NOT_FOLLOWED, True),
    ("nobody called a listed tool", [call("read_file"), call("grep", session="s2")],
     ["alpha_*"], SP.NOT_FOLLOWED, False),
    ("another conversation called it: cannot tell, never blocks", [call("alpha_run", session="s2"),
                                                                    call("read_file")],
     ["alpha_run"], SP.UNVERIFIABLE, False),
    ("another WORKER's listed call is not this worker's", [call("alpha_run", worker="w2"),
                                                           call("read_file", worker="w1")],
     ["alpha_run"], SP.NOT_FOLLOWED, True),
    ("nothing declared: nothing checked", [call("read_file")], [], SP.NOT_APPLICABLE, False),
    ("unreadable or empty ledger: not asking is not being answered", [], ["alpha_run"],
     SP.NOT_APPLICABLE, False),
    ("a call before the worker started does not count", [call("alpha_run", ts=T0 - 50, worker="w1")],
     ["alpha_run"], SP.NOT_FOLLOWED, False),
    ("an outcome row is not a call", [{"event": "outcome", "tool": "alpha_run", "ts": T0 + 5}],
     ["alpha_run"], SP.NOT_FOLLOWED, False),
]


@pytest.mark.parametrize("name,rows,required,state,attributed", CHECK_CASES,
                         ids=[c[0] for c in CHECK_CASES])
def test_check_table(name, rows, required, state, attributed):
    v = SP.check(rows, skill="drill", required=required, worker="w1", since=T0)
    assert v.state == state, (name, v)
    if state in (SP.FOLLOWED, SP.NOT_FOLLOWED) and rows:
        assert v.attributed == attributed
    assert v.blocks is (state == SP.NOT_FOLLOWED)


GLOB_CASES = [("alpha_run", ["alpha_run"], True), ("alpha_run", ["alpha_*"], True),
              ("beta_run", ["alpha_*"], False), ("alpha_run", ["alpha_ru?"], True),
              ("alpha_run", [], False), ("", ["alpha_*"], False)]


@pytest.mark.parametrize("tool,required,expected", GLOB_CASES)
def test_tool_listed_table(tool, required, expected):
    assert SP.tool_listed(tool, required) is expected


def test_check_never_raises_on_garbage_rows():
    v = SP.check([{"event": "call", "ts": "not-a-number"}, None], skill="d",
                 required=["alpha_run"], worker="w1", since=T0)
    assert v.state == SP.NOT_APPLICABLE


# ------------------------------------------------------------------ the skill declares it
VALID_CASES = [
    (None, []), (["alpha_run"], ["alpha_run"]), (["alpha_*", " beta "], ["alpha_*", "beta"]),
]


@pytest.mark.parametrize("value,expected", VALID_CASES)
def test_requires_tools_of_valid(value, expected):
    assert requires_tools_of({"requires_tools": value}) == expected


INVALID_CASES = ["alpha_run", [""], [1], ["bad name"], ["a;b"], ["x" * 65], ["t"] * 17]


@pytest.mark.parametrize("value", INVALID_CASES, ids=[repr(v)[:20] for v in INVALID_CASES])
def test_a_malformed_requires_tools_is_refused_not_guessed(tmp_path, value):
    d = tmp_path / "skills" / "drill"
    d.mkdir(parents=True)
    import json
    (d / "SKILL.md").write_text(
        "---\nname: drill\ndescription: \"x\"\nrequires_tools: %s\n---\n\nbody\n"
        % json.dumps(value), encoding="utf-8")
    with pytest.raises(SkillError):
        load_bundle(d, "project")
    assert requires_tools_of({"requires_tools": value}) == []


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setenv("MCP_SKILLS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "skills.sqlite3"))
    monkeypatch.setenv("MCP_SKILLS_GATE_DIR", str(tmp_path / "gates"))
    return root


def test_create_local_writes_metadata_and_trusts_it(store_env):
    store = SkillStore(str(store_env))
    sk = store.create_local("drill", "在庫の棚卸し表を作るときに使う。", "# 手順\n\nやる。",
                            metadata={"keywords": ["棚卸し", "在庫差異"],
                                      "requires_tools": ["alpha_*"]})
    assert sk.trust == "trusted"
    again = load_bundle(store_env / "skills" / "drill", "project")
    assert again.metadata["keywords"] == ["棚卸し", "在庫差異"]
    assert requires_tools_of(again.metadata) == ["alpha_*"]


def test_create_local_refuses_reserved_and_existing_unless_replace(store_env):
    store = SkillStore(str(store_env))
    store.create_local("drill", "在庫の棚卸し表を作るときに使う。", "# 手順\n\nやる。")
    with pytest.raises(SkillError):
        store.create_local("drill", "別の説明", "# x")
    with pytest.raises(SkillError):
        store.create_local("other", "d", "# x", metadata={"name": "boom"})
    sk = store.create_local("drill", "在庫の棚卸し表を作るときに使う。", "# 手順2\n\nやる。",
                            metadata={"requires_tools": ["alpha_run"]}, replace=True)
    assert sk.trust == "trusted"
    assert "手順2" in sk.body


def test_an_invalid_replacement_leaves_the_previous_skill_intact(store_env):
    store = SkillStore(str(store_env))
    store.create_local("drill", "在庫の棚卸し表を作るときに使う。", "# 手順\n\nやる。")
    before = (store_env / "skills" / "drill" / "SKILL.md").read_bytes()
    with pytest.raises(SkillError):
        store.create_local("drill", "d", "# x", metadata={"requires_tools": "alpha_run"},
                           replace=True)
    assert (store_env / "skills" / "drill" / "SKILL.md").read_bytes() == before


def test_matching_reads_frontmatter_keywords_not_the_body(store_env):
    """THE MEASURED CAUSE: a skill whose body covered a kind of request but whose frontmatter did
    not name it never matched that request, and no error said so. Keywords are the author's
    remedy; this pins that the remedy works through create_local."""
    store = SkillStore(str(store_env))
    desc = "在庫データを集計・調査する依頼のときに使う。「在庫を集計して」「先月の入出庫」等。"
    body = "# 手順\n\n## 倉庫ごとの質問\n倉庫別の在庫は alpha_run で調べる。\n"
    store.create_local("drill", desc, body)
    store.create_local("filler-a", "挨拶文の添削をするときに使う。丁寧な言い回しに直す。", "# a")
    store.create_local("filler-b", "会議室の予約を取るときに使う。日時と人数を聞いて予約する。", "# b")
    goal = "在庫が多い倉庫を上位5つ教えて"
    assert store.match(goal) is None
    store.create_local("drill", desc, body, replace=True,
                       metadata={"keywords": ["倉庫別の在庫", "在庫が多い倉庫", "倉庫ごとの在庫"],
                                 "requires_tools": ["alpha_*"]})
    hit = store.match(goal)
    assert hit and hit["name"] == "drill"


# ------------------------------------------------------------------ the worker's completion gate
GOAL = "在庫が多い倉庫を上位5つ教えて"
DESC = "在庫データを集計・調査する依頼のときに使う。「倉庫別の在庫」「在庫が多い倉庫」「在庫を集計して」。"


@pytest.fixture
def matched_worker(store_env):
    store = SkillStore(str(store_env))
    store.create_local("drill", DESC, "# 手順\n\n倉庫別の在庫は alpha_run で調べる。",
                       metadata={"requires_tools": ["alpha_*"]})
    store.create_local("filler-a", "挨拶文の添削をするときに使う。丁寧な言い回しに直す。", "# a")
    store.create_local("filler-b", "会議室の予約を取るときに使う。日時と人数を聞いて予約する。", "# b")
    w = F.RelayWorker(GOAL, "w1", fanout=False)
    w._first_turn_sent_at = T0
    return w


def _rows(w, rows):
    w._ledger_rows = lambda: rows


def test_the_frame_reports_the_match_and_its_declared_tools(matched_worker):
    assert matched_worker._skill_proc.get("name") == "drill"
    assert matched_worker._skill_proc.get("requires_tools") == ["alpha_*"]
    assert matched_worker._procedure_note().startswith("drill: ")


def test_the_frame_text_is_generic_and_carries_the_standing_rule(matched_worker):
    got = F._with_matched_skill(GOAL, want_body=False)
    assert SP.FRAME_RULE in got
    got_body = F._with_matched_skill(GOAL, want_body=True)
    assert SP.FRAME_RULE in got_body


def test_a_done_without_the_required_tool_is_steered_twice_then_stuck(matched_worker):
    w = matched_worker
    _rows(w, [call("read_file"), call("grep")])           # nobody called a listed tool
    w._on_done_claimed("答え\nDONE")
    assert (w.status, w._skill_steers) == ("ready", 1)
    assert "drill" in w.job and "alpha_*" in w.job
    first = w.job
    w._on_done_claimed("答え\nDONE")
    assert (w.status, w._skill_steers) == ("ready", 2)
    assert w.job != first, "two byte-identical sends in one conversation"
    w._on_done_claimed("答え\nDONE")
    assert w.status == "stuck" and w.outcome == "STUCK"
    assert w.stuck_cause == "procedure"
    assert w.reason.startswith(SC.HEADS["procedure"])
    assert "skill procedure not followed" in w.reason
    assert SC.already_stamped(w.reason), "the generic stamper must not overwrite it"


def test_a_done_after_calling_the_required_tool_is_accepted(matched_worker):
    w = matched_worker
    _rows(w, [call("alpha_run", worker="w1")])
    w._on_done_claimed("答え\nDONE")
    assert w._skill_steers == 0
    assert w.status == "done" and w.outcome == "DONE"


def test_an_unattributable_ledger_never_blocks(matched_worker):
    w = matched_worker
    _rows(w, [call("alpha_run", session="s2")])
    w._on_done_claimed("答え\nDONE")
    assert w._skill_steers == 0 and w.status == "done"
    assert w._procedure_state == SP.UNVERIFIABLE


def test_a_goal_with_no_matched_skill_is_not_checked(store_env):
    w = F.RelayWorker("今日の東京の天気を一行で教えて", "w2", fanout=False)
    w._first_turn_sent_at = T0
    w._ledger_rows = lambda: [call("read_file")]
    w._on_done_claimed("晴れ\nDONE")
    assert w._skill_steers == 0 and w.status == "done"


def test_a_skill_that_declares_nothing_is_not_checked(store_env):
    store = SkillStore(str(store_env))
    store.create_local("drill", DESC, "# 手順\n\n何でもよい。")
    store.create_local("filler-a", "挨拶文の添削をするときに使う。丁寧な言い回しに直す。", "# a")
    store.create_local("filler-b", "会議室の予約を取るときに使う。日時と人数を聞いて予約する。", "# b")
    w = F.RelayWorker(GOAL, "w3", fanout=False)
    w._first_turn_sent_at = T0
    w._ledger_rows = lambda: [call("read_file")]
    assert w._skill_proc.get("name") == "drill" and not w._skill_proc.get("requires_tools")
    w._on_done_claimed("答え\nDONE")
    assert w._skill_steers == 0 and w.status == "done"
