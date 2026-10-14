# -*- coding: utf-8 -*-
"""Completion detection from a synthetic fleet state directory, and failure classification."""
from __future__ import annotations

import gzip
import json
import os

from tools.overnight import classify
from tools.overnight.detect import DONE, FAILED, QUEUED, RUNNING, UNSEEN, FleetReader

NOW = 1_800_000_000.0


def _worker(name, goal, status, outcome=None, task_id="", last="", transcript="", events=None,
            **extra):
    w = {"name": name, "goal": goal, "goal_summary": goal[:40], "status": status,
         "outcome": outcome, "task_id": task_id, "last": last, "transcript": transcript,
         "reason": "r", "phase_events": events or [{"ts": NOW - 600, "event": "pending"},
                                                    {"ts": NOW - 500, "event": "waiting"}]}
    w.update(extra)
    return w


def _fleet(tmp_path, workers, groups=None):
    (tmp_path / "status.json").write_text(
        json.dumps({"running": True, "workers": workers, "groups": groups or [], "total": len(workers)},
                   ensure_ascii=False), encoding="utf-8")
    return FleetReader(tmp_path)


def _transcript(tmp_path, name, goal, answer, mtime, gz=False):
    d = tmp_path / "transcripts"
    d.mkdir(exist_ok=True)
    lines = [{"meta": True, "goal": goal}, {"turn": 1, "role": "user", "text": "q"},
             {"turn": 1, "role": "assistant", "text": answer},
             {"turn": 1, "role": "metric", "name": "turn_wait_s", "value": 1}]
    data = ("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n").encode("utf-8")
    p = d / (name + (".jsonl.gz" if gz else ".jsonl"))
    if gz:
        with gzip.open(p, "wb") as fh:
            fh.write(data)
    else:
        p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return p


def test_done_worker_gives_answer_outcome_and_times(tmp_path):
    tp = _transcript(tmp_path, "r1_w0", "[A1] x", "完全な最終回答\n要点\n1\n2\n3", NOW - 100)
    (tmp_path / "tasks" / "done").mkdir(parents=True)
    (tmp_path / "tasks" / "done" / "t1.outcome.json").write_text(json.dumps(
        {"id": "t1", "ts_done": NOW - 90, "result": {"outcome": "DONE", "turns": 4, "worker": "w0"}}),
        encoding="utf-8")
    rd = _fleet(tmp_path, [_worker("w0", "[A1] x。保存して。", "done", "DONE", "t1", "短い", str(tp))])
    ob = rd.observe({"A1": "[A1]"}, NOW - 3600, NOW)["A1"]
    assert ob.state == DONE and ob.outcome == "DONE"
    assert ob.answer.startswith("完全な最終回答")        # the transcript beats the cut `last`
    assert ob.turns == 4 and ob.finished_ts == NOW - 90 and ob.started_ts == NOW - 500


def test_other_ids_are_not_mixed_up(tmp_path):
    rd = _fleet(tmp_path, [_worker("w0", "[A10] x", "done", "DONE"),
                           _worker("w1", "[A1] y", "waiting")])
    got = rd.observe({"A1": "[A1]", "A10": "[A10]", "A2": "[A2]"}, NOW - 3600, NOW)
    assert got["A1"].state == RUNNING and got["A10"].state == DONE and got["A2"].state == UNSEEN


def test_stuck_outcome_is_failed(tmp_path):
    rd = _fleet(tmp_path, [_worker("w0", "[A1] x", "done", "STUCK")])
    assert rd.observe({"A1": "[A1]"}, 0, NOW)["A1"].state == FAILED


def test_queued_worker(tmp_path):
    rd = _fleet(tmp_path, [_worker("w0", "[A1] x", "pending", events=[{"ts": NOW, "event": "pending"}])])
    assert rd.observe({"A1": "[A1]"}, 0, NOW)["A1"].state == QUEUED


def test_fanout_parent_waits_for_the_merge(tmp_path):
    w = _worker("w0", "[A1] x", "done", "FANOUT", fanout={"campaign_id": "c1"})
    rd = _fleet(tmp_path, [w], groups=[{"group_id": "c1", "merge_state": "merging"}])
    assert rd.observe({"A1": "[A1]"}, 0, NOW)["A1"].state == RUNNING
    rd = _fleet(tmp_path, [w], groups=[{"group_id": "c1", "merge_state": "merged"}])
    assert rd.observe({"A1": "[A1]"}, 0, NOW)["A1"].state == DONE


def test_latest_attempt_wins_when_a_goal_ran_twice(tmp_path):
    old = _worker("w0", "[A1] x", "done", "STUCK",
                  events=[{"ts": NOW - 5000, "event": "waiting"}])
    new = _worker("w1", "[A1] x", "waiting", events=[{"ts": NOW - 50, "event": "waiting"}])
    assert _fleet(tmp_path, [old, new]).observe({"A1": "[A1]"}, 0, NOW)["A1"].state == RUNNING


def test_status_json_missing_or_broken_is_tolerated(tmp_path):
    assert FleetReader(tmp_path).observe({"A1": "[A1]"}, 0, NOW)["A1"].state == UNSEEN
    (tmp_path / "status.json").write_bytes(b'{"workers": [ {"goal": ')
    assert FleetReader(tmp_path).observe({"A1": "[A1]"}, 0, NOW)["A1"].state == UNSEEN
    (tmp_path / "status.json").write_text('{"workers": "oops"}', encoding="utf-8")
    assert FleetReader(tmp_path).observe({"A1": "[A1]"}, 0, NOW)["A1"].state == UNSEEN


def test_worker_with_unknown_shape_does_not_raise(tmp_path):
    rd = _fleet(tmp_path, [{"goal": "[A1] x"}, "junk", {"goal": "[A1] y", "phase_events": "bad",
                                                         "outcome": 5, "status": None}])
    assert rd.observe({"A1": "[A1]"}, 0, NOW)["A1"].state in (QUEUED, RUNNING, DONE, FAILED)


def test_transcript_only_item_is_running_until_idle_then_inferred_done(tmp_path):
    _transcript(tmp_path, "r9_w0", "[A1] x", "回答", NOW - 30)
    rd = FleetReader(tmp_path)
    assert rd.observe({"A1": "[A1]"}, NOW - 3600, NOW)["A1"].state == RUNNING
    _transcript(tmp_path, "r9_w0", "[A1] x", "回答", NOW - 5000)
    ob = FleetReader(tmp_path).observe({"A1": "[A1]"}, NOW - 9000, NOW)["A1"]
    assert ob.state == DONE and ob.outcome == "INFERRED" and ob.answer == "回答"


def test_gzip_transcript_and_old_files_are_handled(tmp_path):
    _transcript(tmp_path, "old", "[A1] x", "古い", NOW - 99999, gz=True)
    _transcript(tmp_path, "new", "[A2] x", "新しい", NOW - 5000, gz=True)
    got = FleetReader(tmp_path).observe({"A1": "[A1]", "A2": "[A2]"}, NOW - 9000, NOW)
    assert got["A1"].state == UNSEEN          # older than the run
    assert got["A2"].answer == "新しい"


def test_fleet_summary_is_read(tmp_path):
    rd = _fleet(tmp_path, [])
    assert rd.fleet_summary()["running"] is True and rd.fleet_running()


# ------------------------------------------------------------------ classification
def test_classes_from_the_end_of_the_answer():
    c = classify.classify_failure
    assert c("...確認しました。DBに接続できません") == classify.DB_DOWN
    assert c("connection refused by host") == classify.DB_DOWN
    assert c("ツールが解錠されていないため実行できません") == classify.LOCKED
    assert c("処理が時間切れになりました") == classify.TIMED_OUT
    assert c("期間はどちらを指しますか。ご指定ください") == classify.AMBIGUOUS
    assert c("よく分からない失敗") == classify.OTHER
    assert c("", status="timeout") == classify.TIMED_OUT


def test_only_the_tail_counts():
    text = "接続できません\n" + ("正常な説明。" * 400)
    assert classify.classify_failure(text) == classify.OTHER


def test_lock_wins_over_db_wording():
    assert classify.classify_failure("未解錠のため接続できません") == classify.LOCKED


def test_success_with_a_failure_tail_is_flagged_only_for_environment_failures():
    assert classify.suspicious_success("結果は…\nDBに接続できませんでした") == classify.DB_DOWN
    assert classify.suspicious_success("どちらの期間か曖昧でしたが集計しました") == ""
    assert classify.suspicious_success("きれいな結果です") == ""


def test_every_category_has_a_next_step():
    for cat in (classify.DB_DOWN, classify.LOCKED, classify.TIMED_OUT, classify.AMBIGUOUS, classify.OTHER):
        assert classify.next_step_for(cat)
