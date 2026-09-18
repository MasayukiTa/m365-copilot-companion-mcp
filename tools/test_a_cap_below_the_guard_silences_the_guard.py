# -*- coding: utf-8 -*-
"""Two silent truncations into durable records, found by sweeping the class rather than waiting.

WHY A SWEEP. The same failure class was tripped three times on 2026-09-18 -- a 4,000-character
cap destroyed the one ChatHub frame that could have served as a template, a 400-character cap
produced a three-field transcription of a six-field request (and three probes built against the
fragment, each reported as an answer), and a 60-character cap ended a job's provenance
mid-sentence. Fixing the three sites I had tripped over is not sweeping a class, so the rest of
the repository was searched for the same shape.

THE WORSE OF THE TWO IT FOUND, and it is worse than the three above.

    memory_save(f"relay.{run_id}.turn{turn}", resp[:4000], scope="relay", ...)

That store exists for cross-session recall, so a later session reading relay.<id>.turnN gets a
response cut mid-thought and no way to know. And tools/memory_ops.memory_save ALREADY REFUSES a
value over MAX_VALUE_CHARS with an explicit error -- which is the right behaviour, and exactly
the pattern the other fixes reach for. Pre-cutting to 4,000 meant that refusal could never
fire: a caller that shortens data to stay under a limit it will never reach has replaced a loud
refusal with a quiet loss. The bound is now derived from the store's own limit, so the guard is
what binds, and a cut announces itself.

THE SECOND was in code written the same day to fix a different instance of this class:
_record_undelivered wrote the undelivered message at [:2000], silently, into the file whose
entire purpose is to say WHAT was lost.

NOT EVERYTHING SHORTENED IS A DEFECT, and the sweep's verdicts are kept here so the distinction
survives: `job_excerpt` / `response_excerpt` name themselves partial; a `Substring` that appends
an ellipsis for a label is announcing it; a hexdigest prefix is a naming scheme, not data loss;
and a console line a human reads once is not a record. What matters is a durable record, or a
value another program parses, cut without saying so.
"""
from __future__ import annotations

import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code(rel):
    """Source with comment lines dropped -- these files explain the mistake they fixed, and a
    text search cannot tell an explanation from the thing explained. Three checks in this
    repository tripped on their own prose in one day."""
    src = io.open(os.path.join(REPO, rel), encoding="utf-8", errors="replace").read()
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


# ---- the turn response kept for cross-session recall --------------------------------------

def test_the_turn_response_is_no_longer_cut_to_a_fixed_4000():
    code = _code("relay/copilot_autopilot_relay.py")
    assert "resp[:4000]" not in code, "the silent 4,000-character cut is back"
    assert "_MEMORY_TURN_CHARS" in code


def test_the_bound_comes_from_the_store_rather_than_being_picked():
    """A number chosen by hand drifts from the limit it was meant to respect. Derived, so
    raising the store's limit raises this with it."""
    code = _code("relay/copilot_autopilot_relay.py")
    assert re.search(r"_MEMORY_TURN_CHARS\s*=\s*_MEMORY_MAX_CHARS\s*-\s*\d+", code), \
        "the bound is no longer derived from memory_ops.MAX_VALUE_CHARS"


def test_the_bound_leaves_room_for_the_marker_so_the_guard_never_refuses():
    """The point of deriving it: head plus marker must still fit, or the fix trades a silent
    cut for a rejected save."""
    from tools.memory_ops import MAX_VALUE_CHARS

    import relay.copilot_autopilot_relay as CAR

    assert CAR._MEMORY_TURN_CHARS < MAX_VALUE_CHARS
    marker = "\n[cut: kept %d of %d characters]" % (CAR._MEMORY_TURN_CHARS, 10 ** 9)
    assert CAR._MEMORY_TURN_CHARS + len(marker) <= MAX_VALUE_CHARS, \
        "a cut value plus its marker would be refused by memory_save"


def test_a_cut_turn_says_how_much_was_kept():
    code = _code("relay/copilot_autopilot_relay.py")
    assert "[cut: kept %d of %d characters]" in code, \
        "the cut no longer names the kept and the original length"


def test_the_guard_it_was_hiding_still_refuses_an_oversized_value():
    """The behaviour the pre-cut was silencing. Worth pinning: it is the reason the fix is a
    derived bound rather than a bigger number."""
    from tools.memory_ops import MAX_VALUE_CHARS, memory_save_local

    out = memory_save_local("t.oversized", "x" * (MAX_VALUE_CHARS + 1), scope="relay")
    assert "error" in out.lower() and str(MAX_VALUE_CHARS) in out, out


# ---- the undelivered message ---------------------------------------------------------------

def test_the_undelivered_message_records_its_real_length():
    """The file exists to say WHAT was lost. A silently shortened copy answers a different
    question."""
    code = _code("bridge/copilot_bridge.py")
    assert '"text": str(text or "")[:2000]' not in code, "the silent 2,000 cut is back"
    assert '"text_len": len(_text)' in code, \
        "the row no longer records the message's real length"
    assert "_UNDELIVERED_TEXT_CHARS" in code


def test_the_undelivered_cut_announces_itself():
    code = _code("bridge/copilot_bridge.py")
    assert "[cut: kept %d of %d characters]" in code
