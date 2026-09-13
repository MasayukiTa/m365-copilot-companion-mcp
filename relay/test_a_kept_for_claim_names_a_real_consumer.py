# -*- coding: utf-8 -*-
"""A function in the unreached baseline may not justify itself with a consumer that is not there.

FIVE OF FIVE, CHECKED BY HAND ON 2026-09-14. Every one of these docstrings named who the
function was kept for, and in every case that consumer did not exist:

    tools/lock_state.py::locked_since        "Kept for the CLI and for diagnostics"
    tools/lock_state.py::matching_record     "Kept for callers that only want to name one
                                              record (the CLI, diagnostics)"
    relay/project_memory.py::list_themes     "For the cockpit and for tests"
    relay/solve_policy.py::plan_and_explain  "One-line human summary ... (for a cockpit / log)"
    relay/chathub.py::collect_text           "Kept for callers that look at a single frame"

`_cli` had `show` and `token-gap`. The cockpit has no project-memory surface and no plan-summary
surface -- its only "theme" is the light/dark one. `plan_and_explain` and `collect_text` have
zero references anywhere in the repository, tests included.

WHY THIS IS WORSE THAN NO JUSTIFICATION, and why it earns a test rather than a note: an entry
that says nothing invites the next reader to ask who should call it. An entry that says "kept
for the CLI" reads as *settled*, and the question stops being asked. Two of these had sat that
way long enough that recovering the answer took a measurement.

THE CHECK IS NOT FUZZY, because the baseline itself supplies the contradiction. The claim is
"a consumer exists"; membership in `NO_CALLER_NO_TEST` / `NO_CALLER_BUT_TESTED` is the finding
that none does. A docstring cannot hold both. Either wire the consumer -- the entry then leaves
the baseline on its own, which is what the ratchet is for -- or say what is true.

THE FIRST MATCHER WAS TOO WIDE, and the miss is the useful part. A case-insensitive `kept for`
flagged `harness_tree.py::justified`, whose docstring says *"a branch earns its place when a
change was KEPT for that class"* -- the wrong "kept", in a sentence about decisions rather than
about callers. A justification is written as its OWN sentence, so the phrase is required at the
start of one. Matching a spelling instead of a shape is the same mistake this file is about.
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import test_nothing_new_is_built_without_a_caller as RATCHET  # noqa: E402

#: Phrases asserting a present-tense consumer. Each was taken from a real docstring rather than
#: invented, so this set grows only when a new shape is actually met.
#:
#: "Kept for" is anchored to the start of a sentence and is CASE-SENSITIVE, for the reason in
#: the module docstring. The named-surface phrases need no anchor: there is no innocent way to
#: write "for the cockpit" in a docstring about a function nothing calls.
CLAIMS = (
    r"(?m)(?:^|(?<=\.\s)|(?<=\.\n))\s*Kept for\b",
    r"(?i)\bfor the CLI\b",
    r"(?i)\bfor (?:the|a) cockpit\b",
    r"(?i)\bfor the evolution loop\b",
)

#: The fix is a docstring that says the consumer is missing -- and those sentences contain the
#: same words, so without this the check would refuse its own remedy.
EXEMPT = re.compile(
    r"no caller|never (?:built|written|run)|does not exist|has no consumer|nothing calls|"
    r"was never|not wired|unreached|had none|no such",
    re.I)

#: Changing a docstring in one of these means an operator re-signs the baseline with a stated
#: reason (see relay/selfimprove/frozen.py). That is not a trade worth making for prose, so a
#: violation here is REPORTED rather than failed -- and pinned below, so a NEW one still fails.
try:
    from relay.selfimprove.frozen import FROZEN_MANIFEST
except Exception:                                  # pragma: no cover - import-order safety
    FROZEN_MANIFEST = []

#: The one outstanding frozen-file claim, 2026-09-14. `get_client_ip` says "Kept for
#: backward-compat with callers that just need the IP string", and there are none.
KNOWN_FROZEN = {"tools/security.py::get_client_ip"}


def _docstring(rel, name):
    path = os.path.join(REPO, rel)
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=rel)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_docstring(node) or ""
    return None


def _claims_in(doc):
    return [c for c in CLAIMS if re.search(c, doc)]


def _violations():
    """(entry, claims, first line) for every baseline entry whose docstring names a consumer."""
    out = []
    for entry in sorted(RATCHET.BASELINE):
        rel, _, name = entry.partition("::")
        doc = _docstring(rel, name)
        if not doc or EXEMPT.search(doc):
            continue
        hits = _claims_in(doc)
        if hits:
            out.append((entry, hits, doc.strip().splitlines()[0][:90]))
    return out


def test_no_baseline_entry_claims_a_consumer_it_does_not_have():
    bad = [v for v in _violations() if v[0].partition("::")[0] not in FROZEN_MANIFEST]
    assert not bad, (
        "%d unreached function(s) name a consumer that the baseline itself says is absent. "
        "Either wire the consumer -- the ratchet drops the entry on its own once you do -- or "
        "change the docstring to say the consumer was never built:\n  %s"
        % (len(bad), "\n  ".join("%s  claims %s\n      %s" % v for v in bad)))


def test_the_frozen_files_outstanding_claims_are_exactly_the_known_ones():
    """A frozen file's prose cannot be corrected without an operator re-signing the baseline,
    so these are recorded instead of fixed -- and recorded EXACTLY, so a new one still fails
    here rather than joining a list nobody re-reads."""
    frozen_bad = {v[0] for v in _violations() if v[0].partition("::")[0] in FROZEN_MANIFEST}
    assert frozen_bad == KNOWN_FROZEN, (
        "the frozen-file claims changed: now %s, recorded %s. A NEW one needs an operator "
        "decision (re-sign with a reason, or wire the consumer); one that GONE means the list "
        "above should shrink." % (sorted(frozen_bad), sorted(KNOWN_FROZEN)))


# ── the matcher itself ────────────────────────────────────────────────────────────────────

def test_the_matcher_can_see_a_claim():
    """A checker that finds nothing everywhere proves nothing anywhere."""
    assert _claims_in("Kept for the CLI and for diagnostics, where naming it is the question.")
    assert _claims_in("Every distinct authority present. For the evolution loop.")
    assert _claims_in("Every remembered theme as (theme, slug, path). For the cockpit.")
    assert _claims_in("A summary of the plan.\n\nKept for callers that want one line.")


def test_the_wrong_kept_is_not_a_claim():
    """THE FALSE POSITIVE THAT NARROWED THIS. `harness_tree.py::justified` says a branch earns
    its place when a change was KEPT for a task class -- a sentence about decisions, not about
    callers, and the only thing separating the two is that a justification starts a sentence."""
    assert not _claims_in(
        "A branch earns its place when a change was KEPT for that class; a branch nobody "
        "measured is a per-class configuration fitted to whatever that class contained.")


def test_ordinary_prose_is_not_a_claim():
    assert not _claims_in("The transport for one goal, under whichever version is active.")
    assert not _claims_in("Returns the rows that were kept, newest first.")


def test_the_remedy_is_not_itself_a_violation():
    """The honest docstring contains the same words; refusing it would make the fix impossible."""
    fixed = ("Kept for the CLI -- except that CLI was never built, so this has no caller. "
             "See docs/unreached_burndown.md.")
    assert _claims_in(fixed) and EXEMPT.search(fixed)


def test_the_baseline_is_not_empty():
    """If BASELINE ever reads as empty, the sweep above is vacuously green."""
    assert len(RATCHET.BASELINE) > 10, len(RATCHET.BASELINE)
