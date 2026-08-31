# -*- coding: utf-8 -*-
"""Titles that identify a task, measured against the archive that made them necessary.

THE ARCHIVE: 424 conversations, 57 distinct titles. 174 named after a prompt preamble, 48
"Microsoft Copilot", 39 after the output-discipline block -- 213 rows carrying text identical
across unrelated tasks.

THE CAUSE was one line in fleet_runner._register_convs, which preferred Copilot's auto-generated
title and fell back to the goal. Copilot names a conversation from the opening of its first
message, and that is PROTOCOL. The goal that would have identified each row was in the same
expression, second.

WHAT THESE TESTS HOLD, beyond "it produces a string":

  * a title identifies, it does not summarise or claim an outcome
  * enumerating known boilerplate is FAIL-OPEN -- proven, not assumed: 174 rows are named after
    a preamble that exists nowhere in this codebase or in any stored goal, because the prompt
    that wrote it has since been deleted. Repetition catches what no list could.
  * the fallback is never empty and never a guess
"""
import pytest

from relay import conv_title as CT


# ── what a title must not be ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "Microsoft Copilot",
    "microsoft copilot",
    "新しいチャット",
    "",
    "  ",
    "ab",
    "。、",                       # punctuation only: a fragment however it arose
])
def test_titles_that_identify_nothing_are_rejected(bad):
    assert CT.is_useless(bad)


@pytest.mark.parametrize("good", [
    "ansible/ansible: retry on 429",
    "sympy",
    "takeuchifile操作",
    "README.md を3行で要約",
])
def test_real_labels_are_kept(good):
    assert not CT.is_useless(good)


# ── the extraction ────────────────────────────────────────────────────────────────────────

def test_a_swe_goal_is_named_by_its_repository_and_issue():
    goal = ("You are fixing a real bug in the open-source project **ansible/ansible** "
            "(language: python).\nThe repository is checked out locally at:\n  C:/w/p05\n\n"
            "== Issue to fix ==\n# Title\n\nStandardize PlayIterator state representation\n\n"
            "## Description\n\nlots of prose\n")
    t = CT.make_title(goal)
    assert t.startswith("ansible/ansible:")
    assert "PlayIterator" in t


def test_the_protocol_preamble_is_removed_before_anything_is_taken():
    from relay.copilot_autopilot_relay import PROTOCOL
    goal = PROTOCOL + "Desktop の Excel を集計して、結果を保存してください。"
    t = CT.make_title(goal)
    assert "call_tool" not in t
    assert "Excel" in t


def test_a_title_never_claims_an_outcome():
    """The DONE/STUCK badge already says what happened. A title that said "fixed the bug" for
    work that failed would be worse than the boilerplate it replaced."""
    t = CT.make_title("Fix the login redirect loop in auth.py")
    for word in ("DONE", "completed", "成功", "失敗", "fixed successfully"):
        assert word not in t


# ── disclosure ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("secret,marker", [
    (r"C:\Users\someone\secret\plan.xlsx を開いて", "<path>"),
    ("/home/someone/keys/id_rsa を読んで", "<path>"),
    ("send it to alice@example.com and confirm", "<email>"),
    ("conversation 7358917f-e5da-4cc7-b344-e285bd0ddbb1 を調べて", "<id>"),
    ("token deadbeefcafebabe1234 を検証", "<hash>"),
])
def test_a_title_redacts_what_a_sidebar_should_not_show(secret, marker):
    """The sidebar is read over shoulders in a way a transcript is not. An extractive title can
    only surface the goal's first clause -- and not even all of that."""
    t = CT.make_title(secret)
    assert marker in t or CT.is_useless(t) or t.startswith("Task ")
    assert "id_rsa" not in t and "alice@example.com" not in t


def test_length_is_capped():
    t = CT.make_title("x" * 500)
    assert len(t) <= CT.MAX_LEN + 1


# ── the fallback ──────────────────────────────────────────────────────────────────────────

def test_the_fallback_is_stable_and_never_empty():
    a = CT.neutral_title("key-1", when=1788160000.0)
    b = CT.neutral_title("key-1", when=1788160000.0)
    assert a == b and a.startswith("Task ") and len(a) > 8


def test_two_different_conversations_get_different_fallbacks():
    assert CT.neutral_title("key-1", 1788160000.0) != CT.neutral_title("key-2", 1788160000.0)


def test_an_unrecoverable_title_becomes_the_fallback_not_a_fragment():
    """Copilot had already truncated the stored title mid-preamble, so stripping the head left
    a tail. A unique meaningless string is not an improvement on a repeated one."""
    t = CT.make_title("。重いゴールは一発で終わらせようとせ")
    assert t.startswith("Task ") or not CT.is_useless(t)


# ── repetition, which is the rule that does not depend on remembering ──────────────────────

def test_repetition_is_detected_without_knowing_any_prompt():
    """THE POINT. 174 rows are named after a preamble that exists nowhere in this codebase --
    the prompt was deleted. No list of known boilerplate could catch it; being carried by many
    unrelated conversations is measurable from the corpus alone."""
    titles = ["a deleted prompt's opening"] * 40 + ["real one", "another"]
    rep = CT.repeated(titles, 3)
    assert "a deleted prompt's opening" in rep
    assert "real one" not in rep


def test_a_mass_produced_title_is_replaced_and_a_recurring_label_is_kept():
    """Measured counts in the archive: 174, 48, 39 are scaffolding; 27, 20, 20, 10 name a
    project or a task type. The threshold sits in the gap."""
    assert not CT.salvageable("preamble", 174)
    assert not CT.salvageable("preamble", 39)
    assert CT.salvageable("sympy", 20)
    assert CT.salvageable("takeuchifile操作", 27)


def test_disambiguation_keeps_the_true_part():
    t = CT.disambiguate("sympy", key="u1", when=1788160000.0)
    assert t.startswith("sympy") and len(t) > len("sympy")
    assert CT.disambiguate("sympy", "u1", 1788160000.0) != CT.disambiguate("sympy", "u2", 1788160000.0)


# ── the whole archive ─────────────────────────────────────────────────────────────────────

def test_the_real_archive_stops_repeating(tmp_path):
    """Runs the rule over the actual .fleet/conversations.json when it is present. Skips
    elsewhere -- a fresh clone has no archive, and that is not a failure."""
    import json
    import os
    from collections import Counter
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo, ".fleet", "conversations.json")
    if not os.path.isfile(path):
        pytest.skip("no local archive in this checkout")
    rows = json.load(open(path, encoding="utf-8"))
    rows = rows if isinstance(rows, list) else list(rows.values())
    if len(rows) < 50:
        pytest.skip("archive too small to say anything")

    first = [CT.make_title((r.get("title") or "").strip(),
                           existing=(r.get("title") or "").strip(),
                           key=(r.get("url") or r.get("title") or ""), when=r.get("ts"))
             for r in rows]
    counts = Counter(first)
    final = []
    for r, d in zip(rows, first):
        key = r.get("url") or (r.get("title") or "")
        n = counts[d]
        if n < 3:
            final.append(d)
        elif CT.salvageable(d, n):
            final.append(CT.disambiguate(d, key, r.get("ts")))
        else:
            final.append(CT.neutral_title(key, r.get("ts")))

    before = len({(r.get("title") or "").strip() for r in rows})
    assert len(set(final)) > before * 3, "titles are still collapsing onto each other"
    assert not CT.repeated(final, 3), "something is still shared by three or more rows"
    assert all(f.strip() for f in final), "a title came out empty"
