# -*- coding: utf-8 -*-
"""Automatic Skill creation (tools/skill_draft) from a SYNTHETIC ledger, end to end.

What must hold for this to be usable in an office:
  * repeated, finished work becomes a proposal; work the ledger cannot ground is REFUSED with a
    reason, and work run too few times or benchmark traffic never appears at all;
  * proposals land only in the proposals directory -- never in a discovered skill root, never
    inside an existing Skill's bundle (which would revoke its approval);
  * work an existing Skill already covers becomes an amendment NEXT TO it, not an edit OF it;
  * a proposal is a bundle the store can import once a person has read and approved it.

Nothing here reads the operator's ledger or session store, or writes their .fleet: the
ledger is a tmp file, the session DB is pointed at a tmp path that does not exist, the
candidate reader is handed the tmp ledger explicitly, and write() is given a tmp directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from relay.skills import SkillError, SkillStore, load_bundle
from tools import skill_candidates, skill_draft, skill_lessons

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skills_business"

SALES = ("D:/営業/売上/2026-%s.csv の月次売上を得意先別に集計してください。"
         "出力形式は 得意先,金額 の2列のみ厳守。")
MINUTES = "今日の議事録を要約してまとめてください"
LEDGER_FAIL = "顧客台帳から今月の新規取引先を抽出して一覧にしてください"
LEDGER_OK = LEDGER_FAIL + "。検索は native search tool を使うこと、call_tool は使わない。"
TWICE = "備品台帳の棚卸結果を課長に報告してください"
BENCH = "Fix the django queryset bug in the admin"
INVOICE = ("請求書チェック: 取引先から届いた請求書を発注書・納品書と突合し、金額・消費税・"
           "振込先・登録番号を確認して、相違一覧の出力形式は 請求書番号,相違内容 の2列のみ厳守")
BACKSLASH = (r"D:\共有\経理\支払\2026-%s.xlsx の支払予定を仕入先別に集計してください。"
             "出力形式は 仕入先,金額 の2列のみ厳守。")


def _ledger_rows(extra=()):
    rows = []

    def add(goal, outcome, n=1):
        for _ in range(n):
            rows.append({"event": "worker_done", "goal": goal, "outcome": outcome,
                         "turns": 3, "ts": 1790000000.0 + len(rows)})

    for month in ("07", "08", "09"):
        add(SALES % month, "DONE")
    add(SALES % "10", "STUCK")
    add(MINUTES, "DONE", 3)
    add(LEDGER_FAIL, "STUCK")
    add(LEDGER_OK, "DONE", 3)
    add(TWICE, "DONE", 2)
    add(BENCH, "DONE", 5)
    add(INVOICE, "DONE", 3)
    for goal, outcome, n in extra:
        add(goal, outcome, n)
    # Rows the reader must skip rather than choke on.
    rows.append({"event": "worker_start", "goal": MINUTES})
    return rows


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A project with one approved Skill (invoice-check), a tmp ledger, and a tmp proposals
    directory. propose() is handed the ledger explicitly and now reads it for the candidates
    too (see test_propose_reads_the_ledger_it_is_given), so no hand-made redirect is needed."""
    monkeypatch.delenv("MCP_SKILLS_INCLUDE_PERSONAL", raising=False)
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "state" / "skills.sqlite3"))
    monkeypatch.setenv("MCP_SKILLS_GATE_DIR", str(tmp_path / "gates"))
    monkeypatch.setattr(skill_candidates, "SESSIONS_DB", str(tmp_path / "no-sessions.sqlite3"))
    proj = tmp_path / "proj"
    shutil.copytree(FIXTURES / "invoice-check", proj / "skills" / "invoice-check")
    store = SkillStore(proj, db_path=tmp_path / "state" / "skills.sqlite3",
                       gate_dir=tmp_path / "gates")
    review = store.request_approval("invoice-check")
    store.confirm_approval("invoice-check", review["token"])
    ledger = tmp_path / "ledger.jsonl"

    def write_ledger(extra=()):
        ledger.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                    for r in _ledger_rows(extra)) + "\n", encoding="utf-8")

    write_ledger()
    return {"tmp": tmp_path, "proj": proj, "store": store, "ledger": ledger,
            "write_ledger": write_ledger, "skills_dir": proj / "skills",
            "proposals": proj / ".fleet" / "skill_proposals"}


def _propose(world):
    return skill_draft.propose(ledger=str(world["ledger"]), skills_dir=str(world["skills_dir"]))


def _tree(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _by_goal(result, kind, prefix):
    return [p for p in result[kind] if p["goal"].startswith(prefix)]


# ----------------------------------------------------------------- what qualifies at all

def test_the_ledger_reader_groups_counts_and_filters(world):
    got = skill_candidates.candidates(ledger=str(world["ledger"]))
    keys = {g["key"]: g for g in got["qualified"]}
    sales = [g for k, g in keys.items() if "月次売上" in k]
    assert len(sales) == 1, "four months of one job, paths normalised, are ONE group"
    assert (sales[0]["runs"], sales[0]["done"], sales[0]["stuck"]) == (4, 3, 1)
    assert got["excluded_benchmark_runs"] == 5
    rejected = {g["key"]: g["why_not"] for g in got["rejected"]}
    assert rejected[skill_candidates.normalise(TWICE)] == "run 2 time(s), needs 3"
    assert not any("django" in k for k in keys)


def test_work_that_mostly_failed_does_not_qualify(world):
    world["write_ledger"](extra=[("経費精算の未処理分を洗い出して C:/経理/未処理 に保存", "STUCK", 3),
                                 ("経費精算の未処理分を洗い出して C:/経理/未処理 に保存", "DONE", 1)])
    got = skill_candidates.candidates(ledger=str(world["ledger"]))
    why = {g["key"]: g["why_not"] for g in got["rejected"]}
    assert why[skill_candidates.normalise("経費精算の未処理分を洗い出して C:/経理/未処理 に保存")] \
        == "finished 1 time(s), needs 2"


# ------------------------------------------------------------------ proposals and refusals

def test_repeated_work_is_proposed_and_ungrounded_work_is_refused(world):
    result = _propose(world)
    sales = _by_goal(result, "new", "D:/営業/売上")
    assert len(sales) == 1
    p = sales[0]
    assert (p["runs"], p["done"]) == (4, 3)
    assert set(p["paths"]) == {"D:/営業/売上/2026-07.csv", "D:/営業/売上/2026-08.csv",
                               "D:/営業/売上/2026-09.csv"}
    assert {"出力形式", "列のみ", "厳守"} <= set(p["contracts"])

    refused = [r for r in result["refused"] if r["goal"] == MINUTES]
    assert len(refused) == 1 and "引用できるものが無い" in refused[0]["why"]
    everything = result["new"] + result["amend"] + result["refused"]
    assert not any(x["goal"] == TWICE for x in everything), "2 runs is not a habit"
    assert not any("django" in x["goal"] for x in everything), "benchmark traffic"


def test_a_lesson_is_quoted_from_the_instruction_that_finished(world):
    result = _propose(world)
    p = _by_goal(result, "new", LEDGER_FAIL)
    assert len(p) == 1 and result["lesson_pairs"] >= 1
    texts = [lesson["text"] for lesson in p[0]["lessons"]]
    assert any("native search tool" in t for t in texts)
    assert "native search tool" in p[0]["body"]


def test_proposal_names_carry_no_business_text(world):
    result = _propose(world)
    for p in result["new"] + result["amend"] + result["refused"]:
        assert re.fullmatch(r"work-[0-9a-f]{10}", p["name"]), p["name"]


def test_a_very_long_instruction_is_quoted_with_a_cut(world):
    long_goal = ("月次の在庫差異を C:/倉庫/棚卸/差異.xlsx から集計し出力形式は 品目,差異 のみ厳守。"
                 + "対象品目: " + "、".join("部品%04d" % i for i in range(600)))
    world["write_ledger"](extra=[(long_goal, "DONE", 3)])
    result = _propose(world)
    p = [x for x in result["new"] + result["amend"] if x["goal"].startswith("月次の在庫差異")]
    assert len(p) == 1
    assert "文字を省略" in p[0]["body"]
    assert len(p[0]["body"]) < len(long_goal)


# ------------------------------------------------------------- where things land on disk

def test_proposals_land_only_in_the_proposals_directory(world):
    before_skills = _tree(world["skills_dir"])
    before_proj = {k: v for k, v in _tree(world["proj"]).items() if not k.startswith(".fleet/")}
    result = _propose(world)
    written = skill_draft.write(result, directory=str(world["proposals"]))
    assert written
    for path in written:
        assert world["proposals"].resolve() in Path(path).resolve().parents
    assert _tree(world["skills_dir"]) == before_skills
    after_proj = {k: v for k, v in _tree(world["proj"]).items() if not k.startswith(".fleet/")}
    assert after_proj == before_proj
    # And the store, scanning the same project, sees exactly what it saw before.
    assert [s.name for s in world["store"].discover()] == ["invoice-check"]
    assert world["store"].invalid_bundles() == {}
    stamp = (world["proposals"] / "last_run.txt").read_text(encoding="utf-8")
    assert "new=%d amend=%d refused=%d" % (len(result["new"]), len(result["amend"]),
                                           len(result["refused"])) in stamp


def test_a_refused_proposal_writes_no_file(world):
    result = _propose(world)
    skill_draft.write(result, directory=str(world["proposals"]))
    refused_names = {r["name"] for r in result["refused"]}
    on_disk = {p.name for p in world["proposals"].iterdir()}
    assert refused_names and not (refused_names & on_disk)


def test_an_amendment_never_edits_or_revokes_the_existing_skill(world):
    store = world["store"]
    before = store.get("invoice-check")
    result = _propose(world)
    amend = [p for p in result["amend"] if p["existing"] == "invoice-check"]
    assert len(amend) == 1
    assert not _by_goal(result, "new", "請求書チェック"), "covered work is not a second Skill"
    written = skill_draft.write(result, directory=str(world["proposals"]))
    after = store.get("invoice-check")
    assert after.trust == "trusted" and after.digest == before.digest
    assert after.files == before.files
    note = world["proposals"] / "invoice-check.amend.md"
    assert str(note) in written
    text = note.read_text(encoding="utf-8")
    assert "`skills/invoice-check/SKILL.md` への追記案 1 件" in text
    assert "上書きしない" in text


# ---------------------------------------------------- from proposal to a Skill in use

def test_a_proposal_becomes_a_usable_skill_only_after_a_person_approves_it(world):
    store = world["store"]
    result = _propose(world)
    skill_draft.write(result, directory=str(world["proposals"]))
    name = _by_goal(result, "new", "D:/営業/売上")[0]["name"]
    bundle = load_bundle(world["proposals"] / name)
    assert bundle.description.startswith("PROPOSED DRAFT -- not reviewed.")

    imported = store.import_external(world["proposals"] / name)
    assert imported.trust == "untrusted"
    with pytest.raises(SkillError):
        store.render(name)
    review = store.request_approval(name)
    assert "同じ作業の実行回数" in review["instruction_preview"]
    store.confirm_approval(name, review["token"])
    rendered = store.render(name)
    assert "同じ作業の実行回数: **4**（うち完了 3）" in rendered
    assert "D:/営業/売上/2026-07.csv" in rendered


def test_a_proposal_for_a_backslash_path_is_a_loadable_bundle(world):
    # Was a strict xfail: the description was hand-quoted YAML with backslashes unescaped, so a
    # Windows path made the SKILL.md invalid ('expected escape sequence ...').
    world["write_ledger"](extra=[(BACKSLASH % m, "DONE", 1) for m in ("07", "08", "09")])
    result = _propose(world)
    p = _by_goal(result, "new", "D:\\共有\\経理")
    assert len(p) == 1
    skill_draft.write(result, directory=str(world["proposals"]))
    bundle = load_bundle(world["proposals"] / p[0]["name"])
    # And the description says what the instruction said, backslashes included.
    assert "D:\\共有\\経理\\支払" in bundle.description


# -------------------------------------------------------- the nightly driver around it

def test_the_driver_records_what_the_draft_step_did(world, monkeypatch):
    from scripts import selfimprove_driver as driver
    log = world["tmp"] / "driver.jsonl"
    monkeypatch.setattr(driver, "LOG", str(log))
    row = driver.refresh_skill_proposals(
        propose=lambda: _propose(world),
        write=lambda r: skill_draft.write(r, directory=str(world["proposals"])))
    assert row["status"] == "ok"
    assert row["new"] >= 2 and row["amend"] == 1 and row["refused"] >= 1
    assert row["files"] == row["new"] + row["amend"]
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["event"] == \
        "skill_proposals"


def test_a_failing_draft_step_is_recorded_not_raised(world, monkeypatch):
    from scripts import selfimprove_driver as driver
    log = world["tmp"] / "driver.jsonl"
    monkeypatch.setattr(driver, "LOG", str(log))

    def boom():
        raise RuntimeError("ledger unreadable")

    row = driver.refresh_skill_proposals(propose=boom, write=lambda r: [])
    assert row["status"] == "error" and "ledger unreadable" in row["reason"]
    assert "ledger unreadable" in log.read_text(encoding="utf-8")


# ---------------------------------------------- isolation claims the test harness relies on

def test_propose_reads_the_ledger_it_is_given(tmp_path, monkeypatch):
    # Was a strict xfail: propose(ledger=X) passed X to skill_lessons.pairs() only; the
    # candidates were read through a default bound at import to the real ledger.
    seen = []

    def spy(ledger=None, include_benchmarks=False):
        seen.append(ledger)
        return {}, 0          # read nothing, from anywhere

    monkeypatch.setattr(skill_candidates, "collect", spy)
    monkeypatch.setattr(skill_candidates, "SESSIONS_DB", str(tmp_path / "none.sqlite3"))
    mine = tmp_path / "ledger.jsonl"
    mine.write_text("", encoding="utf-8")
    skill_draft.propose(ledger=str(mine), skills_dir=str(tmp_path / "skills"))
    assert seen == [str(mine)]


class _Refuse:
    """Stands in for skill_draft's `io`/`os`: records every path write() reaches for and lets
    nothing touch the disk."""

    def __init__(self, seen):
        self.seen = seen
        self.path = os.path

    def makedirs(self, path, *a, **k):
        self.seen.append(str(path))

    def open(self, path, *a, **k):
        self.seen.append(str(path))
        raise OSError("intercepted by the test: nothing is written")


def test_write_without_a_directory_honours_the_test_redirect(monkeypatch):
    # Was a strict xfail: write()'s default `directory=PROPOSALS_DIR` was bound at import,
    # before conftest's redirect, and selfimprove_driver calls write(result) exactly that way.
    seen = []
    fake = _Refuse(seen)
    monkeypatch.setattr(skill_draft, "os", fake)
    monkeypatch.setattr(skill_draft, "io", fake)
    try:
        skill_draft.write({"new": [{"name": "work-0000000000", "body": "x"}], "amend": []})
    except OSError:
        pass
    redirected = Path(skill_draft.PROPOSALS_DIR).resolve()
    assert seen, "write() reached for no path at all"
    for path in seen:
        assert redirected == Path(path).resolve() or redirected in Path(path).resolve().parents, \
            path


def test_the_conftest_redirect_itself_is_in_place():
    production = Path(skill_draft.REPO) / ".fleet" / "skill_proposals"
    assert Path(skill_draft.PROPOSALS_DIR).resolve() != production.resolve()
    assert skill_lessons.MIN_SIMILARITY > 0
