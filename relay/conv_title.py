# -*- coding: utf-8 -*-
"""A conversation title that says which task it was.

THE PROBLEM, MEASURED. Copilot names a conversation from the opening of its first message, and
every worker's first message is PROTOCOL + goal, where PROTOCOL is ~1,400 characters shared by
every task. Across 424 stored conversations:

    174  "あなたはこのゴールに向けて、ツールを使いながら自律的に作業します。重…"   the protocol preamble
     48  "Microsoft Copilot"                                                  Copilot's default
     39  "【出力規律・厳守】あなたはタスク実行者であり…"                          the discipline preamble

213 of 424 -- half the list -- named after text identical across unrelated tasks, so the sidebar
shows runs of visually identical rows.

WHAT THIS IS NOT. It is not a summary. An external review pushed back on that framing and was
right: this is TASK IDENTIFICATION, and the two differ in what they may invent. A summary claims
what happened; a title has to say which task a row is, be stable while somebody is watching it,
and never assert an outcome -- the DONE/STUCK badge already does that, and a title that said
"fixed the login bug" for work that failed would be worse than the boilerplate it replaced.

WHY NO MODEL CALL. A title is cosmetic metadata, and on this deployment a model call means a
Copilot turn on the same tenant quota the actual work uses. Measured 2026-08-31: a shared
tool-planner rate limiter refused 217 of 237 turns in one run, with refusals concentrated where
the fleet was densest (median 35 concurrent replies at a refusal against 5 at a recovery).
Spending that quota on naming rows, and adding another burst source, is not a trade worth
making. Extraction is deterministic, free, and instant.

IT ALSO DISCLOSES LESS. An abstractive title can surface something that was buried in a
transcript; an extractive one can only surface what was already in the goal's first clause --
and paths, addresses and long identifiers are redacted even from that, because the sidebar is
visible over someone's shoulder in a way a transcript is not.
"""
from __future__ import annotations

import hashlib
import re
import time

#: The version stamped onto every title this module produces, so a migration is reversible and
#: a later version can find what an earlier one wrote.
SOURCE = "local-v1"

MAX_LEN = 64
MIN_USEFUL_LEN = 4


def _boilerplate_prefixes():
    """The real constants, imported rather than re-typed.

    A copy of a prefix here would drift the moment the prompt was edited, and the failure would
    be silent: titles quietly go back to being boilerplate because the boilerplate no longer
    matches the copy. Falls back to a short literal set only if the import fails.
    """
    out = []
    try:
        from relay import copilot_autopilot_relay as car
        for name in ("PROTOCOL", "OUTPUT_DISCIPLINE", "SKILL_SENTENCE", "CONTINUE_JOB",
                     "FIX_JOB", "NUDGE_JOB", "RETRY_JOB"):
            val = getattr(car, name, "")
            if isinstance(val, str) and len(val) > 40:
                out.append(val)
    except Exception:
        pass
    out += [
        "あなたはこのゴールに向けて、ツールを使いながら自律的に作業します",
        "【出力規律・厳守】",
        "【最重要】使えるツールは",
    ]
    return out


#: Titles that identify nothing. A row named one of these is exactly the row this module exists
#: to replace, so producing one again is a failure, not an acceptable outcome.
USELESS = {
    "microsoft copilot", "copilot", "new chat", "新しいチャット", "untitled", "無題", "chat",
}

#: Redacted from a title, never from the underlying record. The sidebar is read over shoulders.
_REDACT = [
    (re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+"), "<path>"),          # C:\... / C:/...
    (re.compile(r"(?<![\w.])/(?:home|Users|var|etc|opt)/[^\s\"'<>|]+"), "<path>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<id>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hash>"),
    (re.compile(r"\bBearer\s+\S+", re.I), "<token>"),
]

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_boilerplate(text: str) -> str:
    """Remove every known shared preamble, wherever it sits in the message."""
    out = text or ""
    for block in _boilerplate_prefixes():
        if not block:
            continue
        out = out.replace(block, " ")
        # The prompt is assembled by concatenation and sometimes reaches storage with its
        # whitespace collapsed, so an exact match can miss. Fall back to the opening clause,
        # which is what Copilot actually took the title from.
        head = block[:40]
        if head and head in out:
            idx = out.find(head)
            end = out.find("\n", idx)
            out = out[:idx] + (out[end:] if end > 0 else "")
    return out


def _redact(text: str) -> str:
    for pattern, repl in _REDACT:
        text = pattern.sub(repl, text)
    return text


def _first_clause(text: str) -> str:
    """The first sentence-ish run of the remaining text."""
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#*->・ 　").strip()
        if len(line) < MIN_USEFUL_LEN:
            continue
        if line.lower() in USELESS:
            continue
        # Cut at the first sentence end, in either language.
        cut = re.split(r"(?<=[。．!?])\s|(?<=\. )|\n", line)
        return (cut[0] if cut else line).strip()
    return ""


def _swe_title(goal: str) -> str:
    """SWE-bench goals have a better title inside them than their first clause.

    They open with the same sentence for every instance, and the thing that identifies one is
    the repository plus the issue's own title -- both already written in the goal. Extractive,
    so nothing is invented.
    """
    repo = re.search(r"open-source project \*\*([\w.\-]+/[\w.\-]+)\*\*", goal or "")
    if not repo:
        repo = re.search(r"実在の\s+\w+\s+ライブラリ\s+\*\*([\w.\-]+)\*\*", goal or "")
    if not repo:
        return ""
    title = ""
    m = re.search(r"==\s*Issue to fix\s*==\s*(.*?)(?:\n==|\Z)", goal or "", re.S)
    if m:
        body = m.group(1)
        t = re.search(r"#+\s*Title\s*:?\s*\n+\s*(.+)", body)
        if t:
            title = t.group(1).strip()
        else:
            for line in body.splitlines():
                line = line.strip().lstrip("#*\"' ").strip()
                if len(line) >= MIN_USEFUL_LEN and not line.lower().startswith("## "):
                    title = line
                    break
    return ("%s: %s" % (repo.group(1), title)).strip().rstrip(":").strip()


def neutral_title(key: str = "", when: float = None) -> str:
    """The fallback. Stable, identifying, and honest that it identifies nothing else.

    NOT an empty string and NOT a guess. An empty title makes a row unclickable-looking, and a
    guessed one is the failure this module must not introduce.
    """
    stamp = time.strftime("%Y-%m-%d", time.localtime(when if when else time.time()))
    tag = hashlib.sha256((key or stamp).encode("utf-8")).hexdigest()[:4].upper()
    return "Task %s · %s" % (tag, stamp)


def is_useless(title: str) -> bool:
    """True when a title identifies nothing -- boilerplate, a default, or too short."""
    t = (title or "").strip()
    if len(t) < MIN_USEFUL_LEN:
        return True
    if t.lower() in USELESS:
        return True
    # A FRAGMENT OF BOILERPLATE IS STILL BOILERPLATE. Checking only the start missed the
    # commonest result of stripping: Copilot had already truncated the stored title mid-preamble,
    # so removing the head left a tail like "。重いゴールは一発で終わらせようとせ" -- which
    # identifies nothing, appears on many rows, and is not improved by being shorter. Matching
    # the candidate ANYWHERE inside a known block catches the head, the tail and the middle.
    probe = re.sub(r"\s+", "", t)[:16]
    if probe:
        for block in _boilerplate_prefixes():
            if block and probe in re.sub(r"\s+", "", block):
                return True
    # Punctuation and particles with no content word is a fragment however it arose.
    if not re.search(r"[0-9A-Za-z一-鿿゠-ヿ]", t):
        return True
    return False


def repeated(titles, min_count: int = 3) -> set:
    """Titles that appear on `min_count` or more conversations.

    ENUMERATING BOILERPLATE IS FAIL-OPEN, and this is the correction. `_boilerplate_prefixes`
    can only know the prompts that exist TODAY, while the archive holds titles from prompts that
    have since been deleted -- 174 rows are named after a preamble ("あなたはこのゴールに向けて、
    ツールを使いながら自律的に作業します。重いゴールは…") that appears nowhere in the codebase
    or in any stored goal. No list of known prefixes could ever have caught it.

    Repetition needs no such list. A title carried by many unrelated conversations identifies
    none of them, whatever produced it, and that is measurable from the corpus alone. Same shape
    as the hand-maintained allowlist replaced elsewhere in this repository on the same day: the
    rule that depends on remembering is the rule that fails.
    """
    from collections import Counter
    counts = Counter((t or "").strip() for t in titles if (t or "").strip())
    return {t for t, n in counts.items() if n >= min_count}


#: Above this many occurrences a title is mass-produced boilerplate and cannot be salvaged;
#: below it, it is a recurring but real label that only needs distinguishing.
#:
#: THE NUMBER COMES FROM THE MEASURED DISTRIBUTION, not from taste. In this archive the counts
#: are 174 (a deleted prompt's preamble), 48 ("Microsoft Copilot"), 39 (the discipline preamble),
#: 27 ("takeuchifile操作"), 20 ("django"), 20 ("sympy"), 10 ("flask"). Everything at 27 and below
#: names something true -- a project, a task type -- and is worth keeping with a tag. Everything
#: at 39 and above is prompt scaffolding that identifies no task at all, and appending a tag to
#: it just makes a unique meaningless string. The gap between 27 and 39 is where the line goes.
UNSALVAGEABLE_AT = 35


def salvageable(title: str, count: int) -> bool:
    """Whether a repeated title is worth keeping with a tag, or should be replaced outright."""
    return count < UNSALVAGEABLE_AT and not is_useless(title)


def disambiguate(title: str, key: str = "", when: float = None) -> str:
    """Keep what a repeated title does say, and add what it does not.

    "sympy" on twenty rows is not useless -- it names the project -- it is just not unique. It
    is better kept with a distinguishing tag than replaced by a bare identifier, which would
    throw away the one true thing the row had.
    """
    tag = neutral_title(key, when).split(" ")[1]
    base = (title or "").strip()
    if not base:
        return neutral_title(key, when)
    room = MAX_LEN - len(tag) - 3
    return "%s · %s" % (base[:room].rstrip(), tag)


def make_title(goal: str, existing: str = "", key: str = "", when: float = None) -> str:
    """The title for one conversation. Always returns something usable.

    `existing` is only consulted to decide whether it was already fine; it is never returned
    unchanged if it is boilerplate, and it is never overwritten in storage -- the caller keeps
    the original beside the derived one.
    """
    text = strip_boilerplate(goal or existing or "")
    candidate = _swe_title(goal or "") or _first_clause(text)
    candidate = _CONTROL.sub("", _redact(candidate)).strip()
    candidate = re.sub(r"\s+", " ", candidate)
    if len(candidate) > MAX_LEN:
        # Cut on a boundary rather than mid-word where there is one nearby.
        cut = candidate[:MAX_LEN]
        space = cut.rfind(" ")
        candidate = (cut[:space] if space > MAX_LEN * 0.6 else cut).rstrip() + "…"
    if is_useless(candidate):
        if existing and not is_useless(existing):
            return existing.strip()[:MAX_LEN]
        return neutral_title(key, when)
    return candidate
