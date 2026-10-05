# -*- coding: utf-8 -*-
"""Fewer Copilot conversations: lazy creation, the unsent row, and `merge_conversation`.

PART A (zero-message conversations). A conversation id used to be minted when the Conversation
object was built, and the fleet recorded it (conv_url, transcript `guid` line, worker_done) on
the first status poll -- before any message had gone out. The id is now minted by the first
read that puts it on the wire, and every recorder peeks. A conversation that was opened and
never spoken in leaves a `conversation_created_unsent` row.

PART B (aggregator conversation). `merge_conversation=parent` runs a family's merge in the
splitting worker's conversation by putting its id in the merge goal's `resume_conv`. The merge
is still ONE goal with the same identity, so the exactly-once, requeue, nested-slot and resume
machinery is untouched; default `fresh` is byte-identical to before.

The fresh-submit ambiguity guard lives in relay/copilot_autopilot_relay.py `send()` and is NOT
touched by this change (relay/test_fresh_submit_ambiguity_is_not_retried.py keeps pinning it).
"""
from __future__ import annotations

import copy
import io
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import chathub  # noqa: E402
from relay import conversation_saving as cs  # noqa: E402
from relay import fanout  # noqa: E402
from relay import fleet_resume as fres  # noqa: E402
from relay.socket_driver import CopilotSocketDriver  # noqa: E402
import relay.relay_fleet as rf  # noqa: E402
from tools import settings_keys as SK  # noqa: E402
from tools import settings_path as SP  # noqa: E402
from tests import test_hierarchical_merge as thm  # noqa: E402


def _read(*p):
    return io.open(os.path.join(REPO, *p), encoding="utf-8").read()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.txt"
    monkeypatch.setattr(SP, "NEW_PATH", str(path))

    def put(value=None):
        if value is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text("merge_conversation=%s\n" % value, encoding="utf-8")
    return put


@pytest.fixture
def rows(monkeypatch):
    """Capture every telemetry row this module writes instead of touching .fleet."""
    got = []
    monkeypatch.setattr(cs._mt, "record", lambda mech, **kw: got.append((mech, kw)) or kw)
    return got


@pytest.fixture(autouse=True)
def _fresh_counts():
    with cs._LOCK:
        for k in cs._COUNTS:
            cs._COUNTS[k] = 0
    yield


# ---------------------------------------------------------------- PART A: lazy creation

def _conv():
    return chathub.Conversation(lambda: "token")


def test_building_a_conversation_creates_no_id():
    c = _conv()
    assert c.peek_conversation_id() == ""


def test_the_first_read_that_needs_an_id_creates_it_once():
    c = _conv()
    first = c.conversation_id
    assert first and c.peek_conversation_id() == first
    assert c.conversation_id == first            # stable afterwards


def test_a_resumed_id_is_kept_and_visible_at_once():
    c = _conv()
    c.conversation_id = "continue-me"
    assert c.peek_conversation_id() == "continue-me"
    assert c.conversation_id == "continue-me"


def test_asking_the_driver_which_conversation_it_holds_does_not_create_one():
    c = _conv()
    d = CopilotSocketDriver(c, connect=lambda *a, **k: None)
    ids = d.conversation_ids()
    assert ids["client"] == "" and ids["turns"] == 0
    assert d.conversation_ids()["client"] == ""          # polling twice still creates nothing
    assert c.peek_conversation_id() == ""


def test_the_id_is_created_by_the_call_that_sends_the_first_message(monkeypatch):
    c = _conv()
    seen = {}

    def boom(token, **kw):
        seen["id"] = kw.get("conversation_id")
        seen["peek_before"] = c.peek_conversation_id()
        raise chathub.ChatHubError("stop at the wire")

    monkeypatch.setattr(chathub, "build_ws_url", boom)
    monkeypatch.setattr(chathub, "expires_in", lambda token, **k: 1000.0)
    assert c.peek_conversation_id() == ""
    with pytest.raises(chathub.ChatHubError):
        c.ask("hello", connect=lambda *a, **k: None)
    assert seen["id"] and c.peek_conversation_id() == seen["id"]


def test_a_worker_that_has_not_sent_records_no_conversation_id(monkeypatch):
    """The status poll's capture (_capture_url) reads conversation_ids(); with nothing sent it
    must see nothing, so no `guid` line and no sess: url exist for a silent conversation."""
    src = _read("relay", "relay_fleet.py")
    i = src.index("ids = self.drv.conversation_ids() or {}\n                # CLIENT FIRST")
    assert 'cid = str(ids.get("client") or ids.get("server") or "").strip()' in src[i:i + 600]
    d = CopilotSocketDriver(_conv(), connect=lambda *a, **k: None)
    assert not str(d.conversation_ids().get("client") or "").strip()


def test_the_tab_send_path_and_its_ambiguity_guard_are_untouched():
    src = _read("relay", "copilot_autopilot_relay.py")
    assert "raise FreshSubmitAmbiguous(" in src
    assert "ONE SUBMIT ACTION PER fresh send()" in src
    fleet = _read("relay", "relay_fleet.py")
    assert "self.retryable_override = False" in fleet


# ---------------------------------------------------------------- the unsent row

def test_overdue_needs_an_opened_conversation_no_send_and_the_wait():
    now = 1000.0
    assert cs.unsent_overdue(now - 61, 0.0, 0, now=now) is True
    assert cs.unsent_overdue(now - 59, 0.0, 0, now=now) is False       # not yet
    assert cs.unsent_overdue(0.0, 0.0, 0, now=now) is False             # nothing opened
    assert cs.unsent_overdue(now - 600, now - 500, 0, now=now) is False  # a send happened
    assert cs.unsent_overdue(now - 600, 0.0, 2, now=now) is False       # turns happened


class _Drv:
    def __init__(self, ids):
        self._ids = ids
        self.closed = False

    def conversation_ids(self):
        return dict(self._ids)

    def close(self):
        self.closed = True


def _worker(**kw):
    w = rf.RelayWorker({"text": "a goal that needs a conversation"}, "w9", **kw)
    return w


def test_a_conversation_opened_and_never_used_leaves_one_row(rows):
    w = _worker()
    w.socket, w.drv = True, _Drv({"client": "", "turns": 0})
    w._attached_ts = time.time() - 120
    w.close()
    w._note_conversation_unsent("again")                 # idempotent
    mech = [kw for m, kw in rows if m == "conversation_created_unsent"]
    assert len(mech) == 1
    ex = mech[0]["extra"]
    assert ex["route"] == "socket" and ex["where"] == "close"
    assert ex["has_conversation_id"] is False and ex["age_s"] >= 119
    assert cs.status_block()["conversation_saving"]["unsent_created"] == 1


def test_a_worker_that_sent_immediately_is_unchanged(rows):
    w = _worker()
    w.socket, w.drv = True, _Drv({"client": "abc", "turns": 1})
    w._attached_ts = time.time() - 120
    w._t_send = time.time() - 100
    w.turn = 1
    w.close()
    assert [m for m, _ in rows if m == "conversation_created_unsent"] == []
    assert cs.status_block()["conversation_saving"]["unsent_created"] == 0


def test_a_conversation_still_inside_the_wait_is_not_a_row(rows):
    w = _worker()
    w.socket, w.drv = True, _Drv({"client": "", "turns": 0})
    w._attached_ts = time.time() - 5
    w.close()
    assert [m for m, _ in rows if m == "conversation_created_unsent"] == []


def test_the_pacing_wait_is_a_second_place_the_row_can_come_from(rows):
    w = _worker()
    w.socket, w.drv = True, _Drv({"client": "", "turns": 0})
    w._attached_ts = time.time() - 300
    w._note_conversation_unsent("send_pacing")
    assert [kw["extra"]["where"] for m, kw in rows
            if m == "conversation_created_unsent"] == ["send_pacing"]


def test_attach_records_when_a_conversation_was_opened():
    src = _read("relay", "relay_fleet.py")
    assert src.count("self._attached_ts = time.time()") >= 3      # socket, tab, fresh replay
    assert 'self._note_conversation_unsent("close")' in src


# ---------------------------------------------------------------- PART B: the setting

def test_the_setting_is_declared_each_gate_and_defaults_fresh():
    assert SK.effect("merge_conversation") == SK.EACH_GATE
    assert SK.default("merge_conversation") == cs.MERGE_DEFAULT == "fresh"
    assert cs.MERGE_MODES == ("fresh", "parent")


def test_the_reader_follows_the_file(settings):
    settings(None)
    assert cs.merge_conversation_setting() == "fresh"
    settings("parent")
    assert cs.merge_conversation_setting() == "parent"
    settings("PARENT")
    assert cs.merge_conversation_setting() == "parent"
    settings("maybe")                                    # junk is the default
    assert cs.merge_conversation_setting() == "fresh"
    settings("fresh")
    assert cs.merge_conversation_setting() == "fresh"


def test_the_cockpit_constants_equal_the_python_side():
    cs_src = _read("ui", "EffortPolicy.cs")
    body = cs_src[cs_src.index("class MergeConversationView"):]
    assert 'public const string Key = "%s";' % cs.MERGE_SETTING_KEY in body
    assert 'public const string Default = "%s";' % cs.MERGE_DEFAULT in body
    import re
    m = re.search(r"public static readonly string\[\] Modes = \{([^}]*)\}", body)
    assert m and tuple(x.strip().strip('"') for x in m.group(1).split(",")) == cs.MERGE_MODES


def test_the_cockpit_control_lives_in_the_popup_and_saves_through_savekey_only():
    import re
    src = _read("ui", "FleetCockpit.cs")
    assert "col.Children.Add(MergeConversationControl());" in src
    assert "ctrls.Children.Add(MergeConversationControl());" not in src       # never the header
    assert len(re.findall(r"SaveKey\(MergeConversationView\.Key, _mcVal\)", src)) == 1
    m = re.search(r"UIElement MergeConversationControl\(\).*?\n    }\n", src, re.S)
    assert m and "File." not in m.group(0)
    assert "if (!MergeConversationView.IsMode(sel) || sel == _mcVal) return;" in m.group(0)
    m = re.search(r"void PaintMergeConversation\(\).*?\n    }\n", src, re.S)
    assert m and "!Equals(ComboVal(_mcBox), _mcVal)" in m.group(0)
    assert "MergeConversationView.ParseLine(ln)" in src                          # load, validated
    m = re.search(r"void PaintMergeConversationInEffect\(.*?\n    }\n", src, re.S)
    assert m and 'Obj(root, "conversation_saving")' in m.group(0)
    assert "PaintMergeConversationInEffect(root);" in src
    assert 'case "merge_conversation":' in src
    assert SK.effect("merge_conversation") == "each_gate"


def test_status_json_carries_the_block_additively():
    fr_src = _read("relay", "fleet_runner.py")
    assert "_snap.update(_conversation_saving_block())" in fr_src
    blk = cs.status_block()["conversation_saving"]
    assert set(blk) == {"merge_conversation", "aggregators_saved", "unsent_created"}


# ---------------------------------------------------------------- apply_merge_conversation

def _camp(**kw):
    base = {"goal": "G", "n": 2, "merged": False, "checks": [], "partial": ""}
    base.update(kw)
    return base


def _recs(outcomes=("DONE", "DONE")):
    return [{"finished": True, "outcome": o, "subtask_index": i + 1, "result": "r%d" % i}
            for i, o in enumerate(outcomes)]


def _agg(recs=None):
    return fanout.aggregation_goal("G", recs or _recs(), campaign_id="cX")


def test_fresh_leaves_the_merge_goal_byte_identical(settings, rows):
    settings("fresh")
    agg = _agg()
    before = copy.deepcopy(agg)
    out = cs.apply_merge_conversation(agg, _camp(parent_conv="conv-p"), _recs())
    assert out is agg and agg == before and "resume_conv" not in agg
    assert cs.status_block()["conversation_saving"]["aggregators_saved"] == 0


def test_the_default_with_no_file_is_fresh(settings, rows):
    settings(None)
    agg = _agg()
    cs.apply_merge_conversation(agg, _camp(parent_conv="conv-p"), _recs())
    assert "resume_conv" not in agg


def test_parent_puts_the_parents_conversation_on_the_goal_and_nothing_else(settings, rows):
    settings("parent")
    agg = _agg()
    before = copy.deepcopy(agg)
    key = fres.goal_resume_key(agg)
    cs.apply_merge_conversation(agg, _camp(parent_conv="conv-p"), _recs())
    assert agg.pop("resume_conv") == "conv-p"
    assert agg == before                                  # identity, role, checks, depth intact
    assert fres.goal_resume_key(agg) == key               # the resume / exactly-once key
    assert cs.status_block()["conversation_saving"]["aggregators_saved"] == 1


def test_parent_without_a_recorded_conversation_merges_fresh_and_says_why(settings, rows):
    settings("parent")
    agg = _agg()
    cs.apply_merge_conversation(agg, _camp(), _recs())
    assert "resume_conv" not in agg
    row = [kw for m, kw in rows if m == "aggregator_conversation"][0]
    assert row["triggered"] is False and "no parent conversation" in row["not_triggered_reason"]
    assert cs.status_block()["conversation_saving"]["aggregators_saved"] == 0


def test_a_goal_that_already_resumes_something_is_never_overridden(settings, rows):
    settings("parent")
    agg = _agg()
    agg["resume_conv"] = "someone-elses"
    cs.apply_merge_conversation(agg, _camp(parent_conv="conv-p"), _recs())
    assert agg["resume_conv"] == "someone-elses"


def test_every_merge_leaves_a_measurement_row(settings, rows):
    settings("fresh")
    cs.apply_merge_conversation(_agg(), _camp(), _recs())
    cs.apply_merge_conversation(_agg(_recs(("DONE", "STUCK"))), _camp(checks=[{"type": "x"}]),
                                _recs(("DONE", "STUCK")))
    got = [kw["extra"] for m, kw in rows if m == "aggregator_conversation"]
    assert [g["concat_candidate"] for g in got] == [True, False]
    assert got[0]["slices"] == 2 and got[0]["input_chars"] == 4 and got[1]["done"] == 1


def test_the_parent_reference_needs_a_socket_parent_that_has_spoken(settings):
    settings("parent")
    w = _worker()
    assert cs.parent_conversation_ref(w) == ""                       # no driver
    w.socket, w.drv = True, _Drv({"client": "c1", "server": "s1", "turns": 0})
    assert cs.parent_conversation_ref(w) == ""                       # never spoke
    w.drv = _Drv({"client": "c1", "server": "s1", "turns": 2})
    assert cs.parent_conversation_ref(w) == "c1"
    w.socket = False
    assert cs.parent_conversation_ref(w) == ""                       # a tab has no id to continue
    settings("fresh")
    w.socket = True
    assert cs.parent_merge_kwargs(w) == {}                           # default adds no argument
    settings("parent")
    assert cs.parent_merge_kwargs(w) == {"parent_conv": "c1"}


def test_the_real_split_site_hands_the_parent_conversation_to_the_spawn():
    src = _read("relay", "relay_fleet.py")
    i = src.index("self._spawn_fn(self.goal, kids,")
    assert "**conv_saving_mod.parent_merge_kwargs(self)" in src[i:i + 400]
    assert "def _spawn_children(parent_goal, kids, parent_checks=None, parent_partial=\"\"," in src
    assert "_agg = conv_saving_mod.apply_merge_conversation(_agg, _camp, _recs, run_id=run_id)" in src


# ---------------------------------------------------------------- through run_relay_fleet

GOAL = {"text": "do the big thing in parts", "task_id": "PARENT"}


def _install_split(monkeypatch, parent_ids):
    """thm's fake workers, plus a parent that splits the way the real worker does (the same
    spawn call, the same kwargs) and finishes FANOUT. Records (task_id, resume_conv)."""
    h = thm._install(monkeypatch, {})
    monkeypatch.setattr(fanout, "HIERARCHICAL_MERGE_READY", False)
    base_poll = rf.RelayWorker.poll
    seen = []

    def poll(self):
        tid = getattr(self.task_envelope, "task_id", "") or ""
        if tid not in [s[0] for s in seen]:
            seen.append((tid, self.resume_conv))
        if self.status in rf.TERMINAL:
            return True
        if tid == "PARENT":
            self.socket, self.drv = True, _Drv(parent_ids)
            kids = fanout.child_goals(self.goal, ["part a", "part b", "part c"],
                                      parent_task_id=tid)
            self._spawn_fn(self.goal, kids, parent_checks=None, parent_partial="",
                           **cs.parent_merge_kwargs(self))
            self.status, self.outcome = "done", "FANOUT"
            return True
        return base_poll(self)

    monkeypatch.setattr(rf.RelayWorker, "poll", poll)
    return h, seen


def _go(tmp_path, h, goals):
    tdir = h._crashed_run(tmp_path, [])
    return rf.run_relay_fleet(h._FakeContext(), goals, "http://agent", max_concurrent=8, poll_s=0,
                              transcript_dir=tdir, notify=lambda *a, **k: None, fanout=False), tdir


def _merged_lines(tmp_path, cid):
    return [r for r in thm._ledger(tmp_path)
            if r.get("kind") == "merged" and r.get("campaign_id") == cid]


def test_fresh_family_merges_once_in_a_new_conversation(tmp_path, monkeypatch, settings, rows):
    settings("fresh")
    h, seen = _install_split(monkeypatch, {"client": "conv-parent", "turns": 2})
    calls = thm._spy(monkeypatch)
    _go(tmp_path, h, [dict(GOAL)])
    assert len(calls) == 1, "exactly one merge"
    cid = calls[0]["cid"]
    assert "resume_conv" not in calls[0]["item"]
    assert dict(seen)[cid + "-merge"] is None
    assert len(_merged_lines(tmp_path, cid)) == 1


def test_parent_family_merges_once_in_the_parents_conversation(tmp_path, monkeypatch, settings, rows):
    settings("parent")
    h, seen = _install_split(monkeypatch, {"client": "conv-parent", "turns": 2})
    calls = thm._spy(monkeypatch)
    _, tdir = _go(tmp_path, h, [dict(GOAL)])
    assert len(calls) == 1, "still exactly one merge, never two"
    cid = calls[0]["cid"]
    item = calls[0]["item"]
    assert item["resume_conv"] == "conv-parent"
    assert item["role"] == "aggregator" and item["task_id"] == cid + "-merge"
    assert dict(seen)[cid + "-merge"] == "conv-parent"            # the merge worker got it
    assert [t for t, _ in seen if t.endswith("-merge")] == [cid + "-merge"]
    assert len(_merged_lines(tmp_path, cid)) == 1
    assert cs.status_block()["conversation_saving"]["aggregators_saved"] == 1

    # a restart over the same ledger: the family is merged, nothing is issued again
    rf.run_relay_fleet(thm._seam()._FakeContext(), [], "http://agent", max_concurrent=8,
                       poll_s=0, transcript_dir=tdir, notify=lambda *a, **k: None, fanout=False)
    assert len(calls) == 1 and len(_merged_lines(tmp_path, cid)) == 1


def test_a_tab_parent_merges_fresh_even_under_parent(tmp_path, monkeypatch, settings, rows):
    settings("parent")
    h, seen = _install_split(monkeypatch, {"client": "", "turns": 0})   # nothing spoken
    calls = thm._spy(monkeypatch)
    _go(tmp_path, h, [dict(GOAL)])
    assert len(calls) == 1 and "resume_conv" not in calls[0]["item"]


def test_an_adopted_family_has_no_parent_conversation_and_merges_fresh(tmp_path, monkeypatch,
                                                                       settings, rows):
    """A family rebuilt from the ledger (the process that split it is gone) is the resume path:
    it must merge once, exactly as before, in a fresh conversation."""
    settings("parent")
    h = thm._install(monkeypatch, {})
    monkeypatch.setattr(thm.fanout, "HIERARCHICAL_MERGE_READY", False)
    calls = thm._spy(monkeypatch)
    cid = "cSAVEADOPT"
    hdr = [thm._hdr(cid, 3, "adopted goal text")]
    kids = [thm._kid(cid, i, 3) for i in (1, 2, 3)]
    tdir = h._crashed_run(tmp_path, hdr)
    rf.run_relay_fleet(h._FakeContext(), kids, "http://agent", max_concurrent=8, poll_s=0,
                       transcript_dir=tdir, notify=lambda *a, **k: None, fanout=False)
    assert len(thm._of(calls, cid)) == 1
    assert "resume_conv" not in thm._of(calls, cid)[0]["item"]
    assert len(_merged_lines(tmp_path, cid)) == 1
