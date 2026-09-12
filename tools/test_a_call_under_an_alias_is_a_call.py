# -*- coding: utf-8 -*-
"""The instrument that finds unreached code could not see a call made under an alias.

MEASURED 2026-09-13. `relay/selfimprove/diversify.py::diversify` sat in this repository's
unreached BASELINE -- frozen as known-unreached, exempt from CI forever -- and is called in
production:

    relay/solve_policy.py:24   from relay.selfimprove.diversify import diversify as _diversify
    relay/solve_policy.py:56       genomes = _diversify(base, n)

`scan()` counted references by walking for `ast.Name` and `ast.Attribute`. An aliased import is
neither -- it is `ast.alias(name="diversify", asname="_diversify")` -- and the call site records
the Name `_diversify`. Nothing credited `diversify`.

WHY A FALSE POSITIVE HERE IS AS BAD AS A FALSE NEGATIVE. The baseline is being burned to zero
because it turned "broken" into "known". A wrong entry inside it does the mirror thing: someone
checking the list sees live production code listed as unreached. An inventory with false
entries cannot justify deleting anything, so the instrument was fixed before the list was acted
on. Two of the 93 were wrong.

AND THE FIRST FIX WAS WRONG, which is why the second test exists. Crediting every REFERENCE to
an alias hid `relay/selfimprove/harness_tree.py::branches`, because
`from relay.selfimprove import branches as BR` imports a MODULE that happens to share the
function's name. `from X import y as z` cannot be told from a module import syntactically, so
the discriminator is USE: a function alias gets CALLED (`_diversify(...)`), a module alias gets
ATTRIBUTED (`BR.something()`). Widening a blind spot into a blind eye is the worse trade -- a
false negative here is a live unreached function that never appears at all.

These run against the real repository rather than fixtures. `scan()` reads files under the repo
root, and a fixture would have to be written into the repo to be seen; more to the point, what
is being checked is a claim about THIS code, and the transcript of what the tool says about it
is the evidence.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import unreached  # noqa: E402


def _names(files):
    rows = unreached.scan(files=files) or []
    return {name for _key, name, _rel, _ln, _sp, _tr in rows}


# ── the defect ────────────────────────────────────────────────────────────────────────────

def test_a_function_called_under_an_alias_is_not_reported_unreached():
    """THE MEASURED CASE. solve_policy imports it as `_diversify` and calls it."""
    caller = os.path.join(REPO, "relay", "solve_policy.py")
    callee = os.path.join(REPO, "relay", "selfimprove", "diversify.py")
    if not (os.path.isfile(caller) and os.path.isfile(callee)):
        pytest.skip("the files this was measured on are not present")
    assert "diversify" not in _names(["relay/solve_policy.py",
                                      "relay/selfimprove/diversify.py"]), (
        "エイリアス経由の呼び出しが見えていない -- 現役の本番関数が『未到達』として"
        "ベースラインに凍結される")


def test_importing_a_module_of_the_same_name_does_not_credit_the_function():
    """THE FIRST FIX'S OWN DEFECT. `from relay.selfimprove import branches as BR` imports a
    MODULE; `harness_tree.py::branches` is an unrelated function. Crediting every reference to
    an alias made a live unreached function disappear from the report entirely."""
    tree = os.path.join(REPO, "relay", "selfimprove", "harness_tree.py")
    user = os.path.join(REPO, "relay", "selfimprove", "compare.py")
    if not (os.path.isfile(tree) and os.path.isfile(user)):
        pytest.skip("the files this was measured on are not present")
    assert "branches" in _names(["relay/selfimprove/compare.py",
                                 "relay/selfimprove/harness_tree.py"]), (
        "同名モジュールの alias import を関数の呼び出しと誤認している -- 未到達の関数が"
        "報告から消える（偽陽性より悪い）")


# ── the rule the fix must not have broken ─────────────────────────────────────────────────

def test_an_import_alone_is_still_not_a_call():
    """The whole point of the tool. `campaigns_from_ledger` was imported by its tests and
    called by nobody, and the crash-recovery path it exists for was broken for weeks. If
    importing counted as reaching, that defect would have been invisible from the start.
    """
    src = open(os.path.join(REPO, "tools", "unreached.py"), encoding="utf-8").read()
    i = src.index("aliased = {}")
    block = src[i:i + 1400]
    assert "ast.Call" in block, (
        "エイリアスを『参照されたら』加点している -- import しただけで到達扱いになる")
    assert "isinstance(node.func, ast.Name)" in block


def test_the_scan_still_returns_the_shape_its_readers_expect():
    rows = unreached.scan(files=["tools/unreached.py"])
    assert rows is not None
    for row in rows:
        assert len(row) == 6
        key, name, rel, lineno, span, tests = row
        assert key == "%s::%s" % (rel, name)
        assert isinstance(lineno, int) and isinstance(span, int)
