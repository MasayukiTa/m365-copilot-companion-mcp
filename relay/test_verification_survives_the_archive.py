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

from relay.fleet_runner import _run_id_of


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


@pytest.mark.parametrize("field", ["verified", "verify_attempts", "run_id"])
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
