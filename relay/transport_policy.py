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
    the Work IQ predicate                                  FIXED, in code

The rule behind that split is not caution, it is observability: a mistake may be evolvable
only where a frozen judge sees it. Over-routing to sockets is loud -- `note_failure` records
it, the circuit breaker and the one-way counter close the route, and all of that acts
independently of whatever chose the transport. The classifier sits BEHIND a safety layer it
does not control.

Missing Work IQ is the opposite. A socket conversation without Work IQ context can return a
plausible answer: no fallback fires, the goal reaches DONE, and the label records "the socket
was fine" -- while the answer was formed without the data it needed. Evolving a dimension
whose errors are silent is exactly how "learn to under-predict Work IQ" opens, and it opens
undetectably. So the predicate is code a person maintains, updated from the fallback reasons
this module classifies, not from a fit.

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
#: Anything touching M365 surfaces goes over a tab. The list is deliberately broad: a false
#: "needs Work IQ" costs one tab's memory, and a false "does not" costs an answer built
#: without the data, which nothing here would notice.
WORKIQ_MARKERS = (
    "sharepoint", "onedrive", "outlook", "teams", "m365", "office 365",
    "メール", "予定表", "会議", "カレンダー", "受信トレイ", "添付",
    "work iq", "workiq", "copilot connector", "コネクタ",
)

#: A fallback reason matching any of these is the ROUTE's failure, not the goal's. Kept as
#: patterns rather than exact strings because they are produced by several layers.
ROUTE_CAUSED = (
    r"token", r"capture", r"unauthor", r"401", r"403",
    r"connectionclosed", r"websocket", r"timeout", r"handshake",
    r"refresh", r"expired", r"ChatHubError: this socket route already failed",
)

#: A fallback reason matching one of THESE is about the goal: the socket could carry the
#: conversation and the conversation needed something a tab provides.
TASK_CAUSED = (
    r"carried no text", r"card", r"consent", r"attachment", r"添付", r"許可",
)


def needs_workiq(goal: str) -> bool:
    """The fixed predicate. True means: do not put this goal on a socket."""
    text = (goal or "").lower()
    return any(marker in text for marker in WORKIQ_MARKERS)


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
    if needs_workiq(goal):
        return TAB
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
    """The knobs a genome may move. The Work IQ predicate is deliberately absent."""
    return ("transport_eligible_kinds", "transport_explore_rate")


def choose(goal: str, *, kind="", knobs=None, explore=False) -> str:
    """The transport for one goal, under whichever version the active harness names."""
    try:
        from relay.selfimprove import runtime_config as _rc
        impl = TRANSPORT_VERSIONS.get(_rc.component("transport"), _policy_v1)
    except Exception:
        impl = _policy_v1
    return impl(goal, kind=kind, knobs=knobs, explore=explore)
