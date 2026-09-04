# -*- coding: utf-8 -*-
"""1,063 calls died on a guessed argument name, and the catalogue is why.

RULE 1 orders every agent to read the catalogue first. It answered with 173 rows of
``name -- summary``, alphabetical, no parameter names anywhere -- so the caller invented
them. The per-tool rates say this is a property of the catalogue and not of the caller:

    git_log       27 / 43   62.8% of calls wrong
    git_status    50 / 87   57.5%
    github_file   64 / 122  52.5%
    skill_match  145 / 178  81.5%

On those, the caller is wrong more often than right. Twenty tools cover 96.4% of all calls
and 97.2% of all argument failures; showing their parameter names costs ~470 tokens.
"""
import collections
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import tool_catalogue as C  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(REPO, ".fleet", "tool_events.jsonl")
needs_ledger = pytest.mark.skipif(
    not os.path.isfile(LEDGER), reason="no local tool ledger in this checkout")


def skill_match(text: str) -> str:
    """Find an approved procedure for a task."""
    return text


def read_file(path: str, encoding: str = "utf-8", start_line: int = 1) -> str:
    """Read a text file."""
    return path


def pptx_add_slide(path: str, title: str = "") -> str:
    """Append a slide."""
    return path


def zip_create(path: str) -> str:
    """Make a zip."""
    return path


def undocumented(a, b=1):
    return a


FAKE = {"skill_match": skill_match, "read_file": read_file,
        "pptx_add_slide": pptx_add_slide, "zip_create": zip_create,
        "undocumented": undocumented}
HOT = ("read_file", "skill_match")


# -- the measured case --------------------------------------------------------------------

def test_the_name_that_failed_145_times_is_now_in_the_catalogue():
    """An agent that reads this can see `text`, and never has to guess `query`."""
    got = C.render(FAKE, hot=HOT)
    assert "skill_match(text)" in got


def test_the_head_carries_parameter_names_and_the_tail_does_not():
    got = C.render(FAKE, hot=HOT)
    assert "read_file(path, encoding='utf-8', start_line=1)" in got
    assert "pptx_add_slide(" not in got, "the tail should stay one line per tool"
    assert "pptx_add_slide -- Append a slide." in got


# -- nothing may be dropped ---------------------------------------------------------------

def test_every_tool_still_appears_exactly_once():
    """115 of 173 tools have never been called, but the ledger is almost all coding runs --
    they were never NEEDED, which is a different fact. Dropping one would be unrecoverable
    from inside the system: unlisted, therefore never called, therefore still unlisted."""
    got = C.render(FAKE, hot=HOT)
    for n in FAKE:
        assert got.count("\n  " + n) == 1, "%s appears %d times" % (n, got.count("\n  " + n))


def test_the_count_in_the_header_is_the_whole_registry():
    assert C.render(FAKE, hot=HOT).startswith("%d tools available." % len(FAKE))


# -- the two orderings, which are a deliberate pair ----------------------------------------

def test_the_head_is_ranked_not_alphabetised():
    got = C.render(FAKE, hot=HOT)
    assert got.index("read_file(") < got.index("skill_match("), (
        "the head must keep measured-use order; read_file outranks skill_match 2421 to 178")


def test_the_tail_is_alphabetical():
    """Nothing is known about the tail, so ranking it by a count of zero would only
    scramble it. An agent looking for 'pptx' should find 'pptx'."""
    got = C.render(FAKE, hot=HOT)
    assert got.index("\n  pptx_add_slide") < got.index("\n  undocumented")
    assert got.index("\n  undocumented") < got.index("\n  zip_create")


# -- shapes that must not break -------------------------------------------------------------

def test_a_renamed_tool_does_not_take_the_catalogue_down():
    got = C.render(FAKE, hot=("read_file", "gone_away"))
    assert "read_file(" in got and "gone_away" not in got


def test_an_undocumented_tool_still_gets_a_row():
    assert "\n  undocumented -- " in C.render(FAKE, hot=HOT)


def test_an_empty_registry_renders():
    assert C.render({}, hot=HOT).startswith("0 tools available.")


# -- the signature rendering ----------------------------------------------------------------

def test_annotations_are_dropped_and_names_kept():
    """The measured failure is a NAME failure -- query= for text=, file= for path= -- so
    annotations are the half that can go. Keeping them roughly doubles the head for no
    part of the signal."""
    got = C.compact_signature(read_file)
    assert got == "(path, encoding='utf-8', start_line=1)"
    assert ": str" not in got and "->" not in got


def test_a_builtin_without_an_inspectable_signature_is_not_an_error():
    assert C.compact_signature(len) in ("(...)", "(obj)")


def test_var_args_are_shown_as_such():
    def f(a, *rest, **kw):
        return a
    assert C.compact_signature(f) == "(a, *rest, **kw)"


# -- is the constant still true? -------------------------------------------------------------

@needs_ledger
def test_the_hot_set_still_matches_the_ledger():
    """HOT is a measurement, so it can go stale. This re-derives it and fails when the head
    stops covering what it claims. Skips in CI, where .fleet/ is not committed."""
    calls, counts, argfail = {}, collections.Counter(), collections.Counter()
    with open(LEDGER, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") == "call":
                calls[r.get("id")] = r.get("tool") or "?"
                counts[r.get("tool") or "?"] += 1
            elif r.get("event") == "outcome" and not r.get("ok"):
                blob = json.dumps(r, ensure_ascii=False)
                if ("unexpected keyword argument" in blob
                        or "required positional argument" in blob):
                    argfail[calls.get(r.get("id"), "?")] += 1

    total = sum(counts.values()) or 1
    covered = sum(counts.get(n, 0) for n in C.HOT) / float(total)
    assert covered >= 0.90, (
        "the head covers only %.1f%% of calls; re-derive HOT" % (100 * covered))

    total_af = sum(argfail.values())
    if total_af:
        held = sum(argfail.get(n, 0) for n in C.HOT) / float(total_af)
        assert held >= 0.90, (
            "the head holds only %.1f%% of argument failures; re-derive HOT" % (100 * held))

    # Anything failing this often that is NOT in the head is the next thing to add.
    missing = [(n, c) for n, c in argfail.most_common() if c >= 15 and n not in C.HOT]
    assert not missing, "outside the head and failing often: %r" % (missing,)


@needs_ledger
def test_the_head_is_still_ordered_by_use():
    counts = collections.Counter()
    with open(LEDGER, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") == "call":
                counts[r.get("tool") or "?"] += 1
    ranked = [n for n in C.HOT if counts.get(n)]
    assert ranked == sorted(ranked, key=lambda n: -counts[n]), (
        "HOT is no longer in descending call order")


# -- against the real registry ----------------------------------------------------------------

def _registry():
    os.environ.setdefault("MCP_API_KEY", "test-only")
    os.environ.setdefault("MCP_TOOL_MAP", "1")
    try:
        import importlib
        return importlib.import_module("main")._ALL_TOOLS
    except Exception as exc:                       # pragma: no cover - env-dependent
        pytest.skip("main.py not importable here: %s" % exc)


def test_every_hot_name_is_a_real_tool():
    """A rename would silently shrink the head; the catalogue would still render."""
    allt = _registry()
    missing = [n for n in C.HOT if n not in allt]
    assert not missing, "HOT names no longer registered: %r" % (missing,)


def test_the_real_catalogue_lists_every_registered_tool():
    allt = _registry()
    got = C.render(allt)
    for n in allt:
        assert ("\n  %s(" % n) in got or ("\n  %s -- " % n) in got, "%s is missing" % n
