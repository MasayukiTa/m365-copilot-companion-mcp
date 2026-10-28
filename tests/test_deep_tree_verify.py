# -*- coding: utf-8 -*-
"""Exercise bottom-up merge inputs with recovered synthetic families and fake workers."""
import pytest

from relay import fanout
import relay.relay_fleet as fleet
from tests import test_hierarchical_merge as helpers


@pytest.mark.parametrize("depth", [2, 3, 4])
def test_deep_tree_merge_contents(tmp_path, monkeypatch, depth):
    ids = ["cD%d" % i for i in range(depth)]
    rows = [helpers._hdr(ids[0], 2, "Inspect the assigned source")]
    for i in range(1, depth):
        rows.append(helpers._hdr(ids[i], 2, "Inspect assigned section %d" % i,
                                 ids[i - 1], 1, i + 1))
    goals = [helpers._kid(cid, j, 2, i + 1)
             for i, cid in enumerate(ids) for j in (1, 2)]
    split = {"%s-1" % cid for cid in ids[:-1]}
    h = helpers._install(monkeypatch, {})
    calls = helpers._spy(monkeypatch)
    outputs = {}

    def poll(self):
        if self.status in fleet.TERMINAL:
            return True
        tid = getattr(self.task_envelope, "task_id", "") or ""
        if tid in split:
            self.display_result = self.last_response = helpers.PROPOSAL
            self.status, self.outcome = "done", "FANOUT"
        else:
            if tid.endswith("-merge"):
                cid = tid[:-6]
                records = helpers._of(calls, cid)[0]["records"]
                result = " | ".join(r.get("result") or "" for r in records)
            else:
                result = "OBSERVED[%s]" % tid
            self.display_result = self.last_response = result
            outputs[tid] = result
            self._settle_done(outcome_override="DONE")
        return True

    monkeypatch.setattr(fleet.RelayWorker, "poll", poll)
    helpers._run(tmp_path, h, rows, goals)
    assert [c["cid"] for c in calls] == list(reversed(ids))
    for cid in ids:
        call = helpers._of(calls, cid)[0]
        assert all(r["outcome"] == "DONE" for r in call["records"])
        assert "SUBTASKS_READY" not in call["item"]["text"]
        assert all("SUBTASKS_READY" not in (r.get("result") or "") for r in call["records"])
    root = outputs[ids[0] + "-merge"]
    for cid in ids:
        assert "OBSERVED[%s-2]" % cid in root
    assert "OBSERVED[%s-1]" % ids[-1] in root
    assert "SUBTASKS_READY" not in root
    print("depth=%d merge_order=%s root_output=%s" % (depth, ",".join(c["cid"] for c in calls), root))


def test_depth_four_is_available_from_normal_settings(monkeypatch):
    from relay import fleet_runner
    monkeypatch.setattr(fleet_runner, "_settings_int", lambda *args: 4)
    monkeypatch.setattr(fanout, "hierarchical_merge_setting", lambda: "on")
    assert fanout.effective_max_depth() == 4
    assert fanout.may_split_at(3)
    assert not fanout.may_split_at(4)
