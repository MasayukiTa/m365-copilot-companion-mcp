# -*- coding: utf-8 -*-
"""A size cap must never turn a feature off, and a hand-started --resume must say it resumed.

THE CLASS. `fleet_runner._campaign_lines()` returned [] when .fleet/campaigns.jsonl passed 2 MB
(the live ledger is ~7 MB), so status.json `groups` lost every parent link: all nesting vanished
and nothing said why. relay/fanout_budget.py had the same shape (PR #117). The cap bounded
nothing that mattered (the ledger is append-only and read incrementally) and switched a feature
off silently. These tests pin the fix, sweep the repo for a new instance of the class, and pin
the two related findings of the third live depth-2 run:

  * a `--resume` started by hand re-queued the goals but left the snapshot `pending` (count 0), so
    a later supervisor auto-resume could resume the SAME interrupted run again;
  * the root merge of campaign c53e2941 ended DONE with slot 2 missing: the guard behaved (the
    merge input named the gap and the answer admitted it); that behaviour is pinned here.
"""
from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import acceptance as ACC          # noqa: E402
from relay import family_view as FV          # noqa: E402
from relay import fanout as FO               # noqa: E402
from relay import fleet_resume as FRES       # noqa: E402
from relay import fleet_runner as FR         # noqa: E402
from tools import turn_context as TC         # noqa: E402


# ----------------------------------------------------------------------------- groups keep nesting

def _hdr(cid, ptid, pcid="", root=""):
    return json.dumps({"kind": "campaign", "campaign_id": cid, "goal": "g", "n": 2,
                       "parent_task_id": ptid, "parent_campaign_id": pcid,
                       "root_id": root or cid}, ensure_ascii=False)


def _w(**o):
    base = dict(name="", campaign_id="", task_id="", parent_task_id=None, role="",
                subtask_index=None, status="running", outcome="", goal="", turn=0)
    base.update(o)
    return base


def _nested_fleet():
    ws = [_w(name="po", campaign_id="cO", task_id="tpo", role="producer", status="done",
             outcome="FANOUT"),
          _w(name="b", campaign_id="cO", task_id="tk1", parent_task_id="tpo", role="subtask",
             status="done", outcome="FANOUT", turn=1)]
    for i in range(2):
        ws.append(_w(name="i%d" % i, campaign_id="cI", task_id="ti%d" % i, parent_task_id="tk1",
                     role="subtask", status="running", turn=2))
    return ws


def _big_ledger(path, mb=2.6):
    """A ledger well past the old 2 MB cap: old finished campaigns around the live two."""
    pad = json.dumps({"kind": "child_result", "campaign_id": "cOld", "subtask_index": 1,
                      "outcome": "DONE", "result": "x" * 900}, ensure_ascii=False) + "\n"
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_hdr("cOld", "told") + "\n")
        fh.write(_hdr("cO", "tpo") + "\n")
        for _ in range(int(mb * 1_000_000 / len(pad))):
            fh.write(pad)
        fh.write(_hdr("cI", "tk1", pcid="cO", root="cO") + "\n")
        fh.write(json.dumps({"kind": "merged", "campaign_id": "cOld"}) + "\n")
    assert os.path.getsize(path) > 2_000_000


def test_the_snapshot_keeps_its_nesting_on_a_ledger_past_the_old_cap(tmp_path, monkeypatch):
    sd = tmp_path / "fleet"
    sd.mkdir()
    _big_ledger(str(sd / "campaigns.jsonl"))
    monkeypatch.setattr(FR, "_ACTIVE_STATE_DIR", str(sd))
    ws = _nested_fleet()
    lines = FR._campaign_lines(ws)
    assert lines, "a size cap must never turn the campaign ledger into []"
    # only what the view needs: the live campaigns plus ancestors' headers, not 2.6 MB of rows
    assert sum(len(x) for x in lines) < 20_000
    g = {x["group_id"]: x for x in FV.build_groups(ws, lines)}
    assert g["cI"]["parent_group_id"] == "cO" and g["cI"]["depth"] == 1
    assert g["cO"]["child_group_ids"] == ["cI"]


def test_the_view_from_the_filtered_lines_equals_the_view_from_the_whole_file(tmp_path):
    p = str(tmp_path / "campaigns.jsonl")
    _big_ledger(p)
    ws = _nested_fleet()
    whole = io.open(p, encoding="utf-8").read().splitlines()
    want = json.dumps(FV.build_groups(ws, whole), sort_keys=True)
    got = json.dumps(FV.build_groups(ws, FV.read_campaign_lines(p, {"cO", "cI"})), sort_keys=True)
    assert got == want


def test_the_ledger_read_is_incremental_and_tolerates_a_torn_last_line(tmp_path):
    p = str(tmp_path / "campaigns.jsonl")
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_hdr("cO", "tpo") + "\n")
    assert len(FV.read_campaign_lines(p, {"cO"})) == 1
    with io.open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(_hdr("cI", "tk1", pcid="cO", root="cO") + "\n")
        fh.write('{"kind": "child_result", "campaign_id": "cI", "subtask')      # torn, no newline
    assert len(FV.read_campaign_lines(p, {"cO", "cI"})) == 2
    with io.open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write('_index": 1, "outcome": "DONE"}\n')                              # completed
    assert len(FV.read_campaign_lines(p, {"cO", "cI"})) == 3
    assert FV.read_campaign_lines(str(tmp_path / "absent.jsonl"), {"cO"}) == []


# ----------------------------------------------------------------------------- the repo-wide guard

#: (relative path, function) -> why a size comparison that returns/continues empty is fine there.
#: An entry needs a reason a reviewer can check: bounded CPU that degrades GRACEFULLY, and says so.
ALLOWED = {
    ("tools/coding_ops.py", "_grep_file_lines"):
        "a file past MCP_GREP_MAX_FILE_MB is counted and REPORTED ('skipped N big files')",
}

_ROOTS = ("relay", "tools", "scripts", "bridge", "bench", "ui")


def _mentions_size(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == "st_size":
            return True
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == "getsize") or \
               (isinstance(f, ast.Name) and f.id == "getsize"):
                return True
    return False


def _is_cap_compare(test):
    """`<size> > cap`, `<size> >= cap` or `cap < <size>` anywhere in an `if` test."""
    for n in ast.walk(test):
        if not isinstance(n, ast.Compare):
            continue
        left, ops, comps = n.left, n.ops, n.comparators
        for op, right in zip(ops, comps):
            if isinstance(op, (ast.Gt, ast.GtE)) and _mentions_size(left):
                return True
            if isinstance(op, (ast.Lt, ast.LtE)) and _mentions_size(right) and not _mentions_size(left):
                return True
            left = right
    return False


def _is_empty_const(v):
    if v is None:
        return True
    if isinstance(v, ast.Constant):
        return v.value in (None, "", 0, False, b"")
    if isinstance(v, (ast.List, ast.Tuple, ast.Dict, ast.Set)):
        return not getattr(v, "elts", None) and not getattr(v, "keys", None)
    return False


def _silently_skips(body):
    """True when the branch does nothing but return an empty value / continue / break / pass."""
    for st in body:
        if isinstance(st, (ast.Continue, ast.Break, ast.Pass)):
            continue
        if isinstance(st, ast.Return) and _is_empty_const(st.value):
            continue
        return False
    return bool(body)


def find_silent_size_caps(source):
    """[(function, lineno)] of `if <size> > cap: return <empty>/continue` branches in `source`."""
    tree = ast.parse(source)
    hits = []

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(child, ast.If) and _is_cap_compare(child.test) and _silently_skips(child.body):
                hits.append((fn, child.lineno))
            visit(child, name)

    visit(tree, "<module>")
    return hits


def _tracked_py():
    try:
        out = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True,
                             timeout=60).stdout.split()
    except Exception:
        out = []
    if not out:
        pytest.skip("not a git checkout")
    return [p for p in out if p.split("/")[0] in _ROOTS
            and not os.path.basename(p).startswith("test_") and os.path.exists(os.path.join(REPO, p))]


def test_the_detector_recognises_the_class():
    bad = ("import os\n"
           "def lines(path):\n"
           "    if os.path.getsize(path) > 2_000_000:\n"
           "        return []\n"
           "    return open(path).read().splitlines()\n")
    assert find_silent_size_caps(bad) == [("lines", 3)]
    skip = "def f(p):\n    for q in p:\n        if q.stat().st_size >= 5:\n            continue\n"
    assert find_silent_size_caps(skip) == [("f", 3)]
    ok_marker = ("def f(path, log):\n    if os.path.getsize(path) > 9:\n"
                 "        log.append('capped')\n        return []\n")
    assert find_silent_size_caps(ok_marker) == []
    ok_rotate = "def f(path):\n    if os.path.getsize(path) < 9:\n        return\n"
    assert find_silent_size_caps(ok_rotate) == []


def test_no_size_cap_in_the_repo_switches_a_feature_off_silently():
    found = []
    for rel in _tracked_py():
        try:
            src = io.open(os.path.join(REPO, rel), encoding="utf-8-sig").read()
            hits = find_silent_size_caps(src)
        except (SyntaxError, OSError, ValueError):
            continue
        for fn, line in hits:
            if (rel, fn) not in ALLOWED:
                found.append("%s:%s (line %d)" % (rel, fn, line))
    assert not found, (
        "a size comparison that returns an empty result (or skips) with no record turns a feature "
        "OFF once the file grows, and nothing says why. Stream/tail the file instead, or record "
        "the cap (a mechanism row / a reported count) and list it in ALLOWED with the reason: "
        + "; ".join(found))


def test_the_old_caps_are_gone():
    assert not hasattr(FR, "_MAX_CAMPAIGN_LEDGER_BYTES")
    from relay import fanout_budget as FB
    assert not hasattr(FB, "_MAX_CAMPAIGN_BYTES")


# ----------------------------------------------------------------------------- turn context rotation

def test_open_turns_survive_the_rotation_of_the_turn_context_file(tmp_path, monkeypatch):
    path = str(tmp_path / "turn_context.jsonl")
    monkeypatch.setattr(TC, "CONTEXT_PATH", path)
    TC._CACHE.update(key=None, path=None, windows=[])
    t0 = time.time()
    row = {"event": "open", "worker": "w1", "job": "j1", "run": "r1", "turn": 1,
           "t_send": round(t0, 3)}
    with io.open(path + ".1", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row) + "\n")              # the open row, rotated away just now
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"event": "open", "worker": "w2", "job": "j2", "run": "r1",
                             "turn": 1, "t_send": round(t0 + 1, 3)}) + "\n")
    assert dict(TC.candidates(t0 + 2)).get("w1") == "j1", "the turn open at rotation lost its window"
    assert dict(TC.candidates(t0 + 2)).get("w2") == "j2"


# ----------------------------------------------------------------------------- manual --resume marking

NOW = 2_000_000.0


def _snap(sd, run_id="r1", state="pending", written=None, **extra):
    os.makedirs(os.path.join(str(sd), "interrupted"), exist_ok=True)
    rec = {"schema": 1, "run_id": run_id, "state": state,
           "written_ts": time.time() if written is None else written,
           "marker": {"pid": 1, "argv": ["--x"], "resume_argv": ["--x"]},
           "interrupted": {"free_bytes_at_detection": 1000}, "resume": {"count": 0}}
    rec.update(extra)
    p = os.path.join(str(sd), "interrupted", run_id + ".json")
    with io.open(p, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    return p


def _read(p):
    return json.load(io.open(p, encoding="utf-8"))


@pytest.fixture
def no_lineage(monkeypatch):
    monkeypatch.delenv("MCP_FLEET_RESUME_LINEAGE", raising=False)


def test_a_manual_resume_marks_the_snapshot_once(tmp_path, no_lineage):
    p = _snap(tmp_path)
    assert FRES.mark_manual_resume(str(tmp_path), NOW) == p
    d = _read(p)
    assert d["state"] == "resumed" and d["resume"]["count"] == 1
    assert d["resume"]["launcher"] == "manual" and d["resume"]["last_ts"] == NOW
    # idempotent: the same snapshot is never counted twice
    assert FRES.mark_manual_resume(str(tmp_path), NOW + 5) is None
    assert _read(p)["resume"]["count"] == 1


def test_a_resumed_snapshot_is_refused_by_the_gate(tmp_path, no_lineage):
    p = _snap(tmp_path)
    FRES.mark_manual_resume(str(tmp_path), NOW)
    ok, why = FRES.resume_gate(_read(p), NOW + 10_000, None, None, "")
    assert (ok, why) == (False, "state_resumed")


def test_a_supervisor_launch_marks_once_and_the_coordinator_does_not_add_a_second(
        tmp_path, monkeypatch):
    p = _snap(tmp_path)
    # the launcher (supervisor.ps1 / resume_interrupted_fleet.py) sets the lineage and marks itself
    monkeypatch.setenv("MCP_FLEET_RESUME_LINEAGE", "r1")
    assert FRES.mark_manual_resume(str(tmp_path), NOW) is None
    assert _read(p)["state"] == "pending" and _read(p)["resume"]["count"] == 0
    assert FRES.record_resume(p, NOW, 5_000, "sig")
    d = _read(p)
    assert d["state"] == "resumed" and d["resume"]["count"] == 1
    assert d["resume"]["launcher"] == "supervisor"
    assert FRES.mark_manual_resume(str(tmp_path), NOW + 1, lineage="") is None
    assert _read(p)["resume"]["count"] == 1


def test_an_old_snapshot_without_resume_fields_still_loads_and_marks(tmp_path, no_lineage):
    p = os.path.join(str(tmp_path), "interrupted")
    os.makedirs(p)
    old = os.path.join(p, "old.json")
    with io.open(old, "w", encoding="utf-8") as fh:
        json.dump({"run_id": "old", "state": "pending", "written_ts": 5.0}, fh)
    assert FRES.mark_manual_resume(str(tmp_path), NOW) == old
    d = _read(old)
    assert d["state"] == "resumed" and d["resume"]["count"] == 1


def test_it_marks_the_snapshot_the_goals_came_from(tmp_path, no_lineage):
    older = _snap(tmp_path, "r_old", written=10.0)
    newer = _snap(tmp_path, "r_new", written=20.0)
    assert FRES.mark_manual_resume(str(tmp_path), NOW) == newer
    assert _read(older)["state"] == "pending"
    # with the newest already resumed, the older pending one is NOT adopted by this launch
    assert FRES.mark_manual_resume(str(tmp_path), NOW + 1) is None
    assert _read(older)["state"] == "pending"
    assert FRES.mark_manual_resume(str(tmp_path / "none"), NOW) is None


def test_the_coordinator_calls_the_marker_after_adopting_the_goals():
    src = io.open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8").read()
    i = src.index("resume_goals = resume_goals + _kids")
    j = src.index("mark_manual_resume(args.state_dir", i)
    k = src.index("RESUME IS AN INGRESS TOO", i)
    assert i < j < k, "the marker must run after the cap check and before the run is built"


# ----------------------------------------------------------------------------- c53e2941 merge verdict

def _slot_records():
    return [{"finished": True, "outcome": "DONE", "subtask_index": 1, "result": "paper " * 40},
            {"finished": True, "outcome": "MISSING", "subtask_index": 2, "result": "ink " * 40}]


def _passes(checks, reply):
    out = []
    for spec in checks:
        c = ACC.Check(spec, reply=reply).start()
        out.append(c.poll())
    return out


def test_a_merge_with_a_missing_slot_names_the_gap_to_the_aggregator():
    goal = FO.aggregation_goal("two briefings", _slot_records(), campaign_id="cRoot")
    assert "未完了のサブタスクが 1 個" in goal["text"] and "未完了: 2" in goal["text"]
    assert "「未取得」として明示" in goal["text"]
    assert [c["type"] for c in goal["checks"]] == ["reply_contains"] * 3


def test_a_root_done_that_admits_the_gap_is_accepted():
    # the shape the live root merge produced: slot 2 named 未取得, completeness not claimed
    goal = FO.aggregation_goal("two briefings", _slot_records(), campaign_id="cRoot")
    reply = "サブタスク2（インクの歴史）は未取得です。サブタスク1は取得済み。\nDONE"
    assert all(ok for ok, _ in _passes(goal["checks"], reply))


def test_a_root_done_that_claims_completeness_with_a_missing_slot_is_refused():
    goal = FO.aggregation_goal("two briefings", _slot_records(), campaign_id="cRoot")
    for reply in ("全件取得しました。欠落なし。\nDONE",                       # false completeness
                  "サブタスク2は未取得ですが欠落なしです。\nDONE",            # both words
                  "両方のブリーフィングを統合しました。\nDONE"):              # silent about the gap
        results = _passes(goal["checks"], reply)
        assert not all(ok for ok, _ in results), reply


def test_a_clipped_child_answer_reaches_the_merge_marked_as_clipped():
    body = "x" * 1200                     # what the ledger stores: already cut at the cap
    recs = [{"finished": True, "outcome": "DONE", "subtask_index": 1, "result": body},
            {"finished": True, "outcome": "DONE", "subtask_index": 2, "result": "short"}]
    text = FO.aggregation_prompt("g", recs)
    assert text.count("…（以下略）") == 1 and "short\n" in text
