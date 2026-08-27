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
    # ADDED FROM A REAL RUN, 2026-08-27. Both of these classified as `unknown`, which was
    # correct until somebody read them, and between them they were the two commonest fallback
    # reasons on record -- 19 and 11 occurrences. Because `unknown` is never retried, every
    # one of them opened a tab instead of reconnecting.
    #
    # "could not open the socket" is raised by the route itself when the connection cannot be
    # established at all, so it is transport by construction. The instance that prompted this:
    # InvalidProxyStatus: proxy rejected connection: HTTP 502 -- an upstream corporate proxy
    # refusing the websocket upgrade for about a minute. Three of those in a row closed the
    # route permanently and the remaining forty minutes of an hour-long run went on tabs, at
    # roughly 900 MB of browser instead of 390.
    #
    # "the backend declined the request" is the server refusing a request on an open socket.
    # Read as route-caused because a tab is a different client talking to the same backend and
    # fixes nothing -- and measured: of four such fallbacks on record, three were followed by
    # a worker completing over the SOCKET, so the tab was not what recovered them.
    r"could not open the socket", r"invalidproxystatus", r"proxy rejected",
    # NARROWED after an existing test caught the first version swallowing more than the
    # evidence covered. "the backend declined the request" also carries InvalidRequest, which
    # names a malformed request rather than a connection, and about which nothing has been
    # measured -- so it stays unread, which is the whole point of leaving `unknown` alone.
    # Only the decline that WAS measured is listed.
    r"backend declined the request:\s*internalerror",
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


#: Words that mean the goal ASKS THE MODEL TO DO SOMETHING TO THE WORLD, not to read it.
#:
#: The distinction the reconnect budget needed and never consulted. Re-sending a turn whose
#: delivery is uncertain costs a wasted turn when the goal only reads, and a second mail when
#: it sends one -- and the code that decided whether to re-send had the goal text in hand the
#: whole time. It is written in this module because it is the same kind of judgement as
#: `needs_tab`: a property of the request, fixed, and read by more than one caller.
#:
#: DELIBERATELY BROAD, because the cost is asymmetric. A false positive stops a re-send that
#: would have been harmless and asks a person; a false negative repeats an act nobody can
#: take back. Japanese and English, because the goals here are written in both.
#: Words that mean the goal ASKS THE MODEL TO DO SOMETHING TO THE WORLD, not to read it.
#:
#: MEASURED AGAINST THE REAL GOALS, after the first version was written from imagination and
#: got it almost exactly backwards. Bare `送信` fired on 53 of 193 real goals; 50 of those were
#: `送信者` -- the SENDER of a mail being read -- and 3 were `送信済み`, mail already sent. Two
#: were the verb. A 96%% false-positive rate on the population it exists to judge, and each one
#: would have stopped a harmless reconnect on a read-only search. The same shape was in
#: `登録した`, `登録され`, `投稿され`: nouns and past forms describe what is there to be read.
#:
#: So the Japanese patterns require a VERB form -- して / しろ / せよ / します / する -- which is
#: how a Japanese instruction asks for the act rather than naming it. English keeps word
#: boundaries, where the same ambiguity does not arise in these words.
#:
#:
#: `しています` and `送っている` are DESCRIPTIONS, not requests -- "I create the Week00-23
#: material", "they sent congratulations, so the announcement was mid-April". Both appear in
#: real goals as evidence being reported, and both were read as instructions to act. The
#: request forms are して / してください / しろ / せよ; `して(?!い)` keeps them and drops the rest.
#: Still deliberately broad within that: a false positive stops a re-send and asks a person, a
#: false negative repeats something nobody can take back.
ACTING = (
    r"送信(?:して(?!い)|しろ|せよ|してください)",
    r"送付(?:して(?!い)|しろ|せよ|してください)",
    r"返信(?:して(?!い)|しろ|せよ|してください)",
    r"削除(?:して(?!い)|しろ|せよ|してください)",
    r"移動(?:して(?!い)|しろ|せよ|してください)",
    r"登録(?:して(?!い)|しろ|せよ|してください)",
    r"予約(?:して(?!い)|しろ|せよ|してください)",
    r"招待(?:して(?!い)|しろ|せよ|してください)",
    r"投稿(?:して(?!い)|しろ|せよ|してください)",
    r"アップロード(?:して(?!い)|しろ|せよ|してください)",
    r"保存(?:して(?!い)|しろ|せよ|してください)",
    r"追記(?:して(?!い)|しろ|せよ|してください)",
    r"作成(?:して(?!い)|しろ|せよ|してください)",
    r"書き込み(?:して(?!い)|しろ|せよ|してください)",
    r"送って(?!い)",
    r"作って(?!い)",
    r"消して(?!い)",
    r"下書きを作",
    r"\bsend\b",
    r"\bemail\b",
    r"\breply\b",
    r"\bpost\b",
    r"\bcreate\b",
    r"\bdelete\b",
    r"\bremove\b",
    r"\bupload\b",
    r"\bcommit\b",
    r"\bpush\b",
    r"\bwrite\b",
    r"\bschedule\b",
    r"\binvite\b",
)


def goal_may_act(goal: str) -> bool:
    """Whether re-sending this goal's turn could repeat something done to the world.

    Reading is idempotent; sending is not. Nothing consulted this at re-send time -- the
    reconnect path and the tab fallback both re-sent the turn verbatim, and the only guard
    was a count of how many times they had done it.
    """
    text = (goal or "")
    for pattern in ACTING:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


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
