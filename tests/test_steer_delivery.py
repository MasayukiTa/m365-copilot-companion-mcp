"""A steer must reach somebody, or say why it did not. It did neither.

REPRODUCED, on a live thirteen-worker run driven through the cockpit. A steering message was
typed into the composer, the composer emptied, and the cockpit said "queued for the next turn".
The command file was consumed by the runner -- so the message got that far -- and NOT ONE of
the sixteen newest transcripts carried it.

The break was one line. The cockpit picks the first non-terminal worker it knows about and,
when it finds none, sends an EMPTY worker name; its own comment reads "fall back to empty
string (relay broadcasts to all workers)". Nothing on the other side broadcast:

    w = by_name.get(it.get("worker"))        # by_name.get("") is None
    if w is not None and w.status not in TERMINAL:
        w.steer(...)

so the message was dropped in silence while the surface reported success. A steer that goes
nowhere is worse than an error: the person believes they redirected the work and then watches
it continue in the old direction.
"""
import pytest

from relay.fleet_runner import deliver_steers


class _W:
    def __init__(self, name, status):
        self.name = name
        self.status = status
        self.msgs = []

    def steer(self, text):
        self.msgs.append(text)


def _fleet():
    return [_W("w0", "done"), _W("w1", "waiting"), _W("w2", "refuting"),
            _W("w3", "pending"), _W("w4", "stuck")]


def _run(items, workers=None):
    said = []
    workers = workers if workers is not None else _fleet()
    n = deliver_steers(items, workers, said.append)
    return n, workers, said


# ---- the defect ----------------------------------------------------------------------------

def test_an_empty_worker_name_reaches_every_live_worker():
    """THE ONE THAT WAS BROKEN. The cockpit has always believed this broadcasts, and said so
    in a comment; the relay dropped it instead."""
    n, ws, _said = _run([{"worker": "", "text": "also check X"}])
    got = {w.name for w in ws if w.msgs}
    assert got == {"w1", "w2"}, got
    assert n == 2


def test_a_missing_worker_key_also_broadcasts():
    n, ws, _ = _run([{"text": "also check X"}])
    assert n == 2 and {w.name for w in ws if w.msgs} == {"w1", "w2"}


def test_a_pending_worker_is_not_steered():
    """It has not started, so there is no turn to redirect -- and it will read the goal it was
    given when it does start."""
    _n, ws, _ = _run([{"worker": "", "text": "x"}])
    assert [w for w in ws if w.name == "w3"][0].msgs == []


def test_a_terminal_worker_is_not_steered():
    _n, ws, _ = _run([{"worker": "", "text": "x"}])
    assert [w for w in ws if w.name == "w0"][0].msgs == []
    assert [w for w in ws if w.name == "w4"][0].msgs == []


# ---- every rejection is named ---------------------------------------------------------------

def test_a_steer_for_a_worker_that_does_not_exist_says_so():
    n, _ws, said = _run([{"worker": "w99", "text": "x"}])
    assert n == 0
    assert any("DROPPED" in m and "w99" in m for m in said), said


def test_the_message_lists_the_workers_that_do_exist():
    """A name that does not match is usually a name that nearly matches. Printing the run's
    actual names turns 'it vanished' into 'you meant w1'."""
    _n, _ws, said = _run([{"worker": "W1", "text": "x"}])
    assert any("w0,w1,w2,w3,w4" in m for m in said), said


def test_a_steer_for_a_finished_worker_says_which_and_why():
    n, _ws, said = _run([{"worker": "w0", "text": "x"}])
    assert n == 0
    assert any("DROPPED" in m and "already done" in m for m in said), said


def test_a_broadcast_with_nobody_live_says_so_rather_than_passing():
    """SILENCE IS THE DEFECT, not the dropping. A run whose workers have all finished cannot be
    steered, and the person needs to know that now rather than by watching nothing change."""
    workers = [_W("w0", "done"), _W("w1", "stuck")]
    n, _ws, said = _run([{"worker": "", "text": "x"}], workers)
    assert n == 0
    assert any("DROPPED" in m and "no live worker" in m for m in said), said


def test_empty_text_is_refused_and_named():
    n, _ws, said = _run([{"worker": "w1", "text": "   "}])
    assert n == 0
    assert any("refused" in m and "empty text" in m for m in said), said


def test_a_successful_delivery_is_announced_too():
    """Not only failures. A log that speaks only on error cannot distinguish 'it worked' from
    'the logging is broken', which is how the original defect stayed invisible."""
    _n, _ws, said = _run([{"worker": "w1", "text": "x"}])
    assert any("queued for w1" in m for m in said), said


def test_the_note_says_it_takes_effect_on_the_NEXT_turn():
    """'Interrupt' suggests mid-turn, and it is not. A turn in flight is a request the model is
    already answering; there is nowhere to insert anything until it replies. Saying so is the
    difference between a limitation and a bug report."""
    _n, _ws, said = _run([{"worker": "w1", "text": "x"}])
    assert any("next turn" in m for m in said), said


# ---- shape and robustness -------------------------------------------------------------------

def test_a_single_item_is_accepted_as_well_as_a_list():
    n, ws, _ = _run({"worker": "w1", "text": "x"})
    assert n == 1 and [w for w in ws if w.name == "w1"][0].msgs == ["x"]


def test_a_bare_string_is_accepted():
    n, _ws, _ = _run(["just the text"])
    assert n == 2


def test_one_bad_item_does_not_stop_the_others():
    """A malformed entry in a list must not swallow the steer that follows it."""
    n, ws, said = _run([None, {"worker": "w1", "text": "x"}])
    assert n == 1
    assert [w for w in ws if w.name == "w1"][0].msgs == ["x"]
    assert any("DROPPED" in m for m in said)


def test_delivery_never_raises_whatever_the_worker_does():
    class Angry(_W):
        def steer(self, text):
            raise RuntimeError("worker refused")

    workers = [Angry("w1", "waiting")]
    n, _ws, said = _run([{"worker": "w1", "text": "x"}], workers)
    assert n == 0
    assert any("DROPPED" in m for m in said)


def test_the_runner_calls_it_rather_than_reimplementing_it():
    """The dispatch was inside a nested function, which is why it had no test for the whole
    time it was broken."""
    from _srcprobe import executable_source

    from relay import fleet_runner
    src = executable_source(fleet_runner.deliver_steers)
    assert "TERMINAL" in src
    # THE CALL, not a mention of the name. Read as executable code so a line in a comment
    # cannot satisfy it -- the sibling test that checked a call by substring matched the
    # function's own DEFINITION and passed after the call was deleted.
    from _srcprobe import executable_source_of_file
    code = executable_source_of_file(fleet_runner.__file__)
    assert "deliver_steers(" in code and "enqueue=add_box.append" in code


def test_the_cockpit_no_longer_claims_success_unconditionally():
    """It said 'queued for the next turn' whatever happened, including when it had found no
    worker and sent an empty name."""
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ui", "FleetCockpit.cs"), encoding="utf-8").read()
    i = src.index("RequestSteer(targetWorker, steerText);")
    seg = src[i:i + 1400]
    assert "every live worker" in seg or "全ワーカー" in seg
    assert "targetWorker +" in seg, "the note does not name the worker it went to"


# ---- a message that was queued and never used ---------------------------------------------

def test_a_steer_the_worker_never_used_is_named():
    """MEASURED. A message was delivered to w2 while it was refuting, w2 completed at turn 1,
    and the message was never used -- correctly queued, correctly reported as queued, and
    silently discarded when the worker went terminal. The silence is the defect, not the
    timing: a turn in flight is a request the model is already answering, and there is nowhere
    to insert anything until it replies."""
    from relay.fleet_runner import report_unused_steers

    w = _W("w2", "waiting")
    w.steer_msgs = ["also add the total"]
    said = []
    assert report_unused_steers([w], set(), said.append) == 0, "still running: nothing to say"
    # NOT done, which is deferrable now: the sweep revives a finished worker that still has a
    # message, so announcing a loss there would announce one that has not happened.
    w.status = "cancelled"
    assert report_unused_steers([w], set(), said.append) == 1
    assert any("NEVER USED by w2" in m for m in said), said
    assert any("also add the total" in m for m in said), said


def test_it_is_named_once_and_not_on_every_sweep():
    """At one-second polling a line per sweep is a log nobody reads, which is how the delivery
    bug survived in the first place."""
    from relay.fleet_runner import report_unused_steers

    w = _W("w2", "cancelled")
    w.steer_msgs = ["x"]
    seen, said = set(), []
    for _ in range(5):
        report_unused_steers([w], seen, said.append)
    assert len([m for m in said if "NEVER USED" in m]) == 1


def test_a_worker_that_used_its_steer_is_not_named():
    from relay.fleet_runner import report_unused_steers

    w = _W("w2", "cancelled")
    w.steer_msgs = []
    said = []
    assert report_unused_steers([w], set(), said.append) == 0
    assert said == []


def test_a_worker_without_the_attribute_is_not_an_error():
    from relay.fleet_runner import report_unused_steers

    class Bare:
        name, status = "w9", "done"

    assert report_unused_steers([Bare()], set(), lambda _m: None) == 0


def test_the_sweep_asks_every_tick():
    from _srcprobe import executable_source_of_file

    from relay import fleet_runner
    code = executable_source_of_file(fleet_runner.__file__)
    assert "report_unused_steers(workers, _steer_reported)" in code


# ---- why there is no deferral ---------------------------------------------------------------

def test_a_finished_worker_is_not_revived_by_a_pending_steer():
    """IT WAS TRIED AND IT BROKE A RUN, and the reason has to stay findable.

    A steering message becomes the NEXT turn's job, so a worker that finishes on the turn it
    arrives never speaks it -- measured, five workers out of eight. Reviving such a worker
    looked like the fix. Put inside poll() the check read correctly and never ran, because the
    sweep skips a terminal worker before poll() is called; moved to the sweep it ran, and the
    revived worker's next send reached  on a driver the sweep had already
    released, became an AttributeError, and retried 74 times against a budget of 10.

    Reviving a torn-down worker needs RE-ATTACHMENT. Until that exists, a late steer is
    REPORTED as never used rather than acted on."""
    from _srcprobe import executable_source_of_file
    from relay import relay_fleet
    code = executable_source_of_file(relay_fleet.__file__)
    assert "steer_defers_completion" not in code, (
        "the deferral is back; it needs re-attachment first, see the note at the sweep")


def test_the_reason_stays_where_somebody_would_re_add_it():
    """A comment in a commit message is not where the next person looks."""
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("NO, A PENDING STEER MAY NOT REVIVE")
    j = src.index("if w.status in TERMINAL or w.status == PENDING:", i)
    assert j - i < 1800, "the note has drifted away from the line it explains"
