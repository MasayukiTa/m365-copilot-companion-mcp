from relay.test_fanout_worker import SPLIT_REPLY, _worker


def test_refused_adoption_keeps_parent_working():
    w = _worker(spawn=lambda *args, **kwargs: False)
    w._decide(SPLIT_REPLY)
    assert w.status == "ready"
    assert w.outcome != "FANOUT"
    assert not w.fanout


def test_rehydrated_family_does_not_claim_unqueued_children(tmp_path, monkeypatch):
    from relay import fanout, relay_fleet as rf
    from tests import test_hierarchical_merge as helpers

    goal = "A goal that has an abandoned earlier split"
    cid = fanout.campaign_id_for(goal)
    h = helpers._install(monkeypatch, {})
    tdir = h._crashed_run(tmp_path, [helpers._hdr(cid, 2, goal)])
    seen = []

    def poll(self):
        if self.status in rf.TERMINAL:
            return True
        if not seen:
            self.fanout = True
            self._decide("1. first slice\n2. second slice\n" + fanout.SUBTASKS_READY)
            seen.append((self.status, self.outcome))
        else:
            self._settle_done(outcome_override="DONE")
        return True

    monkeypatch.setattr(rf.RelayWorker, "poll", poll)
    rf.run_relay_fleet(h._FakeContext(), [goal], "http://agent", max_concurrent=1,
                       poll_s=0, transcript_dir=tdir, notify=lambda *a, **k: None)
    assert seen == [("ready", None)]
