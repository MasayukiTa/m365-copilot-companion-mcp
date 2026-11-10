# -*- coding: utf-8 -*-
"""An acknowledged goal a cold start dropped is re-queued exactly once -- and only the orphan.

Measured 2026-11-10: each cold start silently dropped every goal that had landed
(acks/<jid>.ack + tasks/done/<jid>.landed.json) but had no outcome file, while goals that were
already finished, already being worked, or refused must NOT be touched. relay/orphan_readd.py
re-adds the orphan on runner start, exactly once, carrying a resubmitted_after_restart marker.

This builds a fake .fleet with three acked jids -- one with an outcome, one with a live worker,
one orphan -- and asserts only the orphan is re-added, that it carries the marker, and that a
second start (with the first re-add still parked, not yet consumed) re-adds nothing.
"""
import json
import os
import time

from relay import orphan_readd


def _mk(fleet, *parts):
    path = os.path.join(fleet, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _ack(fleet, jid):
    with open(_mk(fleet, "acks", "%s.ack" % jid), "w", encoding="utf-8") as fh:
        fh.write("{}")


def _landed(fleet, jid, status=None):
    rec = {"jid": jid}
    if status:
        rec["status"] = status
    with open(_mk(fleet, "tasks", "done", "%s.landed.json" % jid), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


def _outcome(fleet, jid):
    with open(_mk(fleet, "tasks", "done", "%s.outcome.json" % jid), "w", encoding="utf-8") as fh:
        json.dump({"jid": jid, "outcome": "DONE"}, fh)


def _delivered(fleet, jid):
    with open(_mk(fleet, "tasks", "done", "%s.delivered.json" % jid), "w", encoding="utf-8") as fh:
        json.dump({"jid": jid, "delivered": True}, fh)


def _parked(fleet, jid, text):
    with open(_mk(fleet, "for_fleet", "%s.txt" % jid), "w", encoding="utf-8") as fh:
        fh.write(text)


def _status_with_worker(fleet, jid):
    with open(_mk(fleet, "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"workers": [{"jid": jid, "status": "running"}]}, fh)


def _build_fake_fleet(fleet):
    """Three acked jids: DONE has an outcome, LIVE has a worker, ORPHAN has neither. All three
    landed and all three still have their parked goal text on disk."""
    for jid in ("jidDONE", "jidLIVE", "jidORPH"):
        _ack(fleet, jid)
        _landed(fleet, jid)
        _parked(fleet, jid, "goal text for %s" % jid)
    _outcome(fleet, "jidDONE")
    _status_with_worker(fleet, "jidLIVE")


def _capture_park():
    """A park callable that records (jid, text, marker) and 'parks' the goal by rewriting the
    for_fleet file in the marked JSON shape, so the idempotency gate sees it on a second start.
    Returns (park, calls)."""
    calls = []

    def park(jid, text, marker):
        calls.append((jid, text, dict(marker)))
        return True
    return park, calls


def test_only_the_orphan_is_readded_and_only_once(tmp_path):
    fleet = str(tmp_path / ".fleet")
    os.makedirs(fleet)
    _build_fake_fleet(fleet)

    # The pure decision names only the orphan.
    assert orphan_readd.orphan_jids(fleet) == ["jidORPH"]

    logs = []
    readded = orphan_readd.resubmit_orphans_on_start(
        fleet, log=logs.append, park=_park_into(fleet))

    assert readded == ["jidORPH"]
    # The DONE (outcome) and LIVE (worker) jids were never touched.
    assert "jidDONE" not in readded and "jidLIVE" not in readded
    # One summary line, never a per-jid line: no jid appears in the log at all.
    summary = [m for m in logs if m.startswith("orphan re-add summary:")]
    assert len(summary) == 1
    assert "re-added=1" in summary[0]
    assert not any("jidORPH" in m for m in logs)
    assert not any("jidDONE" in m for m in logs)
    assert not any("jidLIVE" in m for m in logs)

    # The status record carries the count and the jid.
    rec = orphan_readd.read_record(fleet)
    assert rec.get("count") == 1
    assert rec.get("jids") == ["jidORPH"]

    # The re-add parked the orphan as a marked resubmission.
    with open(os.path.join(fleet, "for_fleet", "jidORPH.txt"), encoding="utf-8") as fh:
        obj = json.loads(fh.read())
    assert obj.get(orphan_readd.RESUBMIT_MARKER) is True
    assert obj.get("text") == "goal text for jidORPH"

    # SECOND START, nothing consumed in between: the orphan is still parked as a resubmission,
    # so it must NOT be re-added again.
    logs2 = []
    readded2 = orphan_readd.resubmit_orphans_on_start(
        fleet, log=logs2.append, park=_park_into(fleet))
    assert readded2 == []
    assert orphan_readd.read_record(fleet).get("count") == 0
    # Still exactly one summary line on the second start, with re-added=0.
    summary2 = [m for m in logs2 if m.startswith("orphan re-add summary:")]
    assert len(summary2) == 1
    assert "re-added=0" in summary2[0]


def test_a_refused_landing_is_never_readded(tmp_path):
    """A jid the command channel READ and refused (landed.json status==refused) is a decision,
    not a drop -- it must never be re-added even with no outcome and no worker."""
    fleet = str(tmp_path / ".fleet")
    os.makedirs(fleet)
    _ack(fleet, "jidREF")
    _landed(fleet, "jidREF", status="refused")
    _parked(fleet, "jidREF", "refused goal")

    assert orphan_readd.orphan_jids(fleet) == []
    readded = orphan_readd.resubmit_orphans_on_start(fleet, log=lambda _m: None,
                                                     park=_park_into(fleet))
    assert readded == []


def test_falls_back_to_the_intake_journal_when_the_parked_text_is_gone(tmp_path):
    """If for_fleet/<jid>.txt no longer holds the goal, the intake journal's preview supplies it
    -- the one place that records what was submitted for every route, joined by jid."""
    fleet = str(tmp_path / ".fleet")
    os.makedirs(fleet)
    _ack(fleet, "jidJRNL")
    _landed(fleet, "jidJRNL")
    # No for_fleet file for this jid; the journal holds the preview.
    with open(_mk(fleet, "intake_journal.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"jid": "jidJRNL", "goal": "journal goal text"}) + "\n")

    captured = {}

    def park(jid, text, marker):
        captured[jid] = text
        return True

    readded = orphan_readd.resubmit_orphans_on_start(fleet, log=lambda _m: None, park=park)
    assert readded == ["jidJRNL"]
    assert captured["jidJRNL"] == "journal goal text"


def test_a_delivered_goal_is_never_readded(tmp_path):
    """A jid with a delivered record (tasks/done/<jid>.delivered.json) but no outcome.json is a
    finished goal whose result was delivered -- terminal exactly like an outcome, so it must never
    be re-added, and it is counted under skipped_delivered in the summary line."""
    fleet = str(tmp_path / ".fleet")
    os.makedirs(fleet)
    _ack(fleet, "jidDLV")
    _landed(fleet, "jidDLV")
    _delivered(fleet, "jidDLV")
    _parked(fleet, "jidDLV", "delivered goal")

    assert orphan_readd.orphan_jids(fleet) == []
    logs = []
    readded = orphan_readd.resubmit_orphans_on_start(
        fleet, log=logs.append, park=_park_into(fleet))
    assert readded == []
    summary = [m for m in logs if m.startswith("orphan re-add summary:")]
    assert len(summary) == 1
    assert "re-added=0" in summary[0]
    assert "skipped_delivered=1" in summary[0]


def test_an_ack_older_than_the_age_limit_is_skipped(tmp_path):
    """An orphan whose ack file mtime is older than ORPHAN_MAX_AGE_SECONDS is skipped: its done-dir
    record may have been pruned by retention, leaving no trace to judge it. A fresh orphan under
    the same conditions is still re-added, proving the age is what excludes it."""
    fleet = str(tmp_path / ".fleet")
    os.makedirs(fleet)
    for jid in ("jidOLD", "jidNEW"):
        _ack(fleet, jid)
        _landed(fleet, jid)
        _parked(fleet, jid, "goal text for %s" % jid)

    # Backdate jidOLD's ack well past the limit; leave jidNEW fresh.
    old_path = os.path.join(fleet, "acks", "jidOLD.ack")
    old_time = time.time() - (orphan_readd.ORPHAN_MAX_AGE_SECONDS + 3600)
    os.utime(old_path, (old_time, old_time))

    assert orphan_readd.orphan_jids(fleet) == ["jidNEW"]
    logs = []
    readded = orphan_readd.resubmit_orphans_on_start(
        fleet, log=logs.append, park=_park_into(fleet))
    assert readded == ["jidNEW"]
    summary = [m for m in logs if m.startswith("orphan re-add summary:")]
    assert len(summary) == 1
    assert "re-added=1" in summary[0]
    assert "skipped_too_old=1" in summary[0]


def test_the_age_limit_is_overridable_by_environment(tmp_path, monkeypatch):
    """FLEET_ORPHAN_MAX_AGE_SECONDS overrides the default: an ack a few minutes old is skipped when
    the override shrinks the window below its age."""
    fleet = str(tmp_path / ".fleet")
    os.makedirs(fleet)
    _ack(fleet, "jidENV")
    _landed(fleet, "jidENV")
    _parked(fleet, "jidENV", "env goal")

    ack_path = os.path.join(fleet, "acks", "jidENV.ack")
    past = time.time() - 600  # 10 minutes old
    os.utime(ack_path, (past, past))

    # Default window (48h) keeps it; a 60s override ages it out.
    assert orphan_readd.orphan_jids(fleet) == ["jidENV"]
    monkeypatch.setenv("FLEET_ORPHAN_MAX_AGE_SECONDS", "60")
    assert orphan_readd.orphan_jids(fleet) == []


def test_one_summary_line_counts_every_bucket(tmp_path):
    """A mixed fleet emits exactly one summary line whose counts match: one re-added orphan, one
    skipped delivered, one skipped too old, and one skipped with no recoverable text -- never one
    line per stale jid."""
    fleet = str(tmp_path / ".fleet")
    os.makedirs(fleet)
    # Re-added orphan with text.
    _ack(fleet, "jidORPH")
    _landed(fleet, "jidORPH")
    _parked(fleet, "jidORPH", "orphan goal")
    # Delivered -> skipped_delivered.
    _ack(fleet, "jidDLV")
    _landed(fleet, "jidDLV")
    _delivered(fleet, "jidDLV")
    # Too old -> skipped_too_old.
    _ack(fleet, "jidOLD")
    _landed(fleet, "jidOLD")
    _parked(fleet, "jidOLD", "old goal")
    old_time = time.time() - (orphan_readd.ORPHAN_MAX_AGE_SECONDS + 3600)
    os.utime(os.path.join(fleet, "acks", "jidOLD.ack"), (old_time, old_time))
    # Orphan with no recoverable text -> skipped_no_text (no for_fleet file, no journal entry).
    _ack(fleet, "jidNOTXT")
    _landed(fleet, "jidNOTXT")

    logs = []
    readded = orphan_readd.resubmit_orphans_on_start(
        fleet, log=logs.append, park=_park_into(fleet))
    assert readded == ["jidORPH"]
    summary = [m for m in logs if m.startswith("orphan re-add summary:")]
    assert len(summary) == 1
    line = summary[0]
    assert "re-added=1" in line
    assert "skipped_delivered=1" in line
    assert "skipped_too_old=1" in line
    assert "skipped_no_text=1" in line


def _park_into(fleet):
    """A park callable that writes the goal into this fleet's for_fleet/<jid>.txt in the marked
    JSON shape -- the same shape _park_via_for_fleet writes -- so the second-start idempotency
    gate (which reads that file) is exercised for real without standing up the router."""
    def park(jid, text, marker):
        obj = {"text": text, "priority": False}
        obj.update(marker)
        path = _mk(fleet, "for_fleet", "%s.txt" % jid)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False))
        return True
    return park
