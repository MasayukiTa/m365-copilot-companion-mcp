# -*- coding: utf-8 -*-
"""The verdict has to reach the file the dashboard reads, and rows have to name their run.

WHY verify_rate WAS 0.0. The self-improvement dashboard reads `.fleet/history.json`, and that
file is written by the WPF cockpit from the fleet's status snapshot -- a second program, in
another language, copying a chosen subset of fields. It copied eleven and not `verified`.

The three-state answer exists and always did: `None` = the acceptance check never ran, `False`
= it ran and failed, `True` = it passed. `status` cannot carry that distinction, because "done"
covers both "proved" and "never asked". Measured before the fix: 44 archived rows, none with
`verified`; the live snapshot for the same worker had it. So the dashboard was not reporting a
measured zero, it was reporting an absent field, and 0.318 completion sat beside it looking
equally trustworthy.

relay_fleet.py already warns about exactly this, next to a ledger it writes itself rather than
through the cockpit: anything that reaches an analysis only through history.json is "hostage to
a second program choosing to carry the field".

The other half is identity. Four notions of "run" exist across the ledgers and none of them
join -- a process id, an epoch, "<epoch>#<worker>", and the transcripts' "<run_id>_<name>" --
while judge.jsonl (5,712 verdicts) and skill_use.jsonl carry no identity at all. The
transcripts hold the only id that names a run, so that is the one to publish.
"""
import io
import re

import pytest

from relay.fleet_runner import _run_id_of, goals_from_command, read_commands
from relay.relay_fleet import RelayWorker
from relay.task_router import add_goal_to_live_fleet


class _W:
    def __init__(self, transcript=""):
        self.transcript = transcript


# ---- the run id: read from the transcript, never minted ---------------------

def test_the_run_id_is_the_transcripts_own():
    assert _run_id_of(_W(r"C:\x\.fleet\transcripts\r6a9f9ad6_a0_w0.jsonl"), 0) == "r6a9f9ad6_a0"


def test_a_compressed_transcript_gives_the_same_id():
    """Old transcripts are gzipped; a run must not change identity when its file is compressed."""
    assert _run_id_of(_W("/t/r6a9f8f94_a0_w12.jsonl.gz"), 0) == "r6a9f8f94_a0"


def test_two_workers_of_one_run_share_it():
    """This is the whole point: an id that is unique per ROW joins to nothing. history.json's
    existing `key` is "<epoch>#<worker>" and was unique in all 44 rows."""
    assert _run_id_of(_W("a/r1_a0_w0.jsonl"), 0) == _run_id_of(_W("a/r1_a0_w9.jsonl"), 0)


def test_a_worker_with_no_transcript_still_names_its_run():
    """A worker that has not written a turn yet has no file to read. Falling back to the run's
    start epoch keeps its rows grouped with the run instead of anonymous."""
    got = _run_id_of(_W(""), int("6a9f9ad6", 16))
    assert got == "r6a9f9ad6"


def test_a_path_without_a_worker_suffix_does_not_produce_a_wrong_id():
    """`<run_id>_<name>` is the only shape that carries a name. A bare file name has no worker
    segment to strip, so returning it would invent an id from something that is not one."""
    assert _run_id_of(_W("/t/nofrills.jsonl"), 7) == "r7"


# ---- the snapshot publishes it, and the cockpit carries it forward ----------

def test_the_snapshot_publishes_the_run_id(repo_root=None):
    src = io.open("relay/fleet_runner.py", encoding="utf-8").read()
    assert '"run_id": _run_id_of(w, started)' in src, "the snapshot no longer names the run"


@pytest.mark.parametrize("field", ["verified", "verify_attempts", "run_id", "jid"])
def test_both_archive_sites_carry_the_field(field):
    """BOTH SITES. One archive path runs when a worker reaches a terminal state, the other when
    the operator retires it by hand. Fixing one and leaving the other would make the history
    depend on which route retired the worker -- and that split is the exact shape of the misses
    this codebase keeps recording."""
    src = io.open("ui/FleetCockpit.cs", encoding="utf-8").read()
    hits = len(re.findall(r'e\["%s"\]\s*=' % field, src))
    assert hits == 2, "%s is written at %d of the 2 archive sites" % (field, hits)


def test_verified_is_copied_as_a_tri_state_not_coerced():
    """`S(w, "verified")` would stringify null to "" and false to "False", and the dashboard
    would then see one value where there are three. The raw object has to pass through."""
    src = io.open("ui/FleetCockpit.cs", encoding="utf-8").read()
    assert 'e["verified"] = w.ContainsKey("verified") ? w["verified"] : null;' in src
    assert 'e["verified"] = S(w, "verified")' not in src, "the tri-state was flattened to a string"


# ═══════════════════════════════════════════════════════════════════════════════════════
# 2026-09-09 -- codex-plan item 1 ("実行から検証までの計測を、まず1件成立させる").
#
# run_id (above) names a fleet SWEEP, shared by every worker in it -- it was never meant to
# answer "what happened to the goal I admitted". jid names the ADMITTED GOAL: minted once by
# task_router.py at submission, and threaded here through add_goal_to_live_fleet ->
# goals_from_command -> Worker.jid -> both snapshot builders -> both cockpit archive sites.
# It is what lets .fleet/tasks/done/<jid>.json (admission), .fleet/acks/<jid>.ack
# (delivery -- NOT .fleet/acked/, which this comment used to name and which has no writer
# anywhere in the tree), and a history.json row's verified/verify_attempts (this file's subject)
# join on ONE id -- the plan's exact evidence bar.
# ═══════════════════════════════════════════════════════════════════════════════════════

def test_a_bare_goal_has_no_jid():
    """Absence is meaningful, not a bug: a goal that never passed through admission (a bare
    -g flag, an ad-hoc retry) has nothing to join to and must not be handed a minted one."""
    w = RelayWorker("plain string goal", "w0")
    assert w.jid is None


def test_a_goal_dict_carries_its_jid_onto_the_worker():
    w = RelayWorker({"text": "do the thing", "jid": "abc123def456"}, "w0")
    assert w.jid == "abc123def456"


def test_jid_survives_from_admission_through_to_the_worker(tmp_path, monkeypatch):
    """The exact chain the plan's evidence bar names: add_goal_to_live_fleet (admission's
    write) -> read_commands -> goals_from_command (the worker's read). No fleet is actually
    started here -- this is the SEAM between the two halves, tested the way this project
    tests seams after being burned twice by ones nothing covered (gate_verdict/verdict,
    keep/kept)."""
    import relay.task_router as tr
    monkeypatch.setattr(tr, "FLEET_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tr, "TASKS", str(tmp_path / "tasks"))
    tr.ensure_dirs()
    add_goal_to_live_fleet("do the thing", str(tmp_path), jid="jid-e2e-001")
    goals = [g for c in read_commands(str(tmp_path)) for g in goals_from_command(c)]
    assert len(goals) == 1
    assert goals[0]["jid"] == "jid-e2e-001"
    w = RelayWorker(goals[0], "w0")
    assert w.jid == "jid-e2e-001"


def test_the_snapshot_publishes_jid():
    src = io.open("relay/fleet_runner.py", encoding="utf-8").read()
    assert '"jid": getattr(w, "jid", None) or ""' in src, "the live snapshot no longer names the goal"


def test_the_final_return_dict_also_carries_jid():
    """Same defect class run_id already had: a dict built once after the sweep exits, read
    only by the FINAL snapshot -- the one on disk exactly when the cockpit archives every
    terminal worker at once. Missing here means a goal that finished right as the run ended
    would carry jid in status.json but not in the archived history row."""
    src = io.open("relay/relay_fleet.py", encoding="utf-8").read()
    assert '"jid": getattr(w, "jid", None) or ""' in src
    src2 = io.open("relay/fleet_runner.py", encoding="utf-8").read()
    assert '"jid": r.get("jid", "")' in src2


def test_no_checks_configured_is_not_the_same_as_verification_failed():
    """The bug this fix closes: a DONE claim with no acceptance checks used to set
    verified=False -- indistinguishable, in every downstream reader, from a check that
    actually ran and failed. __init__'s own comment already declared the contract this
    restores: 'None=not checked, True/False after a gate ran'. Measured 2026-09-09: 67 of 68
    verified=False rows in history.json had verify_attempts=0 -- this exact branch, not a
    failed gate, produced almost all of them."""
    w = RelayWorker("no checks on this goal", "w0")
    assert w.checks == []
    assert w.verified is None                 # the documented initial state
    w._on_done_claimed()
    assert w.verified is None, (
        "a DONE claim with no configured checks was recorded as a FAILED verification")
    assert w.verify_attempts == 0, "no gate ran, so nothing should have been attempted"


def test_a_real_verification_failure_is_still_false_not_none():
    """The other half: this fix must not blur a genuine failure back into 'unknown'. A worker
    WITH checks that actually fail must still read False after a poll cycle, distinctly from
    both True and the no-checks None case above."""
    w = RelayWorker({"text": "goal with a check", "checks": [{"kind": "python",
                     "code": "assert False"}]}, "w0")
    assert w.checks
    w._on_done_claimed()
    assert w.status == "verifying"
    # _on_done_claimed -> _advance_check already started the first pending check, so
    # _active_check is set. Replace it with a fake whose poll() reports a failure without
    # actually running a subprocess -- _poll_verify is the real per-tick driver the round-
    # robin loop calls; this is the same seam test_fleet_verify.py's own tests drive.
    assert w._active_check is not None, "no check was started for a non-empty checks list"
    w._active_check = type("FakeCheck", (), {
        "poll": staticmethod(lambda: (False, "assert failed"))})()
    w._poll_verify()
    assert w.verify_attempts == 1
    assert w.verified is False, "a check literally reporting failure was not recorded as False"
