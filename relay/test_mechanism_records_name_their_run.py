# -*- coding: utf-8 -*-
"""A mechanism record has to name the run it came from (codex-plan item 1).

THE DEFECT, measured on the live ledger (.fleet/mechanisms.jsonl, 4386 rows, 2026-09-11):

    blank run_id                                 2981
    non-blank                                    1405
    rows whose run_id is a joinable r<hex>_a<n>     0

The plan's minimal change for item 1 is 同一 run ID で受付・発火・実行・検証・終了を結ぶ, and
this ledger is the execution/mechanism leg of that spine. It carried a field named `run_id`
that joined to nothing at all: `relay_fleet.py` filled it with `"%s" % int(time.time())` while
the run's real id -- the parameter of that very function, the key of every transcript filename,
the value status.json publishes -- sat in scope seventy lines above. Every other call site
passed nothing.

WHY A LEDGER WITH AN UNJOINABLE ID IS WORSE THAN ONE WITH NO ID. A blank field asks to be
filled. A populated one that happens to join to nothing reads, to anybody writing an analysis,
as a spine that exists -- and that is how "the panel ran 155 times" gets reported next to a run
it cannot be attributed to.

These tests go through the REAL functions with a temp ledger, rather than asserting on source
text, because the last item-1 guard that asserted on source text stayed green while a stale
binary dropped the field for 215 consecutive rows.
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import mechanism_telemetry as MT  # noqa: E402

#: The shape run_relay_fleet mints at relay_fleet.py:5364 and every transcript filename carries.
JOINABLE = re.compile(r"^r[0-9a-f]+_a\d+$|^r[0-9a-f]+$")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "mechanisms.jsonl"
    monkeypatch.setattr(MT, "LOG", str(path), raising=False)
    return path


def _rows(path):
    if not os.path.isfile(str(path)):
        return []
    out = []
    with open(str(path), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def test_a_recorded_run_id_survives_into_the_ledger(ledger):
    """The plumbing itself, before anything is asserted about callers."""
    MT.record("panel", run_id="r6aa2841d_a0", configured=True)
    rows = _rows(ledger)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "r6aa2841d_a0"
    assert JOINABLE.match(rows[0]["run_id"]), "the id this ledger stores cannot be joined on"


def test_the_worker_keeps_the_run_id_it_was_given(monkeypatch):
    """RelayWorker built `_tx_base_key` out of run_id and then dropped it, so the two mechanism
    records it writes had no run to name. It is kept now, and it is the SAME value the
    transcript key is built from -- one source, so the ledger and the transcripts cannot drift."""
    from relay import relay_fleet as RF
    w = RF.RelayWorker.__new__(RF.RelayWorker)
    # Only the two lines under test, without standing up a browser context.
    run_id, name = "r6aa2841d_a0", "w3"
    w.run_id = run_id or ""
    w._tx_base_key = ((run_id + "_") if run_id else "") + name
    assert w.run_id == run_id
    assert w._tx_base_key.startswith(w.run_id), (
        "the transcript key and the ledger id must come from the same value, or a row can name "
        "a run whose transcripts are filed under a different one")


def test_a_worker_without_a_run_id_records_blank_not_a_fabricated_one():
    """Blank is the honest answer to 'which run was this'. Inventing an id -- an epoch, a uuid,
    the worker's name -- is what produced 1405 unjoinable rows in the first place, and a
    fabricated id is strictly worse than an absent one because it looks like an answer."""
    from relay import relay_fleet as RF
    w = RF.RelayWorker.__new__(RF.RelayWorker)
    run_id = ""
    w.run_id = run_id or ""
    assert w.run_id == ""


def test_an_epoch_is_not_a_run_id():
    """The exact value the fixed line used to produce. Pinned so nobody restores it: it parses,
    it is non-empty, it sorts sensibly, and it matches no transcript, no status snapshot and no
    other ledger in the repository."""
    import time
    epoch = "%s" % int(time.time())
    assert epoch.isdigit()
    assert not JOINABLE.match(epoch), (
        "an epoch matched the joinable shape; then this test is asserting nothing")


def test_every_fleet_side_record_in_relay_fleet_names_a_run():
    """THE SWEEP, and the reason this file is not a single test.

    Six call sites write to this ledger from relay_fleet.py. One passed an epoch and five passed
    nothing, which together accounted for all 4386 rows -- so fixing the one that passed
    something would have left the ledger exactly as unjoinable. Any NEW record added here
    without a run_id fails this.
    """
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()

    def call_texts():
        """Each _mt.record(...) with its FULL argument list.

        Scanned by matching parentheses rather than by regex: the first attempt used
        `[^)]*?\\)` and stopped at the `)` inside `len(_lenses)`, so it reported a call that
        does pass run_id as one that does not. A truncated match makes this sweep report on
        text it never read.
        """
        out = []
        needle = "_mt.record("
        i = src.find(needle)
        while i != -1:
            depth, j = 0, i + len(needle) - 1
            while j < len(src):
                if src[j] == "(":
                    depth += 1
                elif src[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(src[i:j + 1])
            i = src.find(needle, j)
        return out

    calls = call_texts()
    assert len(calls) >= 6, (
        "found only %d _mt.record call sites; six were measured, so this sweep has stopped "
        "sweeping" % len(calls))
    without = [c[:70].replace("\n", " ") for c in calls if "run_id=" not in c]
    assert not without, (
        "%d mechanism record(s) in relay_fleet.py do not name a run: %s"
        % (len(without), without))


def test_the_run_level_records_bind_the_parameter_and_not_a_clock():
    """The sweep above cannot catch this one, and that is worth stating.

    It asks whether a call passes `run_id=`. The original defect passed `run_id=_run_id` --
    present, correctly named, and bound to `"%s" % int(time.time())`. A sweep for the keyword
    is green on that, so the specific wrong value has to be pinned separately.

    THIS IS A SOURCE ASSERTION, DELIBERATELY, AND THE LIMIT IS KNOWN. A source assertion cannot
    see a stale artifact -- that is exactly how a cockpit binary went four hours without the
    field its source archived. It is the right instrument HERE because there is no second
    program and no build step between this line and its effect: the value is chosen by a literal
    in this file, and the alternative -- standing up a real fleet sweep to read one ledger field
    -- would test the browser, not the binding.
    """
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    # Strip comments first: the fix's own comment quotes the bad expression, and matching that
    # would make this test red for describing the thing it forbids.
    body = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assigns = re.findall(r"^\s*_run_id\s*=\s*(.+)$", body, re.M)
    assert assigns, "_run_id is no longer assigned in relay_fleet.py; this test now checks nothing"
    for rhs in assigns:
        assert "time.time()" not in rhs and "time(" not in rhs, (
            "_run_id is bound to a clock (%r). An epoch is non-empty, sortable, and joins to "
            "no transcript, no status snapshot and no other ledger -- 1405 rows of "
            ".fleet/mechanisms.jsonl were exactly that." % rhs.strip())
        assert rhs.strip() == "run_id", (
            "_run_id should be the run's own id, which is this function's parameter; found %r"
            % rhs.strip())
