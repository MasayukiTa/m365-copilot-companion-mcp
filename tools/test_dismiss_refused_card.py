# -*- coding: utf-8 -*-
"""A refused card the person can HIDE, and the hide STAYS hidden.

The owner's report (2026-10-27): a card refused for being too long has no × and no way out but
強制投入 (force in). A refused card has a record behind it (.fleet/tasks/refused/<id>.json), so the
old Dismissable rule -- stale AND no file behind it -- never let it be cleared. The fix adds a
person-only dismiss that HIDES the card WITHOUT deleting the record, and the hide survives a
cockpit restart.

This pins the Python half (tools/fleet_intake.py): the dismiss list is written to
.fleet/dismissed_refused.json beside status.json, never under tasks/refused; dismissing is
idempotent and durable; '却下をすべて消す' dismisses every not-yet-forced record at once; the
record file is LEFT IN PLACE; and force in is untouched -- a forced record is skipped by the
bulk dismiss and a dismiss never changes a record on disk. No live fleet, no real .fleet.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_PATH)

from relay import task_router as TR  # noqa: E402
from tools import fleet_intake as FI  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Isolate BOTH dirs the dismiss path touches: TR.TASKS (the refused records live under it)
    and TR.FLEET_STATE_DIR (where dismissed_refused.json is written, beside status.json). The
    force/refused test isolates only TASKS because force never writes outside it; dismiss does,
    so the state dir is patched too, or the test would write the real .fleet/dismissed_refused.json.
    """
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "state"))
    TR.ensure_dirs()
    os.makedirs(TR.FLEET_STATE_DIR, exist_ok=True)
    return tmp_path


def _make_refused(reason="too_long", goal="a refused goal nobody wants to queue",
                  source="phone", similar_to="", similarity=None):
    """Write a FAKE refused record with the real writer and return its id."""
    rid = FI._write_refused_record(reason, goal, source,
                                   similar_to=similar_to, similarity=similarity)
    assert rid
    return rid


def _dismiss_file():
    return os.path.join(TR.FLEET_STATE_DIR, FI.DISMISSED_REFUSED_FILE)


def _refused_file(rid):
    return os.path.join(FI._refused_dir(), "%s.json" % rid)


# -- the dismiss list lives beside status.json, not under tasks/refused ----------------------

def test_the_dismiss_list_is_in_the_state_dir_not_under_tasks_refused():
    """It must sit in .fleet (FLEET_STATE_DIR), beside status.json, so clearing or forcing a
    refusal (which touch tasks/refused) never disturbs the dismiss list and vice versa."""
    path = FI._dismissed_refused_path()
    assert os.path.basename(path) == "dismissed_refused.json"
    assert os.path.dirname(path) == TR.FLEET_STATE_DIR
    # the record dir (tasks/refused) must not be anywhere on the path -- check the directory
    # segments, not the basename, which legitimately contains the word "refused".
    segments = os.path.normpath(os.path.dirname(path)).split(os.sep)
    assert "refused" not in segments
    assert "tasks" not in segments


# -- dismissing hides the card, keeps the record, and is durable -----------------------------

def test_dismissing_a_card_records_the_id_without_deleting_the_record():
    rid = _make_refused()
    assert os.path.isfile(_refused_file(rid))         # the record exists first
    assert FI.dismiss_refused(rid) is True
    # the id is now in the dismiss list...
    assert rid in FI._read_dismissed_refused()
    # ...and the record file is LEFT IN PLACE (hide, never delete)
    assert os.path.isfile(_refused_file(rid)), "the record must not be deleted by a dismiss"


def test_a_dismiss_persists_so_a_reload_does_not_bring_the_card_back():
    """The whole point: re-reading the list (a cockpit restart) still sees the id, so the card
    stays hidden."""
    rid = _make_refused()
    FI.dismiss_refused(rid)
    # a fresh read off disk -- what the cockpit does every tick and on restart
    assert rid in FI._read_dismissed_refused()
    # and the on-disk file literally holds it
    with open(_dismiss_file(), encoding="utf-8") as fh:
        data = json.load(fh)
    assert rid in data["ids"]


def test_dismissing_the_same_card_twice_is_a_noop_that_still_succeeds():
    rid = _make_refused()
    assert FI.dismiss_refused(rid) is True
    assert FI.dismiss_refused(rid) is True            # idempotent
    ids = [x for x in FI._read_dismissed_refused() if x == rid]
    assert ids == [rid], "the id must appear exactly once, not twice"


def test_an_empty_id_dismisses_nothing():
    assert FI.dismiss_refused("") is False
    assert FI.dismiss_refused(None) is False
    assert FI._read_dismissed_refused() == set()


def test_a_torn_dismiss_file_reads_as_an_empty_set_and_hides_nothing():
    """A dismiss list that cannot be read hides nothing -- it never resurfaces a card by error,
    and it never raises."""
    os.makedirs(TR.FLEET_STATE_DIR, exist_ok=True)
    with open(_dismiss_file(), "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    assert FI._read_dismissed_refused() == set()


def test_a_bare_array_dismiss_file_is_read_too():
    """An older or hand-written file may be a bare array instead of {\"ids\":[...]}; both read."""
    os.makedirs(TR.FLEET_STATE_DIR, exist_ok=True)
    with open(_dismiss_file(), "w", encoding="utf-8") as fh:
        json.dump(["a", "b"], fh)
    assert FI._read_dismissed_refused() == {"a", "b"}


# -- dismiss all hides every not-yet-forced card at once, records kept -----------------------

def test_dismiss_all_hides_every_not_yet_forced_record():
    rids = [_make_refused(goal="goal %d here now" % i) for i in range(3)]
    grew = FI.dismiss_all_refused()
    assert grew == 3
    hidden = FI._read_dismissed_refused()
    for rid in rids:
        assert rid in hidden
        assert os.path.isfile(_refused_file(rid)), "dismiss-all must not delete the records"


def test_dismiss_all_skips_a_record_already_forced_in():
    """A forced record's job is already queued; the bulk dismiss leaves it alone so force in is
    not quietly undone from the list's point of view."""
    stay = _make_refused(goal="force this one through")
    FI.force_submit_refused(stay, source="cli")       # stamps the record forced
    other = _make_refused(goal="hide this other one")
    FI.dismiss_all_refused()
    hidden = FI._read_dismissed_refused()
    assert other in hidden
    assert stay not in hidden, "a forced record must not be swept into the dismiss list"


def test_dismiss_all_with_an_explicit_id_list_dismisses_exactly_those():
    rids = [_make_refused(goal="g %d" % i) for i in range(2)]
    grew = FI.dismiss_all_refused(ids=[rids[0]])
    assert grew == 1
    hidden = FI._read_dismissed_refused()
    assert rids[0] in hidden
    assert rids[1] not in hidden


# -- force in is untouched by the dismiss path ----------------------------------------------

def test_dismissing_a_card_does_not_change_the_record_so_force_still_works():
    """A dismiss only hides the card; the record is intact, so a person can still force the goal
    in afterwards (dismiss and force are independent)."""
    rid = _make_refused(goal="still forceable after a dismiss")
    FI.dismiss_refused(rid)
    rec = FI._read_refused_record(rid)
    assert rec is not None
    assert rec["forced"] is False                     # dismiss did not stamp it forced
    jid = FI.force_submit_refused(rid, source="cockpit")
    assert jid and not jid.startswith("["), "force in must still queue the goal after a dismiss"


# -- the operator CLI reaches dismiss, import never parses argv -------------------------------

def test_the_cli_wires_dismiss_without_touching_argv_on_import():
    import inspect
    src = inspect.getsource(FI._cli)
    assert "--dismiss-refused" in src
    assert "--dismiss-all-refused" in src
    assert "dismiss_refused" in src
    assert "dismiss_all_refused" in src
