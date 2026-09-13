# -*- coding: utf-8 -*-
"""A name defined in two modules was skipped whenever either one was referenced.

The 2026-09-13 fix took half of it: a ZERO reference count resolves the ambiguity without
resolving the name, so every place is reported. The other half stayed:

    if len(places) != 1 and prod_refs[name]:
        continue                  # some definition IS reached; a name count cannot say which

**421 definitions sat behind that line**, against a visible baseline of 68 -- measured over
tracked non-test modules, where 106 public names are defined more than once. Attributing a
reference to the module the AST names reports eighteen of them; each was then checked by hand
against the owning module's own importers.

THE OBVIOUS DESIGN WOULD HAVE FOUND THREE, and the only reason that is a sentence rather than a
commit is that it was measured first:

    attribute by import, count every Name/Attribute        411 ambiguous,  3 reportable
    ...count only CALL positions                           401,           11
    ...+ self./cls. cannot reach a module-level function   401,           11
    ...+ a bare call resolves to a definition HERE         103,           28

TWO DEFECTS IN THE IMPLEMENTATION, both caught by measuring rather than by reading:

  * THE FIRST VERSION WAS A NO-OP. It filtered the candidate places and then fell through to
    `if prod_refs[name]: continue`, true by construction for everything reaching that branch.
    The scan reported the same 68 names; the attribution was thrown away one line later, and it
    looked exactly like a rule that had found nothing.

  * CREDITING ONLY CALLS WAS WRONG IN THE OTHER DIRECTION. `main.py` does
    `from tools.data_ops import read_json` and lists `read_json` in the TOOLS tuple; the gateway
    calls it later by dispatch. Six registered tools were reported unreached. A registration is
    a use and it is not a call.

So: credit widely (any reference the AST can attribute), doubt narrowly (only call positions
can make a name ambiguous). That pair can only produce a false "reached" -- the error this tool
already prefers to make loudly, per its own header: *prefer noise over silence.*
"""
from __future__ import annotations

import ast
import os
import sys
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import unreached as U  # noqa: E402


def _attribute(files_src):
    """Run `_attribute_calls` over a dict of {rel: source} and return its two maps."""
    from collections import defaultdict

    qualified, unattributable = defaultdict(int), defaultdict(int)
    files = list(files_src)
    for rel, src in files_src.items():
        tree = ast.parse(textwrap.dedent(src), filename=rel)
        U._attribute_calls(rel, tree, files, qualified, unattributable)
    return qualified, unattributable


# ── the rule that did the work ────────────────────────────────────────────────────────────

def test_a_bare_call_resolves_to_the_definition_in_the_same_file():
    """ORDINARY SCOPING, NOT A HEURISTIC, and the single change that took the yield from 11 to
    28. Five of the six files calling a bare `summarise` define their own."""
    q, u = _attribute({
        "a/mine.py": "def summarise(x):\n    return x\ndef go():\n    return summarise(1)\n",
    })
    assert q[("a/mine.py", "summarise")] == 1
    assert u["summarise"] == 0, "a call to a local definition is not evidence about anyone else"


def test_a_call_through_a_module_alias_names_its_module():
    q, _u = _attribute({
        "relay/turn_outcome.py": "def summarise(x):\n    return x\n",
        "relay/user.py": "from relay import turn_outcome as _to\n\n"
                         "def go():\n    return _to.summarise(1)\n",
    })
    assert q[("relay/turn_outcome.py", "summarise")] == 1


def test_a_direct_import_names_its_module():
    q, _u = _attribute({
        "relay/turn_outcome.py": "def summarise(x):\n    return x\n",
        "relay/user.py": "from relay.turn_outcome import summarise\n\n"
                         "def go():\n    return summarise(1)\n",
    })
    assert q[("relay/turn_outcome.py", "summarise")] == 1


def test_a_method_call_can_never_reach_a_module_level_function():
    """`self.check()` is a method. Counting it as ambiguity kept `check` unjudgeable."""
    _q, u = _attribute({
        "a/m.py": "class C:\n    def go(self):\n        return self.check()\n",
    })
    assert u["check"] == 0


def test_an_unattributable_call_is_recorded_as_doubt():
    """A bare call to a name this file neither defines nor imports could mean any definition."""
    _q, u = _attribute({"a/m.py": "def go():\n    return summarise(1)\n"})
    assert u["summarise"] == 1


def test_a_star_import_makes_every_call_in_that_file_ambiguous():
    """It binds names the scan cannot enumerate; guessing would be the silent failure again."""
    _q, u = _attribute({"a/m.py": "from b import *\n\ndef go():\n    return summarise(1)\n"})
    assert u["summarise"] >= 1


# ── credit widely, doubt narrowly ─────────────────────────────────────────────────────────

def test_a_registration_is_a_use_even_though_it_is_not_a_call():
    """THE SECOND DEFECT. main.py imports read_json and puts it in a tuple; the gateway calls it
    by dispatch. Crediting only call positions reported six registered tools as unreached."""
    q, _u = _attribute({
        "tools/data_ops.py": "def read_json(p):\n    return p\n",
        "main.py": "from tools.data_ops import read_json\n\nTOOLS = (read_json,)\n",
    })
    assert q[("tools/data_ops.py", "read_json")] >= 1


def test_a_non_call_reference_never_creates_doubt():
    """The asymmetry is the design: a mention credits, only a CALL can make a name ambiguous.
    Counting every Name/Attribute as doubt is the version measured at 411 ambiguous."""
    _q, u = _attribute({"a/m.py": "def go(rows):\n    return [r.summarise for r in rows]\n"})
    assert u["summarise"] == 0


# ── and the end-to-end property, against the real repository ──────────────────────────────

def test_the_scan_reports_more_than_the_bare_name_count_could():
    """THE NO-OP DEFECT, pinned. The first version returned exactly what it returned before."""
    rows = U.scan()
    assert rows is not None
    assert len(rows) > 70, (
        "the scan is back to the bare-name count; the attribution is being discarded "
        "(the first version filtered `places` and then fell through to `if prod_refs[name]`)")


def test_what_cannot_be_attributed_is_carried_out_rather_than_dropped():
    """A name the tool refuses to judge is as invisible as a row it never prints."""
    rows = U.scan()
    amb = getattr(rows, "ambiguous", None)
    assert isinstance(amb, list) and amb, "the scan no longer says what it could not decide"
    assert amb == sorted(set(amb))
    assert all(isinstance(n, str) for n in amb)


def test_the_reader_prints_the_ambiguity(capsys):
    assert U.main(["--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "not attributable" in out, out[-400:]
