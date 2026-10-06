# -*- coding: utf-8 -*-
"""Testing the parser is how the defect happened. This enters at the production seam.

THE DEFECT: `campaigns_from_ledger` was 43 lines, exported, and fully unit-tested -- and
nothing called it. The crash-recovery path it exists for was broken the whole time, silently.
Wiring it (2026-09-13) was the fix.

THE TEST I WROTE FOR THAT FIX MADE THE SAME MISTAKE. `test_a_family_survives_the_process_that_
split_it.py` calls `_campaigns_from_disk` directly. It proves the reader reads. It would have
passed on every day the reader had no caller, which is precisely the state it was written to
prevent -- and an adversarial review said so in those words: "A persistence/recovery feature is
not proven by testing its parser."

So this file starts at `run_relay_fleet`, the entry point a crashed fleet actually re-enters,
and asserts what an operator would notice: a family split before the crash gets MERGED after
it. The reader's caller can no longer disappear, or become semantically inert, without failing
here.

NO BROWSER. The harness is the one `relay/test_admission.py` established: a fake CDP context
plus a monkeypatched attach/poll/close, so the whole admission-and-merge loop runs for real
while nothing opens a tab.
"""
from __future__ import annotations

import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.relay_fleet as rf                                   # noqa: E402
from relay.relay_fleet import RelayWorker, TERMINAL, run_relay_fleet  # noqa: E402

PARENT = "1〜3月のメールを一覧化する"
CID = "cRECOVER"
CHECKS = [{"type": "pytest", "args": "-q tests/"}]
PARTIAL = "1月分 120件は分割前に取得済み"


class _FakeContext:
    def cookies(self):
        return []


def _browserless(mp):
    """attach/poll/close without a browser, and RAM/disk out of the way. Everything else --
    admission, the campaign loop, the merge queue -- is the real code.

    EVERY PATCH GOES THROUGH `monkeypatch` so it is undone when the test ends. Doing it by
    hand leaked `rf.free_disk_gb = lambda: 500.0` into the whole session once: a test in
    bench/ eleven minutes later read 500 GB free, and it is the test that exists to catch a
    disk-unit mismatch, so my leak was reported as that defect. It passed alone and failed in
    the suite -- the signature of exactly this.
    """
    mp.setattr(rf, "avail_phys_mb", lambda: 64000.0)
    mp.setattr(rf, "free_disk_gb", lambda path=None: 500.0)

    def fake_attach(self, context, agent_url):
        self.page = object()
        self.status = "waiting"
        return True

    def fake_poll(self):
        if self.status in TERMINAL:
            return True
        self.status, self.outcome = "done", "DONE"
        self.verified = True
        self.display_result = "この範囲は完了しました（%s）" % self.name
        return True

    def fake_close(self):
        self.closed = True
        self.page = None
        self.drv = None

    mp.setattr(RelayWorker, "attach", fake_attach)
    mp.setattr(RelayWorker, "poll", fake_poll)
    mp.setattr(RelayWorker, "close", fake_close)


def _crashed_run(tmp_path, rows, cwd="C:/work"):
    """A .fleet-shaped tree left behind by a run that split a family and then died."""
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    with io.open(str(tmp_path / "campaigns.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(tdir)


def _header(n=2, merged=False):
    rows = [{"kind": "campaign", "campaign_id": CID, "goal": PARENT, "n": n,
             "cwd": "C:/work", "checks": CHECKS, "partial": PARTIAL}]
    if merged:
        rows.append({"kind": "merged", "campaign_id": CID})
    return rows


def _orphan_children(n=2):
    """The children the dead run had queued, re-presented as goals the way `_unfinished()`
    would re-queue them: same campaign id, same slice numbers, role=subtask."""
    return [{"text": "%s\n\n担当 %d/%d" % (PARENT, i, n), "campaign_id": CID,
             "task_id": "%s-%d" % (CID, i), "role": "subtask", "parent_task_id": CID,
             "depth": 1, "subtask_index": i, "subtask_of": n, "cwd": "C:/work"}
            for i in range(1, n + 1)]


def _run(tmp_path, monkeypatch, rows, goals):
    tdir = _crashed_run(tmp_path, rows)
    _browserless(monkeypatch)
    return run_relay_fleet(_FakeContext(), goals, "http://agent",
                           max_concurrent=4, poll_s=0, transcript_dir=tdir,
                           notify=lambda *a, **k: None, fanout=False)


def _aggregators(res):
    return [r for r in res if (r.get("role") or "") == "aggregator"
            or "統合" in (r.get("goal") or "") or "分割実行の結果" in (r.get("goal") or "")]


# ── the seam ──────────────────────────────────────────────────────────────────────────────

def test_a_family_split_before_the_crash_is_merged_after_it(tmp_path, monkeypatch):
    """THE WHOLE POINT. Before the reader was wired, these children all finished and the
    answer they were collected for was never assembled -- and nothing said so."""
    res = _run(tmp_path, monkeypatch, _header(n=2), _orphan_children(2))
    merged = _aggregators(res)
    assert len(merged) == 1, (
        "クラッシュ前に分割された家族が統合されない -- 子は全部終わるのに、"
        "そのために集めた答えが組み立てられない（結果 %d 件、統合 %d 件）"
        % (len(res), len(merged)))


def test_the_merge_carries_what_only_the_ledger_still_held(tmp_path, monkeypatch):
    """cwd, the whole-goal acceptance check and the pre-split parent's work exist NOWHERE
    else once the process is gone: children no longer carry the check by design, and the
    partial belonged to a worker that ended."""
    res = _run(tmp_path, monkeypatch, _header(n=2), _orphan_children(2))
    m = _aggregators(res)[0]
    text = m.get("goal") or ""
    assert PARENT in text, "統合が親のゴールを持っていない"
    assert PARTIAL in text, "分割前に親が終えていた分が失われている"


def test_a_family_already_merged_is_not_delivered_twice(tmp_path, monkeypatch):
    """`merged` used to live only in memory, so rehydration alone would re-queue the merge for
    every campaign the fleet had ever finished, and the operator would get the same combined
    answer again with no way to tell which was current."""
    res = _run(tmp_path, monkeypatch, _header(n=2, merged=True), _orphan_children(2))
    assert _aggregators(res) == []


def test_an_incomplete_family_waits_instead_of_merging(tmp_path, monkeypatch):
    """The header says two slices; only one comes back. Merging would report a sweep that
    never ran as though it had."""
    res = _run(tmp_path, monkeypatch, _header(n=2), _orphan_children(2)[:1])
    assert _aggregators(res) == [], "半分の家族を統合した"


def test_a_run_with_no_ledger_is_unchanged(tmp_path, monkeypatch):
    """The ordinary case: nothing crashed, nothing to rehydrate, one plain goal."""
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _browserless(monkeypatch)
    res = run_relay_fleet(_FakeContext(), ["ただの用事"], "http://agent",
                          max_concurrent=2, poll_s=0, transcript_dir=str(tdir),
                          notify=lambda *a, **k: None, fanout=False)
    assert len(res) == 1 and _aggregators(res) == []


def test_the_merge_is_recorded_so_a_third_run_does_not_repeat_it(tmp_path, monkeypatch):
    """The note has to reach the file, not just the in-memory flag -- otherwise the next
    restart delivers the same merge again. Asserted on the ledger the run actually wrote."""
    tdir = _crashed_run(tmp_path, _header(n=2))
    _browserless(monkeypatch)
    run_relay_fleet(_FakeContext(), _orphan_children(2), "http://agent",
                    max_concurrent=4, poll_s=0, transcript_dir=tdir,
                    notify=lambda *a, **k: None, fanout=False)
    lines = io.open(os.path.join(os.path.dirname(tdir), "campaigns.jsonl"),
                    encoding="utf-8").read()
    assert '"kind": "merged"' in lines, "統合したのに台帳に残っていない"
    assert CID in lines
