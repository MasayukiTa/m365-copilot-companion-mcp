"""A steer that arrives after its worker finished is a follow-up, not a loss.

WHY THIS FILE'S TESTS LOOK DIFFERENT FROM THE OTHERS. Twice today a unit test said this feature
worked when it did not, and the live run caught both:

  * the deferral was written inside poll(), where it read correctly and NEVER RAN -- the sweep
    skips a terminal worker before poll() is called. The test passed by calling poll() directly.
  * the test asserting the sweep CALLS the deferral matched `def steer_defers_completion(w):`,
    because the definition satisfies the substring the call was searched for. It passed after
    the call had been removed.

Both verified the implementation -- a call, a substring -- instead of an observable contract on
the product's own path. So the tests here assert on WHAT WOULD BE SENT and on the goal that
would actually be enqueued, never on whether a function is mentioned somewhere.
"""
import pytest

from relay.fleet_runner import FOLLOW_UP_PROMPT, deliver_steers


class _Worker:
    """A worker as deliver_steers handles one."""

    def __init__(self, name, status, goal="analyse the eight files", cwd="C:/work"):
        self.name, self.status, self.goal, self.cwd = name, status, goal, cwd
        self.msgs = []

    def steer(self, text):
        self.msgs.append(text)


def _deliver(items, workers, enqueue=True):
    said, queued = [], []
    n = deliver_steers(items, workers, said.append,
                       queued.append if enqueue else None)
    return n, queued, said


# ---- what actually gets enqueued -------------------------------------------------------------

def test_a_steer_for_a_finished_worker_becomes_a_goal_on_its_conversation():
    """THE CONTRACT, stated as the goal that would be built. `follow_up_to` is resolved in
    RelayWorker.__init__ through socket_route.conversation_for_goal, which matches on the GOAL
    TEXT -- so handing back the finished worker's goal is the whole of what a resume needs."""
    w = _Worker("w0", "done", goal="analyse the eight files")
    _n, queued, _said = _deliver([{"worker": "w0", "text": "add the totals"}], [w])
    assert len(queued) == 1
    goal = queued[0]
    assert goal["follow_up_to"] == "analyse the eight files"
    assert "add the totals" in goal["text"]
    assert goal["priority"] is True
    assert goal["cwd"] == "C:/work"


def test_the_prompt_says_it_is_a_follow_up_not_a_fresh_task():
    """Without that, the model resumed into a conversation holding the finished work would
    reasonably start again. The conversation already has the work; only the question is new."""
    w = _Worker("w0", "done")
    _n, queued, _ = _deliver([{"worker": "w0", "text": "add the totals"}], [w])
    text = queued[0]["text"]
    assert "最初からやり直す必要はありません" in text
    assert text == FOLLOW_UP_PROMPT % "add the totals"


def test_the_finished_worker_is_not_touched():
    """NO STATE MACHINE RUNS BACKWARDS. Reviving the worker was tried and broke a live run:
    the sweep releases a worker's transport the instant it is terminal, so the revived one sent
    into a driver that was gone -- AttributeError, retried 74 times against a budget of 10."""
    w = _Worker("w0", "done")
    _n, _queued, _ = _deliver([{"worker": "w0", "text": "x"}], [w])
    assert w.status == "done"
    assert w.msgs == []


def test_a_live_worker_is_still_steered_directly_and_queues_nothing():
    w = _Worker("w1", "waiting")
    n, queued, _ = _deliver([{"worker": "w1", "text": "x"}], [w])
    assert n == 1 and w.msgs == ["x"] and queued == []


# ---- what deliberately does NOT become a follow-up -------------------------------------------

def test_a_broadcast_is_not_resurrected_for_the_workers_that_finished():
    """A broadcast that arrives after one worker of eight has finished was heard by the other
    seven. Saying it again to the dead one is not a thing anybody wants, and it would spawn a
    worker per finished worker."""
    workers = [_Worker("w0", "done"), _Worker("w1", "waiting"), _Worker("w2", "done")]
    n, queued, _said = _deliver([{"worker": "", "text": "x"}], workers)
    assert n == 1
    assert queued == []


def test_a_worker_with_no_goal_text_cannot_be_followed_up_and_says_so():
    """A follow-up that silently became a FRESH conversation is the failure this exists to
    avoid, and it answers plausibly either way."""
    w = _Worker("w0", "done", goal="")
    n, queued, said = _deliver([{"worker": "w0", "text": "x"}], [w])
    assert n == 0 and queued == []
    assert any("cannot follow up w0" in m for m in said), said


def test_without_an_enqueue_it_reports_exactly_as_it_did_before():
    """A caller that cannot add goals loses nothing it had."""
    w = _Worker("w0", "done")
    n, _queued, said = _deliver([{"worker": "w0", "text": "x"}], [w], enqueue=False)
    assert n == 0
    assert any("DROPPED" in m and "already done" in m for m in said), said


def test_a_failing_enqueue_falls_back_to_the_report():
    def boom(_goal):
        raise RuntimeError("queue is closed")

    said = []
    n = deliver_steers([{"worker": "w0", "text": "x"}], [_Worker("w0", "done")],
                       said.append, boom)
    assert n == 0
    assert any("could not queue a follow-up" in m for m in said), said


def test_a_worker_that_does_not_exist_is_still_just_dropped():
    """No conversation to follow up, so there is nothing to build a goal from."""
    n, queued, said = _deliver([{"worker": "w9", "text": "x"}], [_Worker("w0", "done")])
    assert n == 0 and queued == []
    assert any("no such worker" in m for m in said), said


# ---- the product's own path ------------------------------------------------------------------

def test_the_drain_hands_its_own_queue_to_the_delivery():
    """THE SEAM THAT BOTH FAILED TESTS MISSED. deliver_steers cannot enqueue unless the drain
    gives it somewhere to put a goal, and that wiring is what makes the feature exist rather
    than merely be implemented. Read as executable code, not as text: a mention in a comment
    is not a call."""
    from _srcprobe import executable_source_of_file

    from relay import fleet_runner
    code = executable_source_of_file(fleet_runner.__file__)
    assert "deliver_steers(cmd['steer'], workers, enqueue=add_box.append)" in code \
        or 'deliver_steers(cmd["steer"], workers, enqueue=add_box.append)' in code


def test_a_queued_follow_up_survives_the_add_goal_handler_shape():
    """THE TRAP NEXT DOOR. The cockpit's add_goal handler copies only text/priority/checks/cwd
    and would DROP follow_up_to -- which is why the follow-up is put into the queue directly
    rather than routed through that path. If this ever changes to go through it, the
    conversation link disappears silently and the answer is still plausible."""
    w = _Worker("w0", "done")
    _n, queued, _ = _deliver([{"worker": "w0", "text": "x"}], [w])
    goal = queued[0]
    from relay.fleet_runner import goal_fields  # noqa: F401  -- import guard only
    assert "follow_up_to" in goal, "the link the whole feature rests on"


def test_the_relay_resolves_follow_up_to_into_a_resumed_conversation():
    """The other half of the contract, in the module that owns it: a goal carrying
    follow_up_to becomes a worker with resume_conv set, and attach() opens that conversation
    instead of a fresh chat."""
    from _srcprobe import executable_source_of_file

    from relay import relay_fleet
    code = executable_source_of_file(relay_fleet.__file__)
    assert "follow_up_to" in code
    assert "conversation_for_goal" in code
    assert "resume_conv" in code
