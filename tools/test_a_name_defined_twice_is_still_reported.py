# -*- coding: utf-8 -*-
"""A new function's NAME could retire an existing finding, and nothing said so.

`tools/unreached.py` counts references by BARE NAME, so with a name defined in two modules it
cannot say which definition a call reached. Its answer was to drop the name from the scan
entirely — the worst of the three available outcomes, because a row that is never printed is a
row nobody argues with.

MEASURED 2026-09-13, by walking into it. Adding a function called `require` to
`relay/invariants.py` removed `relay/selfimprove/autonomy.py::require` from the inventory:

    live unreached: 74
    stale: ['relay/selfimprove/autonomy.py::require']

That is the hard autonomy gate an adversarial review had flagged hours earlier, and the same
function a `.ps1` false-match had already nearly hidden once that day for an unrelated reason.
Two different blind spots, one function, and in both cases the symptom was a finding quietly
leaving the list.

THE FIX RESOLVES THE AMBIGUITY WITHOUT RESOLVING THE NAME. A zero reference count means NO
definition of that name is reached, whichever one a caller would have meant, so every place is
reported. Only a non-zero count is genuinely ambiguous, and that is the one case still skipped.
It immediately surfaced two entries that had never been printed at all — `compact`, defined in
both `bridge/session_store.py` and `relay/selfimprove/episode_record.py`.

The rule this follows is the one the burn-down already states: **a false "reached" is worse than
a false "unreached"**. Prefer noise over silence.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import unreached as U  # noqa: E402


def _write(tmp_path, rel, body):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return str(p)


def _scan(tmp_path, files, monkeypatch):
    monkeypatch.setattr(U, "REPO", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return {r[0]: r for r in (U.scan(files=files) or [])}


# ── the defect ────────────────────────────────────────────────────────────────────────────

def test_both_definitions_are_reported_when_neither_is_reached(tmp_path, monkeypatch):
    """THE DEFECT. Two modules, one name, no callers — and the name used to vanish."""
    _write(tmp_path, "a.py", "def twin():\n    return 1\n")
    _write(tmp_path, "b.py", "def twin():\n    return 2\n")
    rows = _scan(tmp_path, ["a.py", "b.py"], monkeypatch)
    assert set(rows) == {"a.py::twin", "b.py::twin"}, sorted(rows)


def test_an_attributable_call_resolves_which_definition_it_reached(tmp_path, monkeypatch):
    """THIS TEST USED TO ASSERT THE OPPOSITE, and the change is the point.

    It read "a reached name is still skipped rather than guessed" -- with a call in hand the
    bare-name count cannot say WHICH definition it meant, so both were skipped. True of a COUNT,
    and not true of the AST: `from a import twin` says exactly which one. Measured 2026-09-14,
    that skip was hiding **421 definitions** of 106 duplicated names, against a visible baseline
    of 68, and lifting it revealed eighteen -- each then checked by hand.

    So the fixture that used to prove the limitation now proves it is gone: `a.py::twin` is
    credited and `b.py::twin`, which nothing reaches, is reported. Reporting both would still be
    two findings where there is one; reporting NEITHER was one finding thrown away.
    """
    _write(tmp_path, "a.py", "def twin():\n    return 1\n")
    _write(tmp_path, "b.py", "def twin():\n    return 2\n")
    # THE CALLER HAS TO BE `main`. In a synthetic tree nothing is reachable unless it is an
    # ENTRYPOINT, and `main` is the only root this scanner has (PROTOCOL). The first version
    # used `go()`, which nothing calls -- so once scan() started iterating to a fixed point,
    # `go` was dead, the call inside it stopped counting, and `a.py::twin` was reported. The
    # test then read as a bug in attribution when attribution was right: a fixture that forgets
    # to give its chain a root is asserting that dead code is alive.
    _write(tmp_path, "c.py", "from a import twin\n\n\ndef main():\n    return twin()\n")
    rows = _scan(tmp_path, ["a.py", "b.py", "c.py"], monkeypatch)
    assert "a.py::twin" not in rows, "the definition the import names was reported anyway"
    assert "b.py::twin" in rows, "the definition nothing reaches is still being hidden"


def test_a_call_the_ast_cannot_place_is_still_skipped_rather_than_guessed(tmp_path,
                                                                          monkeypatch):
    """The case that is genuinely ambiguous, and remains so. A bare call in a file that neither
    defines nor imports the name could have meant either definition, and reporting both would be
    two false findings. It is SAID rather than silently dropped -- see `scan().ambiguous`."""
    _write(tmp_path, "a.py", "def twin():\n    return 1\n")
    _write(tmp_path, "b.py", "def twin():\n    return 2\n")
    # `main` for the same reason as above: the point here is that the call CANNOT BE PLACED, and
    # a caller that is itself dead would make both definitions unreported for the ordinary
    # reason instead -- the test would pass while saying nothing about ambiguity.
    _write(tmp_path, "c.py", "def main():\n    return twin()\n")
    rows = _scan(tmp_path, ["a.py", "b.py", "c.py"], monkeypatch)
    assert "a.py::twin" not in rows and "b.py::twin" not in rows, sorted(rows)


def test_a_single_definition_is_unaffected(tmp_path, monkeypatch):
    """The ordinary path, which carried every finding before this and must not have moved."""
    _write(tmp_path, "a.py", "def alone():\n    return 1\n")
    rows = _scan(tmp_path, ["a.py"], monkeypatch)
    assert "a.py::alone" in rows, sorted(rows)


def test_each_place_carries_its_own_line_number(tmp_path, monkeypatch):
    """Two rows for one name are only useful if each points at its own definition."""
    _write(tmp_path, "a.py", "def twin():\n    return 1\n")
    _write(tmp_path, "b.py", "# a comment\n# another\ndef twin():\n    return 2\n")
    rows = _scan(tmp_path, ["a.py", "b.py"], monkeypatch)
    assert rows["a.py::twin"][3] == 1
    assert rows["b.py::twin"][3] == 3


# ── the finding it was found by ───────────────────────────────────────────────────────────

def test_the_autonomy_gate_is_visible_in_this_repository():
    """PINNED BY NAME, because it is the one that disappeared. `require` now exists in two
    modules' worth of vocabulary and the gate must still be on the list."""
    rows = U.scan()
    if rows is None:
        pytest.skip("git could not list the tracked files here")
    keys = {r[0] for r in rows}
    assert "relay/selfimprove/autonomy.py::require" in keys, (
        "自律ゲートがまた名前衝突で消えている")


def test_the_names_the_fix_revealed_are_on_the_list():
    """They had never been printed once. If a caller appears for either, the ratchet's other
    half fails and they come off — which is the only way they should leave.

    `compact` (bridge/session_store.py + relay/selfimprove/episode_record.py) was the pair this
    fix originally surfaced, but 9b63a94 wired bridge/session_store.py::compact behind a CLI, so
    it left BASELINE entirely -- an example that fixed itself is no longer an example of
    anything. `compare` (bench/skill_probe.py + bench/companionbench/shadow_rules.py) is still
    defined twice and still unreached in production today (checked with tools/unreached.py), so
    it now carries the property this test exists to pin: a colliding name stays on the list
    instead of quietly dropping out. Also note REASONS no longer uses the "revealed" kind for
    either name -- the 2026-09-24 C-1 close-out reclassified every entry that used to sit as
    "revealed" (which only said HOW a row arrived) into an actual verdict, "deliberate" for both
    `compare` rows. What matters here is that a reason is on record at all, not which of
    ALLOWED_REASONS it is.
    """
    import importlib

    m = importlib.import_module("tools.test_nothing_new_is_built_without_a_caller")
    for k in ("bench/skill_probe.py::compare",
              "bench/companionbench/shadow_rules.py::compare"):
        assert k in m.BASELINE, k
        assert m.REASONS.get(k, ("", ""))[0] in m.ALLOWED_REASONS, k
