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
    # Exactly one log line names the re-added orphan with the marker.
    assert any("jidORPH" in m and orphan_readd.RESUBMIT_MARKER in m for m in logs)
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
