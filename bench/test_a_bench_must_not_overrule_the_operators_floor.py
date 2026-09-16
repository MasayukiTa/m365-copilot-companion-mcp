# -*- coding: utf-8 -*-
"""A benchmark may turn the disk gate off. It may not turn the OPERATOR's floor off.

fleet_runner resolves the floor as CLI > settings.txt > env, so passing --disk-floor-gb at all
BEATS the cockpit's control. relay/task_router.py learned this on 2026-09-15: the panel showed
1 GB, the operator had set it there, and an autostarted run used 2 -- the panel was displaying
a number the run did not use. It was fixed to pass the flag only when settings.txt carries no
floor.

The same hard-coding survived in two bench orchestrators as a bare "0" in a list of literals.
In both files the surrounding comment defends --no-fanout at length and says nothing at all
about the 0, which is how you recognise a constant nobody has had to justify.

WHY NOT SIMPLY DELETE THE OVERRIDE. On an eval host there is no settings.txt, and a bench run
that defers on headroom produces an empty row rather than a result -- it is measuring a model,
not admission. So the flag stays where nobody has chosen, and yields where someone has. That
is the same shape task_router settled on, and the standing rule on the owner's workstation is
to free disk rather than to lower the floor.
"""
from __future__ import annotations

import io
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


@pytest.mark.parametrize("module,attr", [
    ("bench.review_run", "_bench_disk_floor_args"),
    ("bench.swe_solve_decoupled", "_bench_disk_floor_args"),
])
def test_the_flag_yields_to_a_floor_the_operator_chose(module, attr, monkeypatch, tmp_path):
    """BEHAVIOURAL, both ways: a chosen floor wins, an unset one leaves the bench alone."""
    import importlib
    from tools import settings_path as SP
    mod = importlib.import_module(module)
    fn = getattr(mod, attr)

    path = tmp_path / "settings.txt"
    monkeypatch.setattr(SP, "NEW_PATH", str(path))
    monkeypatch.delenv("APPDATA", raising=False)

    # NOBODY HAS CHOSEN: the bench keeps its own behaviour.
    assert fn() == ["--disk-floor-gb", "0"]

    # THE OPERATOR HAS CHOSEN: the flag is not passed, so settings.txt decides.
    path.write_text("disk_floor_gb=1\n", encoding="utf-8")
    assert fn() == []

    # ZERO IS A CHOICE TOO. The cockpit clamps that control to 0..100 and therefore offers 0;
    # treating it as "unset" would substitute a number for one the operator picked -- and the
    # result would be identical here, which is exactly why it has to be asserted rather than
    # assumed.
    path.write_text("disk_floor_gb=0\n", encoding="utf-8")
    assert fn() == []


def test_nothing_hard_codes_a_floor_override_any_more():
    """The literal that was copied into two orchestrators. A third appearing is the drift
    returning -- and it returns quietly, because a bench that runs to completion looks like a
    bench that is working."""
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if d not in (".git", ".venv", "node_modules", "__pycache__", "output",
                                "outputs", ".fleet")]
        for name in files:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            path = os.path.join(root, name)
            body = io.open(path, encoding="utf-8", errors="replace").read()
            if re.search(r'"--disk-floor-gb"\s*,\s*"0"', body):
                # The conditional helper is allowed to name it -- that is where the decision
                # lives. What is forbidden is a command line that carries it unconditionally.
                if "_bench_disk_floor_args" in body and "operator_set_a_disk_floor" in body:
                    continue
                offenders.append(os.path.relpath(path, REPO))
    assert not offenders, "these pass --disk-floor-gb 0 unconditionally: %r" % offenders


def test_the_question_has_one_owner():
    """task_router asked it privately first; two more callers appeared. Three copies of
    'has the operator chosen a floor' would disagree the way five copies of the settings path
    did."""
    from relay.fleet_runner import operator_set_a_disk_floor
    assert callable(operator_set_a_disk_floor)
    src = io.open(os.path.join(REPO, "relay", "task_router.py"),
                  encoding="utf-8", errors="replace").read()
    assert "operator_set_a_disk_floor" in src, \
        "task_router grew its own copy of the answer again"
