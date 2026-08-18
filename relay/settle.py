"""When a reply is finished -- one rule, as a pure function.

WHY THIS EXISTS

"Has the reply finished?" is decided in four places, and only one of them carries the guard
that 3,931 measured replies justified. `wait_for_idle` requires the text to be byte-identical
across several consecutive reads before accepting it; `_FleetWorker`, `RefuterSession` and
`ResearchSession.poll` do not. Three of them carry a comment saying they apply the same guard
directly. They do not, and a comment is not a mechanism.

The weakest of the four is the refuter's, which has no sample requirement and no marker
concept at all -- so a half-written refutation can be accepted as a verdict. That component is
the selector in best-of-N, which is the part the project's own notes identify as the ceiling
on output quality.

WHAT IS HERE AND WHAT IS NOT

Only the decision. No page, no clock, no I/O: the time is an argument, so a test can drive a
whole settle sequence deterministically instead of sleeping through one. Everything that
depends on driver state stays outside -- in particular `_is_stale_repeat`, which asks whether
this text was already accepted for the PREVIOUS turn, and which needs history this function
deliberately does not have.

THE RULE, PROMOTED FROM THE CANONICAL SITE UNCHANGED

  generating          -> reset. The Stop button is the authoritative "still going" signal,
                         and reading through it is what produced a 102-character mid-word
                         capture that looked stable because streaming had paused.
  processing          -> SKIP, not reset. A placeholder ("処理中です。") carries no
                         information about the answer, so it is not evidence the answer
                         changed. Measured on 2026-08-10: the block cycles answer ->
                         placeholder -> empty -> answer every few seconds with the Stop
                         button absent throughout, and resetting on each one needlessly
                         delays a turn that has already finished. A placeholder still can
                         never be accepted -- `last` is only ever set from a real read.
  text changed        -> reset and start counting, at ONE sample rather than zero: the read
                         that saw the new text is itself the first observation of it.
  text unchanged      -> another sample.
  accept when         stable_count >= need_samples AND elapsed >= need_dwell, where a tail
                         with no protocol marker doubles BOTH -- in case the Stop button
                         flickered off between two streamed chunks and a mid-stream pause is
                         what looks stable.

NOTHING HERE IS UNUSED-CODE-SHAPED BY ACCIDENT

This module is deliberately not wired into any call site yet. The migration is one site per
step so that each is separately revertible, and the first of those steps has a hard
requirement: `wait_for_idle` must behave IDENTICALLY afterwards. If moving the canonical site
onto this function changes what it accepts, the function is wrong, not the site.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: How many consecutive byte-identical reads before a reply may be accepted. Mirrors
#: `REPLY_SETTLE_SAMPLES` in the relay, including the floor of 2 -- one sample is not
#: stability, it is a single read.
DEFAULT_SAMPLES = max(2, int(os.environ.get("MCP_REPLY_SETTLE_SAMPLES", "3")))

#: What a missing protocol marker multiplies the requirements by. Was written as a bare
#: `* 2` in several places, which is how two of them came to disagree about whether it
#: applied to the sample count as well as the dwell.
MARKERLESS_DWELL_FACTOR = float(os.environ.get("MCP_MARKERLESS_DWELL_FACTOR", "2.0"))

#: The four things a step can conclude. Named rather than boolean because "skip" and
#: "waiting" are different states that a two-valued return would merge, and the difference
#: between them is the placeholder finding above.
RESET = "reset"
SKIP = "skip"
WAITING = "waiting"
ACCEPT = "accept"


@dataclass(frozen=True)
class SettleState:
    """What the poller knows between reads. Frozen: each step returns a new one.

    Immutable so that a caller cannot accidentally keep counting on a state it also handed
    somewhere else -- the settle counters are the whole decision, and a shared mutable one is
    how two poll loops in the same process would silently settle each other's turns.
    """
    last: str | None = None
    stable_count: int = 0
    stable_since: float | None = None


def _predicate(value, text, *, name):
    """A bool, or a callable applied to the text. Anything else is a caller error.

    Not coerced with `bool()`. A predicate that arrives as the string "false" would be
    truthy, and at this particular seam that means accepting a reply the caller believed it
    had marked as still processing.
    """
    if callable(value):
        value = value(text)
    if not isinstance(value, bool):
        raise TypeError("%s must be a bool or a callable returning one, got %r"
                        % (name, type(value).__name__))
    return value


def requirements(*, dwell_s, has_marker, samples=None,
                 markerless_factor=MARKERLESS_DWELL_FACTOR) -> tuple:
    """(need_samples, need_dwell) for a tail with or without a protocol marker.

    Split out because it is the part every site got subtly different, and because a test can
    assert on it without driving a sequence.
    """
    base = DEFAULT_SAMPLES if samples is None else int(samples)
    if has_marker:
        return base, float(dwell_s)
    return int(round(base * markerless_factor)), float(dwell_s) * markerless_factor


def settle_step(state: SettleState, text, *, now, dwell_s, generating, is_processing,
                has_marker, samples=None,
                markerless_factor=MARKERLESS_DWELL_FACTOR) -> tuple:
    """One poll. Returns (new_state, outcome) where outcome is one of the four constants.

    `generating`, `is_processing` and `has_marker` may each be a bool or a predicate over
    `text`. `now` is the caller's clock reading -- passed in so a sequence can be replayed.

    ACCEPT IS NOT "DONE". It means the text has settled; the caller still has to ask whether
    this is a byte-identical repeat of the previous turn's accepted reply, which is the
    stale-capture signature and needs history this function does not carry.
    """
    # None becomes empty; anything already a string is left ALONE. Coercing with `str()`
    # would rebuild a `str` subclass as a plain one, so the next poll would compare the new
    # value against a different object than the caller handed over -- a reset where the
    # caller's own equality would have said "unchanged".
    if text is None:
        text = ""
    elif not isinstance(text, str):
        text = str(text)
    if _predicate(generating, text, name="generating"):
        return SettleState(), RESET

    if _predicate(is_processing, text, name="is_processing"):
        # SKIP, and the state survives. The placeholder is not evidence about the answer, so
        # it neither advances nor destroys the stability accumulated so far.
        return state, SKIP

    if text != state.last:
        # ONE, NOT ZERO. The read that just saw the new text is the first observation of it,
        # and starting at zero would silently require one extra poll everywhere -- a change
        # that would look like "the guard got stricter" rather than like an off-by-one.
        return SettleState(last=text, stable_count=1, stable_since=now), RESET

    marker_ok = _predicate(has_marker, text, name="has_marker")
    need_samples, need_dwell = requirements(dwell_s=dwell_s, has_marker=marker_ok,
                                            samples=samples,
                                            markerless_factor=markerless_factor)
    count = state.stable_count + 1
    # COMPARED TO None, NOT FOR TRUTHINESS. `stable_since` is a clock reading, and a bare
    # `if state.stable_since:` treats a legitimate 0.0 as unset, pinning elapsed at 0.0
    # forever -- a turn that then never settles no matter how long it sits still.
    elapsed = (now - state.stable_since) if state.stable_since is not None else 0.0
    new = SettleState(last=text, stable_count=count, stable_since=state.stable_since)
    if count >= need_samples and elapsed >= need_dwell:
        return new, ACCEPT
    return new, WAITING


def explain(state: SettleState, *, now, dwell_s, has_marker, samples=None,
            markerless_factor=MARKERLESS_DWELL_FACTOR) -> dict:
    """Why this state has not been accepted yet -- for a trace line or a stuck-turn report.

    A turn that never settles while the answer looks complete on screen is otherwise
    indistinguishable from one that is still streaming, and that ambiguity is what made the
    original truncation bug take 3,931 replies to characterise.
    """
    need_samples, need_dwell = requirements(dwell_s=dwell_s, has_marker=bool(has_marker),
                                            samples=samples,
                                            markerless_factor=markerless_factor)
    elapsed = (now - state.stable_since) if state.stable_since is not None else 0.0
    return {
        "stable_count": state.stable_count, "need_samples": need_samples,
        "elapsed": round(elapsed, 3), "need_dwell": need_dwell,
        "has_marker": bool(has_marker),
        "short_by_samples": max(0, need_samples - state.stable_count),
        "short_by_seconds": round(max(0.0, need_dwell - elapsed), 3),
        "text_len": len(state.last or ""),
        # THE TAIL, NOT THE BODY. Truncation shows up at the end -- the capture that started
        # this was 102 characters ending mid-word -- and a trace carrying the head would have
        # looked entirely healthy.
        "tail": (state.last or "")[-60:],
    }
