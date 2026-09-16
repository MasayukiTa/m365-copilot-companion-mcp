# -*- coding: utf-8 -*-
"""~160 writers, one reader, and nothing made a new writer tell the reader about itself.

tools/tool_ledger.py recognises a fixed set of words in a returned `[... <word>: ...]`
string. That set was derived by surveying what the tools actually write -- a snapshot, taken
once, on 2026-09-16. A tool added tomorrow that returns "[foo broke: ...]" would be filed as
a success, its failures would never reach the health dot, and nothing anywhere would say so.
That is the same silent-omission shape the CI test manifest exists to prevent.

THE RULE, AND WHY IT IS NOT "EVERY WORD". Most of the tail is tool NAMES:
"[memory_read: no topic found]" is `memory_read`, not a convention. A word only becomes a
convention when more than one module reaches for it, so that is the threshold: a classifying
word used by two or more modules must be listed below as a failure, as neutral, or as
benign. One module's private wording is its own business; two modules agreeing is a
convention the ledger has to know about.
"""
from __future__ import annotations

import collections
import glob
import io
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

#: `return "[...`, `return f"[...`, `return ("[...` up to the ":" or "]" that ends the head.
RETURNED_BRACKET = re.compile(r'return\s*\(?\s*[fr]{0,2}"\[([^"\\]{0,80}?)[:\]]')

#: A word is a convention once this many modules use it.
MODULES_FOR_A_CONVENTION = 2

#: Words the ledger treats as THE CALL FAILED. Must stay in step with
#: tools/tool_ledger.py::_FAILURE_SHAPE -- a test below checks that it does.
FAILURE_WORDS = {"error", "failed", "timeout", "refused"}

#: Words the ledger treats as NO EVIDENCE: the tool declined, or could not run at all.
NEUTRAL_WORDS = {"skipped", "aborted", "unavailable"}

#: Words that lead ordinary successful output. Each needs a reason, because "it is not a
#: failure" is the claim being made and an unexamined entry here is how one gets in.
BENIGN_WORDS = {
    "required": "[confirmation required] -- a two-phase handshake, nothing has gone wrong",
    "stdout": "the first line of a process's captured output; 8,216 rows in the ledger",
    "stderr": "the same, for the other stream",
}


def _classify(head: str) -> str:
    """The word that says what a bracketed head means, or "" if there is nothing to read.

    An f-string slot is not a word -- it is an artefact of reading source rather than output
    -- so "[pwsh_exec timeout after {timeout}s]" must not classify as "{timeout}s", nor, as
    the first draft of this did, as "after".

    A head that CONTAINS a word the ledger knows is classified by it wherever it sits. Only a
    head with no known word falls back to its last token, and that is the case this file is
    hunting: a convention nobody has classified yet.
    """
    tokens = [t.lower() for t in head.split() if "{" not in t and "}" not in t]
    if not tokens:
        return ""
    known = FAILURE_WORDS | NEUTRAL_WORDS | set(BENIGN_WORDS)
    return next((t for t in tokens if t in known), tokens[-1])


@pytest.mark.parametrize("head,expected", [
    ("pwsh_exec timeout after 30s", "timeout"),
    ("pwsh_exec timeout after {timeout}s", "timeout"),
    ("read_file error", "error"),
    ("call_tool git_status error", "error"),
    ("confirmation required", "required"),
    ("stdout", "stdout"),
    # THE CASE THE GUARD EXISTS FOR. A word nobody has classified must survive extraction
    # intact and reach the assertion, or the guard passes by finding nothing.
    ("some_tool broke", "broke"),
    ("some_tool exploded horribly", "horribly"),
    ("", ""),
])
def test_the_classifier_reads_the_word_that_decides(head, expected):
    assert _classify(head) == expected


def test_an_unclassified_convention_would_actually_fail_the_guard():
    """Proving the assertion can fire. A guard nobody has seen fail is a guard nobody knows
    the shape of -- and this one was rewritten once after it fired for the right reason."""
    word = _classify("some_tool broke")
    assert word not in (FAILURE_WORDS | NEUTRAL_WORDS | set(BENIGN_WORDS)), \
        "the fallback now lands on a known word, so a new convention would pass unnoticed"


def _classifying_words():
    """word -> set of modules that return a bracketed literal ending in it."""
    seen = collections.defaultdict(set)
    paths = sorted(glob.glob(os.path.join(REPO, "tools", "*.py")))
    paths.append(os.path.join(REPO, "main.py"))
    for path in paths:
        base = os.path.basename(path)
        if base.startswith("test_") or base.endswith("_test.py"):
            continue
        try:
            text = io.open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in RETURNED_BRACKET.finditer(text):
            head = m.group(1).strip()
            if not head:
                continue
            word = _classify(head)
            if word:
                seen[word].add(base)
    return seen


def test_every_word_two_modules_agree_on_is_classified():
    known = FAILURE_WORDS | NEUTRAL_WORDS | set(BENIGN_WORDS)
    unclassified = {}
    for word, modules in _classifying_words().items():
        if len(modules) < MODULES_FOR_A_CONVENTION:
            continue            # one module's private wording, not a convention
        if word in known:
            continue
        unclassified[word] = sorted(modules)
    assert not unclassified, (
        "a way of reporting an outcome that %d+ modules agree on, which the ledger does not "
        "classify. Decide what each means and add it to FAILURE_WORDS, NEUTRAL_WORDS or "
        "BENIGN_WORDS -- an unclassified word is filed as a SUCCESS: %r" %
        (MODULES_FOR_A_CONVENTION, unclassified))


def test_the_list_here_and_the_pattern_there_have_not_drifted():
    """This file's FAILURE_WORDS is a second copy of a fact, which is what this whole
    exercise is about. It has to be checked, or it becomes the next row in the ledger."""
    from tools import tool_ledger as L

    for word in FAILURE_WORDS:
        assert L.looks_failed("[some_tool %s: detail]" % word), \
            "%r is listed here as a failure and the ledger does not agree" % word
    for word in NEUTRAL_WORDS:
        assert L.looks_unavailable("[some_tool %s: detail]" % word), \
            "%r is listed here as neutral and the ledger does not agree" % word
    for word in BENIGN_WORDS:
        text = "[some_tool %s] and then some ordinary output" % word
        assert not L.looks_failed(text) and not L.looks_unavailable(text), \
            "%r is listed here as benign and the ledger treats it as an outcome" % word


def test_the_survey_still_finds_the_convention_it_was_built_from():
    """A guard that silently matches nothing passes for ever. If the extraction breaks --
    a formatting change, a move to another return style -- this says so instead."""
    words = _classifying_words()
    assert "error" in words, "the extraction found no `[<name> error:` returns at all"
    assert len(words["error"]) >= 10, (
        "only %d module(s) look like they report errors; the survey has stopped working"
        % len(words["error"]))


@pytest.mark.parametrize("word,reason", sorted(BENIGN_WORDS.items()))
def test_every_benign_word_carries_its_reason(word, reason):
    assert reason and len(reason) > 20, \
        "%r is exempted from being an outcome with no argument for why" % word
