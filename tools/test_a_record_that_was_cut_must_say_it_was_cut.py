# -*- coding: utf-8 -*-
"""Two fields in a job's archive record, both found broken by reading one real archived job.

FOUND BY LOOKING AT AN ACTUAL FILE, NOT BY A FAILING TEST. `.fleet/tasks/done/<id>.json` for a
goal submitted at 03:33 on 2026-09-18 was opened to answer "who submitted this?", and it
answered -- and two other things in the same twelve lines were wrong:

  * `created` was absent, so the job read as created at 1970-01-01. Every question of the form
    "how long did this wait before anything picked it up" is unanswerable from the archive, and
    the pending file that held `created` is deleted at the moment the done record is written.
    run_job already rescues `origin` for exactly this reason, with a comment saying "a
    distinction that does not reach the audit trail is not a distinction" -- and stopped one
    field short.

  * `origin.source` ended mid-sentence: "LOCAL_LOOP w2 の依頼。実行中のクライアントIP
    20.210.146.129 はローカル書込・実行ツールが". The caller had named both the requesting
    worker and the client IP, and the cap was 60 characters. Nothing recorded that it had been
    cut, so it read as a caller that trailed off rather than as a field too small for its job.

The second is the rule this repository already applies in evidence_trace -- a truncated
argument list is RECORDED AS truncated, because "silently keeping the first 4,000 characters
means a destination named at character 4,001 is not merely missed, it is missed by a check that
then reports nothing wrong".
"""
from __future__ import annotations

import tools.fleet_intake as FI
import relay.task_router as TR


# ---- created survives into the done record ----------------------------------------------

def test_created_reaches_the_done_record():
    """THE DEFECT. Without it the archive can time the finish and not the wait."""
    job = {"id": "abc123", "type": "fleet_goal", "created": 1_700_000_000.0,
           "payload": {"goal": "x"}, "origin": {"via": "mcp", "source": "somebody"}}
    rec = TR.run_job(dict(job), now_ts=1_700_000_500.0)
    assert rec["created"] == 1_700_000_000.0
    assert rec["ts_done"] == 1_700_000_500.0
    # ...which is the whole point: the wait is now computable from the record alone.
    assert rec["ts_done"] - rec["created"] == 500.0


def test_origin_still_survives_alongside_it():
    """The field the same line already rescued must not regress while its neighbour is fixed."""
    job = {"id": "abc124", "type": "fleet_goal", "created": 1.0,
           "payload": {"goal": "x"}, "origin": {"via": "mcp", "source": "somebody"}}
    rec = TR.run_job(dict(job), now_ts=2.0)
    assert rec["origin"] == {"via": "mcp", "source": "somebody"}


def test_a_job_with_no_created_does_not_gain_a_false_one():
    """An absent value is a fact. Filling it in with `now` would make every legacy job look
    like it was created the instant it finished -- a wait of zero, which is a number someone
    would believe."""
    job = {"id": "abc125", "type": "fleet_goal", "payload": {"goal": "x"}}
    rec = TR.run_job(dict(job), now_ts=2.0)
    assert "created" not in rec


# ---- a cut field says it was cut ---------------------------------------------------------

def test_a_source_that_fits_is_untouched():
    assert FI._clean("LOCAL_LOOP w2", 300) == "LOCAL_LOOP w2"
    assert "[cut]" not in FI._clean("LOCAL_LOOP w2", 300)


def test_a_source_that_does_not_fit_says_so():
    """THE DEFECT. The old behaviour returned a sentence that simply stopped."""
    out = FI._clean("x" * 500, 60)
    assert out.endswith("[cut]"), out
    assert len(out) <= 60, "the marker pushed the field over its own limit"


def test_the_real_source_that_was_cut_now_fits():
    """The measured case, at the budget it now has. A source that names the requesting worker
    AND the client IP is not an unusual source -- it is what a real one said."""
    real = ("LOCAL_LOOP w2 の依頼。実行中のクライアントIP 20.210.146.129 は"
            "ローカル書込・実行ツールが使えないため、フリートに委譲する。")
    out = FI._clean(real, FI.MAX_SOURCE_CHARS)
    assert "[cut]" not in out, out
    assert out.endswith("委譲する。"), out


def test_an_address_is_still_redacted_before_anything_else():
    """The cap changed; what _clean exists to remove did not."""
    out = FI._clean("mail from someone@example.com about it", 300)
    assert "someone@example.com" not in out
    assert "<address redacted>" in out
