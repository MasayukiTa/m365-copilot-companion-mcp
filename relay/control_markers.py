"""The protocol's end-of-turn marker, parsed as a control value instead of matched as prose.

WHAT THE MARKER IS. Every PROTOCOL/CONTINUE prompt requires the agent to end a complete turn
with one of DONE / CONTINUE / STUCK / FAIL / RESEARCH / ANALYZE / PLAN_READY, some of them
carrying a reason after a colon. It is control plane: it says whether the turn finished and
what the loop should do next. It is not part of the answer.

WHAT IT WAS. `"DONE" in last_line.upper()` -- a substring test over the reply's last line,
with the marker left in the text afterwards for every prose classifier to read as content.

WHY THAT IS THE WRONG SHAPE, MEASURED RATHER THAN ARGUED. Across 1,015 assistant replies in
the stored transcripts, 813 carried a marker on the last line:

    bare marker alone           591   72.7%
    MARKER: argument            214   26.3%
    marker buried in prose        8    1.0%

and the third row is not what it looks like. Seven of those eight are `FAIL — reason`,
`FAIL: reason`, `FAIL：reason`: real markers whose argument form the old tuple did not model,
because FAIL was listed as though it were bare. The eighth is the actual false positive --

    Error: Error executing tool: Failed to get AI insights (Inva...

-- where FAIL matched inside "Failed". Nothing else. And "prose, then a marker at the end of
the line" (`作業完了。DONE`), which was the reason to fear an anchored grammar, occurs ZERO
times. So anchoring the grammar loses nothing real and drops the one genuine misread.

WHY IT MATTERS BEYOND THAT ONE. The argument to a FAIL is the agent explaining why it could
not proceed -- "推測による改変は行えないため", "第三者情報の調査を継続することは不適切と判断".
That is the agent's own control-plane reason, and it was left in the text that the refusal
detectors, the canned-non-answer detectors and the locked-client detectors then read as
though the model had said it about the request. A control marker separated at a typed
boundary cannot be misread as prose, because the prose classifiers never see it.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

#: The closed set of markers the protocol defines. A marker outside this set is not a marker.
KINDS = ("DONE", "CONTINUE", "STUCK", "FAIL", "RESEARCH", "ANALYZE", "PLAN_READY")

#: Markers that mean the turn is finished and the goal with it, or finished and continuing.
#: Kept here rather than at the call sites so "what does this marker mean" has one answer.
TERMINAL_KINDS = frozenset({"DONE", "FAIL", "STUCK"})

#: Separators seen between a marker and its argument in the real transcripts: ASCII colon,
#: the full-width colon a Japanese keyboard produces, an em dash and a hyphen. Matched as a
#: set rather than assumed to be ":" -- three of the seven argument-bearing FAILs on record
#: used one of the other three.
_SEP = r"[:\uFF1A\u2014\u2015\u2212-]"

#: Anchored: the marker must START the last line, modulo markdown emphasis and whitespace.
#: `**DONE**` occurs; `the DONE marker was omitted` must not.
_RE = re.compile(
    r"^[\s>*_`#]*(" + "|".join(KINDS) + r")[\s*_`]*(?:" + _SEP + r"\s*(?P<arg>.*))?$",
    re.IGNORECASE,
)
#: A bare marker may carry terminal punctuation and nothing else.
_BARE_TAIL = re.compile(r"^[\s*_`.。!！]*$")

#: THE TRAILING FORM: prose, then the marker at the very END of the line.
#:
#: This shape does not occur in the 1,015 stored replies -- not once -- and the first version
#: of this module rejected it on exactly that evidence. A settle test caught it within the
#: minute, with the fixture `これは最終回答です DONE`, and the failure was not cosmetic: a reply
#: whose marker goes unrecognised is never ACCEPTED in the unified settle path, so the turn
#: does not complete at all.
#:
#: The corpus is a fact about these agents on these runs, not a guarantee about the next one,
#: and the protocol prompt itself says 最後の行に DONE -- an agent that writes 作業完了。DONE is
#: COMPLYING with it. Rejecting a compliant reply costs a hung turn; accepting this shape
#: costs nothing measurable, because the one false positive on record
#: ("...Failed to get AI insights (Inva…") has FAIL mid-word AND mid-line, so an end-anchored
#: form still refuses it. When the asymmetry is "a delay" against "a turn that never
#: finishes", the grammar takes the wider reading.
#: The lookbehind is a NEGATIVE one -- "not in the middle of a word" -- rather than a list of
#: allowed preceding characters. The first attempt listed whitespace and brackets, and 作業完了。DONE
#: (the very example written above as the compliant case) failed on the Japanese full stop.
#: Enumerating what may precede a marker in two writing systems is a list nobody can finish.
_TRAILING = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(KINDS) + r")[\s*_`.。!！]*$", re.IGNORECASE)


class ControlMarker(NamedTuple):
    """The parsed control value. `kind` is always upper case and always one of KINDS."""
    kind: str
    argument: str
    line: str                     # the raw line it came from, for the record

    @property
    def terminal(self) -> bool:
        return self.kind in TERMINAL_KINDS


def parse(text: str) -> Optional[ControlMarker]:
    """The control marker on the last non-empty line of `text`, or None.

    Returns None rather than a marker of kind "UNKNOWN": a reply without a marker is a
    perfectly ordinary thing (202 of the 1,015 measured replies), and inventing a value for it
    would put a non-marker into a set that exists to be closed.
    """
    if not text:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1].strip()
    m = _RE.match(last)
    if m:
        arg = (m.group("arg") or "").strip()
        if not arg:
            # No separator matched, so everything after the marker word must be punctuation --
            # otherwise this is prose that merely begins with the word.
            if not _BARE_TAIL.match(last[m.end(1):]):
                return None
        return ControlMarker(m.group(1).upper(), arg, last)
    t = _TRAILING.search(last)
    if t:
        return ControlMarker(t.group(1).upper(), "", last)
    return None


def split(text: str):
    """(prose, marker) -- the reply with its control line removed, and the marker.

    THE POINT OF THE WHOLE MODULE. Prose classifiers get `prose`; the loop gets `marker`. A
    FAIL's argument is the agent's reason for stopping, and letting a refusal detector read it
    as though the model had said it about the request is how a control value becomes evidence
    about content.
    """
    marker = parse(text)
    if marker is None:
        return (text or ""), None
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            del lines[i]
            break
    return "\n".join(lines), marker


def has_marker(text: str) -> bool:
    """Whether the turn ended with a protocol marker. The completion detector's question."""
    return parse(text) is not None
