"""Every turn that may use a tool goes out through one function, or the password is lost.

The unlock machinery -- the proactive preflight and the reactive retry -- lives in
_run_one_turn and nowhere else. _drain_pending_queue called _send_and_stream_once directly,
so every queued input went out past all of it: steering typed during a /goal run, and every
promoted idle /send. An instruction typed into main while nothing was running therefore
reached an agent that had never been given the password. That is the "unlock is not read on
a follow-up" the operator reported, and it is a consequence of the responsibility living in
"what callers remember to do" rather than in one place.

The drain also carried a weaker copy of the consent handling that helper already owns, and
skipped the turn timestamp the tool probe reads. A second way to send a turn is a second set
of things to remember, and this one had already forgotten three.

Source-level, like the sibling bridge suites.

Run: pytest -q bridge/test_one_turn_gate.py
"""
from pathlib import Path

SOURCE = (Path(__file__).with_name("copilot_bridge.py")).read_text(encoding="utf-8")


def _fn(name):
    body = SOURCE[SOURCE.index("    def %s(" % name):]
    return body[:body.index("\n    def ")]


def test_the_unlock_machinery_is_in_one_function():
    body = _fn("_run_one_turn")
    assert "_BRIDGE_UNLOCK_PREFLIGHT_DONE" in body
    assert "BRIDGE_UNLOCK_PREFIX % pw" in body
    assert "_bridge_should_auto_unlock" in body


def test_queued_input_goes_through_it():
    body = _fn("_drain_pending_queue")
    assert "self._run_one_turn(sid, item, stream_out=False)" in body
    # The comment explains what it used to call; the CODE must not call it.
    code = chr(10).join(l.split("#")[0] for l in body.splitlines())
    assert "_send_and_stream_once" not in code


def test_the_consent_sentinel_is_handled_rather_than_persisted():
    """_run_one_turn returns a dict when a card could not be auto-approved. Persisting that
    would write the sentinel into the transcript as if it were an answer."""
    body = _fn("_drain_pending_queue")
    assert "isinstance(final, dict)" in body
    i = body.index("isinstance(final, dict)")
    assert "continue" in body[i:i + 400]


def test_one_bad_item_does_not_abandon_the_queue():
    body = _fn("_drain_pending_queue")
    assert "except Exception:" in body


def test_the_only_other_direct_sender_is_the_uncontaminated_critic():
    """The critic must NOT get session bookkeeping or an unlock: it judges text against a
    rubric, uses no tools, and seeing the working conversation would defeat its purpose. Every
    other direct call would be a turn that quietly lacks the password."""
    lines = [(i, l) for i, l in enumerate(SOURCE.splitlines())
             if "self._send_and_stream_once(" in l]
    turn_body = _fn("_run_one_turn")
    outside = []
    for i, l in lines:
        if l.strip() in [x.strip() for x in turn_body.splitlines()]:
            continue
        outside.append((i + 1, l.strip()[:60]))
    # the rubric prompt and its JSON-only retry, and nothing else
    assert len(outside) == 2, outside


def test_the_critic_says_why_it_bypasses():
    """So the next reader does not 'fix' it into the shared path and contaminate it."""
    assert "Uses _send_and_stream_once(..., stream_out=False) directly" in SOURCE
    assert "uncontaminated" in SOURCE
