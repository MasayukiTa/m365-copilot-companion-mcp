"""Which goals may go over a socket, and which parts of that answer are allowed to evolve.

THIS IS NOT `routing`, AND THE DIFFERENCE IS A PROPERTY OF THE CODE RATHER THAN OF THE IDEA

`routing` is forbidden because a routing decision selects the harness a task runs under, so an
optimiser pointed at it learns to steer work toward the branch whose checks are laxest -- the
same shape as tuning the grader. A transport decision selects only how the conversation is
carried. The manifest, the graders, the folder policy and the unlock boundary are identical on
both sides, so getting it wrong buys memory, never permission.

THAT HOLDS ONLY WHILE IT REMAINS TRUE. The day anything takes a shortcut on the socket side --
a consent step only the tab performs, a different acceptance rule, a thinner log -- transport
selection becomes routing selection and belongs back under FORBIDDEN. The invariant is written
beside the component in manifest.py so a future promotion argument has to meet it rather than
appeal to this file's name.

WHAT MAY EVOLVE AND WHAT MAY NOT

    thresholds, the eligible-kind list, feature weights    evolvable
    the attachment rule                                    FIXED, and above the version table

The rule behind that split is not caution, it is observability: a mistake may be evolvable
only where a frozen judge sees it. Over-routing to sockets is loud -- `note_failure` records
it, the circuit breaker and the one-way counter close the route, and all of that acts
independently of whatever chose the transport. The classifier sits BEHIND a safety layer it
does not control.

A SILENT ERROR IS THE ONE THAT MUST NOT BE EVOLVABLE, and that argument stands. It was
originally made about Work IQ: a socket conversation lacking M365 context could return a
plausible answer, no fallback would fire, the goal would reach DONE, and the label would record
"the socket was fine" while the answer was built without the data it needed. Errors nobody can
see are exactly how "learn to under-predict" opens, undetectably.

THE PREMISE UNDER THAT EXAMPLE WAS MEASURED AND IS FALSE (2026-08-21). Work IQ rides the
socket. Twenty socket turns across eight request classes -- tool calls, arithmetic, image
generation, external web search, an unlock-gated file write, inbox reads, calendar reads and a
calendar WRITE -- produced zero fallbacks. And the check was built so plausibility could not
pass it: both transports were asked about a calendar entry created earlier that same day and
verified independently, and BOTH named it. A route without Work IQ cannot invent an entry it
has never seen.

So the fear was right and the target was wrong. What survives as FIXED is the one property
measured to force a tab, and it is not a guess about text at all: an ATTACHMENT. The Analyst
puts a local file into a real <input type=file>, and a socket has nowhere to put a file. It is
knowable from a parameter the caller already set, so it needs no classifier and can never be
wrong in the silent direction.

It sits ABOVE the version table rather than inside a version, because it is not a policy. A
version table exists to let two opinions be compared; there is no second opinion about where a
file can be put.

FALLBACKS ARE NOT ALL LABELS

A fallback means a tab was needed. It does not mean the CLASSIFIER was wrong. A token that
expired, a capture that failed, an endpoint that dropped -- those are the route's problems and
say nothing about the goal, and training on them teaches "tasks at this hour need tabs".
Only task-caused fallbacks are evidence about a classification.
"""
from __future__ import annotations

import re

TAB, SOCKET = "tab", "socket"

#: FIXED. Not read from any genome, and `evolvable_fields()` refuses to return it.
#:
#: The whole rule. Measured across twenty socket turns and eight request classes as the only
#: property that structurally forces a tab -- see the module note for why the previous rule
#: (anything mentioning an M365 surface) was removed rather than narrowed.
ATTACHMENT = "attachment: a socket has nowhere to put a local file"


def needs_tab(upload_path: str = "") -> bool:
    """True when this task cannot be carried by a socket, whatever any policy prefers.

    Takes the caller's own parameter, NOT the goal text. Reading the text is what the removed
    rule did, and the measurement says the text carries no signal about this: goals that name
    mail, calendars and SharePoint were carried by the socket, with ground truth to prove it.
    """
    return bool(upload_path)


#: A fallback reason matching any of these is the ROUTE's failure, not the goal's. Kept as
#: patterns rather than exact strings because they are produced by several layers.
ROUTE_CAUSED = (
    r"token", r"capture", r"unauthor", r"401", r"403",
    r"connectionclosed", r"websocket", r"timeout", r"handshake",
    r"refresh", r"expired", r"ChatHubError: this socket route already failed",
    # ADDED FROM A REAL FALLBACK, 2026-08-21: "turn deadline exceeded before a completion
    # frame". It classified as `unknown`, which was correct -- nobody had read it yet. Now
    # somebody has: it is the socket's own budget running out, and nothing about the goal
    # (reviewing an invoice script) made it slow. Left in the ROUTE list so a classifier is
    # never taught "invoice reviews need tabs" from a clock.
    r"deadline exceeded", r"went silent",
)

#: A fallback reason matching one of THESE is about the goal: the socket could carry the
#: conversation and the conversation needed something a tab provides.
TASK_CAUSED = (
    r"carried no text", r"card", r"consent", r"attachment", r"添付", r"許可",
)


def classify_fallback(reason: str) -> str:
    """'route', 'task' or 'unknown' -- and unknown is NOT folded into either.

    An unclassified reason is a reason nobody has read yet. Counting it as route-caused would
    quietly exonerate the classifier; counting it as task-caused would quietly train on noise.
    It is left as its own value so a human can look at what accumulates there and extend the
    lists, which is how the fixed predicate gets maintained.
    """
    text = (reason or "").lower()
    for pattern in TASK_CAUSED:
        if re.search(pattern, text, re.IGNORECASE):
            return "task"
    for pattern in ROUTE_CAUSED:
        if re.search(pattern, text, re.IGNORECASE):
            return "route"
    return "unknown"


def _policy_v1(goal: str, *, kind="", knobs=None, explore=False) -> str:
    """Whatever the route offers. THE BEHAVIOUR THAT WAS ALREADY THERE.

    The first draft of this returned TAB for everything, on the reasoning that tabs are what
    the fleet did before sockets existed. That was wrong and a test of the socket route caught
    it within the minute: the route IS here now, gated by its own switch, so a default of
    "always tab" does not preserve the status quo -- it silently disables a feature.

    A component's v1 has to be what happens today, or promoting the component changes
    behaviour for everyone who never asked for the experiment.
    """
    return SOCKET


def _policy_v2(goal: str, *, kind="", knobs=None, explore=False) -> str:
    """Sockets for goals the FIXED predicate clears and the evolvable list admits.

    `explore` is how the one-sided-label problem is answered. A classifier is only ever told
    it was wrong about goals it sent to a socket; goals it sent to a tab return no evidence at
    all, so it cannot learn that a tab was unnecessary and it drifts toward whatever it
    already believes. A small fraction of tab-predicted goals therefore go over a socket
    anyway -- which is safe HERE and almost nowhere else, because a wrong transport costs a
    fallback rather than a wrong answer.

    The exploration decision is the caller's, not this function's: a policy that reached for a
    random number would return different answers for one goal on two runs, and a component
    whose two arms are not reproducible is not measurable.
    """
    knobs = knobs or {}
    eligible = knobs.get("transport_eligible_kinds")
    if eligible is not None and kind and kind not in set(eligible):
        return SOCKET if explore else TAB
    return SOCKET


#: The version table. Until this existed, `transport` was a name a genome could carry and
#: nothing read -- the defect this repository has now found in four separate components.
TRANSPORT_VERSIONS = {
    "transport/v1": _policy_v1,
    "transport/v2": _policy_v2,
}


def evolvable_fields() -> tuple:
    """The knobs a genome may move. The attachment rule is deliberately absent."""
    return ("transport_eligible_kinds", "transport_explore_rate")


def choose(goal: str, *, kind="", knobs=None, explore=False, upload_path="") -> str:
    """The transport for one goal, under whichever version the active harness names.

    THE STRUCTURAL RULE IS APPLIED FIRST, so no version -- present or future, hand-written or
    evolved -- can send an attachment over a socket. A version that could would not be a worse
    policy; it would be a broken one.
    """
    if needs_tab(upload_path):
        return TAB
    try:
        from relay.selfimprove import runtime_config as _rc
        impl = TRANSPORT_VERSIONS.get(_rc.component("transport"), _policy_v1)
    except Exception:
        impl = _policy_v1
    return impl(goal, kind=kind, knobs=knobs, explore=explore)


# ------------------------------------------------------------------------------------------
# Was the turn already delivered when the socket gave up?
# ------------------------------------------------------------------------------------------
#
# THE FALLBACK RE-SENDS THE TURN VERBATIM, AND WHETHER THAT IS SAFE DEPENDS ON THE REASON.
#
# `_fall_back_to_tab` says "THE GOAL IS NOT AFFECTED... the turn that was lost is re-sent". That
# holds when the turn never reached the server. It does not hold when the turn arrived, the
# model acted on it, and only the ANSWER was unusable -- "the turn completed but carried no
# text" is exactly that case, and re-sending it asks the model to do the work a second time.
# For a goal that writes a file the repeat is invisible; for one that sends a message, books
# something, or appends to a record, it is a second real-world act.
#
# This does not decide whether to re-send. It makes the ambiguity nameable and countable, so a
# duplicate is something the record can show rather than something nobody thought to look for.
# Changing the re-send behaviour on the strength of zero observed fallbacks would be trading a
# known cost -- a lost turn -- for a hazard nobody here has measured.

#: Reasons where the turn demonstrably reached the server before the failure.
DELIVERED = (
    r"carried no text", r"card", r"consent", r"empty answer", r"no DONE",
)

#: Reasons where the failure happened before anything could have been sent.
NOT_DELIVERED = (
    r"token", r"unauthor", r"401", r"403", r"expired", r"refresh",
    r"capture", r"handshake", r"already failed",
)


def delivery_status(reason: str) -> str:
    """'delivered', 'not_delivered' or 'unknown' for the turn that was being sent.

    UNKNOWN IS THE HONEST MAJORITY AND IS NOT FOLDED EITHER WAY. A connection that dropped
    mid-flight may or may not have delivered the frame; calling that "not delivered" would
    quietly certify the re-send as safe, and calling it "delivered" would inflate a duplicate
    count with events that never duplicated anything. It is left as its own value so what
    accumulates there can be read by a person.
    """
    text = (reason or "").lower()
    for pattern in DELIVERED:
        if re.search(pattern, text, re.IGNORECASE):
            return "delivered"
    for pattern in NOT_DELIVERED:
        if re.search(pattern, text, re.IGNORECASE):
            return "not_delivered"
    return "unknown"


def duplicate_risk(reason: str) -> bool:
    """True when re-sending this turn could repeat an act the model already performed."""
    return delivery_status(reason) in ("delivered", "unknown")
