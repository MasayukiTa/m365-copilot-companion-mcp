# -*- coding: utf-8 -*-
"""relay/task_kind.py and its only caller, the (role, kind) axis of scripts/capability_profile.py.

The label is computed by the report alone: these tests pin that no fleet module loads it, that
the default profile is unchanged, and that nothing is written."""
import ast
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import task_kind as TK  # noqa: E402
from scripts import capability_profile as CP  # noqa: E402


def test_known_wordings_get_their_kind():
    cases = {
        "IMPLEMENTATION TASK D2. Work ONLY inside the git worktree": TK.CODE_CHANGE,
        "社内電話台帳と連絡先カードの突合チェックです": TK.BULK_CHECK,
        "mcl DB(ADAMSサーバ)で不良データを集計する": TK.DATA_QUERY,
        "最新の受信メール3件を教えてください": TK.M365_LOOKUP,
        "Harmless check, read-only. Use the run_python tool once": TK.TOOL_PROBE,
        "I need two separate handbooks, hand them to two workers": TK.WRITING,
        "Open the Python file relay/socket_tools.py and quote its first line": TK.LOCAL_FILES,
    }
    for text, kind in cases.items():
        assert TK.task_kind(text) == kind, text


def test_unrecognised_or_non_text_stays_unknown():
    for goal in (None, 5, [], "", "   ", "判定結果を図にして", "17の平方根を答えてください。"):
        assert TK.task_kind(goal) == TK.UNKNOWN


def test_deterministic_and_closed_set():
    text = "mcl DB で IMPLEMENTATION TASK"
    assert {TK.task_kind(text) for _ in range(5)} == {TK.CODE_CHANGE}
    assert TK.task_kind("ADAMS") == TK.task_kind("adams") == TK.DATA_QUERY
    assert TK.KINDS[-1] == TK.UNKNOWN and len(set(TK.KINDS)) == len(TK.KINDS)


def test_narrower_kind_wins_by_rule_order():
    # a code task that mentions the database, a database task that names a repo path
    assert TK.task_kind("IMPLEMENTATION TASK: fix the odbc reader in tools/x.py") == TK.CODE_CHANGE
    assert TK.task_kind("C:\\work\\a.py の mcl DB を調べる") == TK.DATA_QUERY


def _w(role, outcome, goal, i, turn=3):
    return {"role": role, "name": "w%d" % i, "outcome": outcome, "turn": turn,
            "run_id": "r%d" % i, "task_id": "t%d" % i, "goal": goal}


def test_profile_per_role_and_kind_uses_the_same_min_n_rules():
    ws = [_w("producer", "DONE", "mcl DB の不良を集計", i) for i in range(5)]
    ws += [_w("producer", "STUCK", "IMPLEMENTATION TASK X", 10 + i) for i in range(4)]
    rep = CP.derive([], ws, [], with_kind=True)
    k = rep["profiles_by_kind"]["producer"]
    assert k["data_query"]["success"]["label"] == "good" and k["data_query"]["n_runs"] == 5
    assert k["code_change"]["n_runs"] == 4
    assert k["code_change"]["success"]["label"] == "unknown"       # 4 < min n 5
    assert k["code_change"]["success"]["rate"] == 0.0              # still printed
    assert rep["kind_coverage"] == {"code_change": 4, "data_query": 5}
    text = CP.render_text(rep, by="kind")
    assert "data_query" in text and "code_change" in text


def test_child_and_aggregator_take_the_goal_of_their_family():
    camps = [{"kind": "campaign", "campaign_id": "c1", "goal": "二つの冊子を書く"},
             {"kind": "child_result", "campaign_id": "c1", "subtask_index": 1,
              "task_id": "c1-1", "outcome": "DONE"},
             {"kind": "merge_done", "campaign_id": "c1"},
             {"kind": "child_result", "campaign_id": "c2", "subtask_index": 1,
              "task_id": "c2-1", "outcome": "DONE"}]            # no campaign row: unknown
    rep = CP.derive([], [], camps, with_kind=True)["profiles_by_kind"]
    assert rep["subtask"]["writing"]["n_runs"] == 1
    assert rep["aggregator"]["writing"]["n_runs"] == 1
    assert rep["subtask"]["unknown"]["n_runs"] == 1


def test_default_report_is_unchanged_and_never_loads_the_kind_module():
    ws = [_w("producer", "DONE", "mcl DB", i) for i in range(5)]
    rep = CP.derive([], ws, [])
    assert "profiles_by_kind" not in rep and "kind_coverage" not in rep
    assert set(rep["profiles"]["producer"]) >= {"tool_calls", "tool_mix", "n_runs"}
    # the runtime reader shares the module: loading profiles must not import the kind module
    code = ("import sys; sys.path.insert(0, %r); from scripts import capability_profile as C; "
            "C.load_profiles(%r); print('relay.task_kind' in sys.modules)"
            % (REPO, os.path.join(REPO, "no_such_fleet_dir")))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "False", out.stderr


def _py_files(*roots):
    for root in roots:
        for dirpath, dirs, files in os.walk(os.path.join(REPO, root)):
            dirs[:] = [d for d in dirs if d not in (".venv", "__pycache__", "node_modules")]
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(dirpath, f)


def test_only_the_report_uses_the_label_and_it_really_calls_it():
    users = []
    for path in _py_files("relay", "tools", "scripts", "bench"):
        if os.path.basename(path) in ("task_kind.py", "test_task_kind.py"):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        if "task_kind" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            imported = (isinstance(node, ast.ImportFrom) and
                        (node.module == "relay.task_kind" or
                         any(a.name == "task_kind" for a in node.names))) or \
                       (isinstance(node, ast.Import) and
                        any(a.name == "relay.task_kind" for a in node.names))
            if imported:
                users.append(os.path.relpath(path, REPO).replace("\\", "/"))
                break
    assert users == ["scripts/capability_profile.py"], users
    # the real caller: derive hands TK.task_kind to build_records, which calls kind_of(...)
    with open(os.path.join(REPO, "scripts", "capability_profile.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "TK.task_kind" in src and "kind_of(" in src


def test_the_report_writes_nothing(tmp_path):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / "history.json").write_text(json.dumps(
        [{"outcome": "DONE", "turn": 2, "run_id": "r", "name": "w0", "goal": "mcl DB"}]),
        encoding="utf-8")
    (fleet / "campaigns.jsonl").write_text("", encoding="utf-8")

    def snap():
        return sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in fleet.iterdir())
    before = snap()
    assert CP.main(["--fleet-dir", str(fleet), "--by", "kind", "--format", "text"]) == 0
    assert snap() == before
