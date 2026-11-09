import json

import pytest

from relay import fanout, relay_fleet as rf
from tests import test_hierarchical_merge as helpers


def _run(tmp_path, monkeypatch, mode):
    goal = "A goal with an earlier split"
    cid = fanout.campaign_id_for(goal)
    h = helpers._install(monkeypatch, {})
    children = [helpers._kid(cid, i, 2) for i in (1, 2)]
    rows = [helpers._hdr(cid, 2, goal)] + [
        {"campaign_id": cid, "subtask_index": c["subtask_index"], "goal": c}
        for c in children]
    if mode == "partial":
        rows.append({"kind": "child_result", "campaign_id": cid, "subtask_index": 1,
                     "outcome": "DONE", "result": "prior answer"})
    tdir = h._crashed_run(tmp_path, rows)
    queued = []
    seen = []
    box = list(children) if mode == "alive" else []
    original = rf.RelayWorker.poll

    def poll(self):
        if self.goal == goal and not seen:
            self.fanout = True
            self._decide("1. first slice\n2. second slice\n" + fanout.SUBTASKS_READY)
            seen.append((self.status, self.outcome, [g.get("subtask_index") for g in box]))
            if self.status not in rf.TERMINAL:
                self._settle_done(outcome_override="DONE")
            return True
        if getattr(self.task_envelope, "role", "") == "subtask":
            queued.append(self.subtask_index)
        return original(self)

    monkeypatch.setattr(rf.RelayWorker, "poll", poll)
    if mode == "failure":
        import builtins
        real_open = builtins.open
        def broken(path, *args, **kwargs):
            if str(path).endswith("campaigns.jsonl") and args and args[0] == "a":
                raise OSError("injected ledger failure")
            return real_open(path, *args, **kwargs)
        monkeypatch.setattr(builtins, "open", broken)
    rf.run_relay_fleet(h._FakeContext(), [goal], "http://agent", max_concurrent=1,
                       poll_s=0, transcript_dir=tdir, add_box=box, notify=lambda *a, **k: None)
    return seen, queued


@pytest.mark.parametrize("mode, expected", [("missing", [1, 2]), ("partial", [2]),
                                             ("alive", [1, 2])])
def test_verified_adoption(tmp_path, monkeypatch, mode, expected):
    seen, queued = _run(tmp_path, monkeypatch, mode)
    assert seen[0][0:2] == ("awaiting_children", "FANOUT")
    assert sorted(seen[0][2]) == ([] if mode == "alive" else expected)
    assert sorted(queued) == expected


def test_ledger_write_failure_runs_parent(tmp_path, monkeypatch, capsys):
    seen, queued = _run(tmp_path, monkeypatch, "failure")
    assert seen[0][0:2] == ("ready", None)
    assert not queued
    assert "campaign ledger write failed" in capsys.readouterr().out


def test_parent_is_readmitted_before_its_merge(tmp_path, monkeypatch):
    h = helpers._install(monkeypatch, {})
    tdir = h._crashed_run(tmp_path, [])
    attachments = []
    sent = []

    class Driver:
        def conversation_ids(self):
            return {"client": "parent-conversation", "turns": 1}

    def attach(self, context, agent_url):
        attachments.append((self.name, self.resume_conv))
        self.drv = Driver()
        self.socket = True
        self.status = "ready"
        return True

    def poll(self):
        if self.name == "w0" and not self._fanin_resumed:
            self.fanout = True
            self._decide("1. first slice\n2. second slice\n" + fanout.SUBTASKS_READY)
        else:
            assert self.drv is not None
            if self._fanin_resumed:
                sent.append(self.resume_conv)
            self.display_result = self.last_response = "finished"
            self._settle_done(outcome_override="DONE")
        return True

    monkeypatch.setattr(rf.RelayWorker, "attach", attach)
    monkeypatch.setattr(rf.RelayWorker, "poll", poll)
    rf.run_relay_fleet(h._FakeContext(), ["a goal to split"], "http://agent",
                       max_concurrent=1, poll_s=0, transcript_dir=tdir,
                       notify=lambda *a, **k: None)
    assert [conv for name, conv in attachments if name == "w0"] == [None, "parent-conversation"]
    assert sent == ["parent-conversation"]
