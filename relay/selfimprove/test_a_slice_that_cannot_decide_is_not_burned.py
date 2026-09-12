# -*- coding: utf-8 -*-
"""A configuration that cannot reach a verdict burned the slice anyway, and said so too late.

MEASURED 2026-09-11/12. A run was launched with `--n 100` against the default `min_n=100`. The
OFF arm lost one chunk of twenty to a staging failure, so the paired McNemar had N=80,
`significance_gate` returned `underpowered`, and `burned.add(fresh)` then consumed all 100
fresh instances for a verdict that says nothing.

BURNING IS RIGHT ON ITS OWN TERMS. Those instances were seen by the system under test, so they
are contaminated whatever the verdict was; a registry that only burned on success would leak
the failures back into later slices. What was wrong is that the arithmetic was knowable before
anything ran and nothing said it.

    n <  min_n   cannot produce a verdict even with zero attrition   -> refuse
    n == min_n   produces one only if NOTHING is lost                -> warn, name the measure
    n >  min_n   the default is 200 for exactly this reason

THE POOL IS FINITE. SWE-bench Verified is 500 instances and there is no more of it. Reporting
what remains as a NUMBER OF RUNS is what turns "we ran out" from a discovery made at the end of
a night into something an operator can plan against before choosing --n.

(Measured while writing this: of 232 burned ids only 182 are in this spec, so the remaining
pool was 318 rather than the 500-232=268 I had assumed. The report is computed, not derived
from a count of the registry, precisely because those two are not the same question.)
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from relay.selfimprove import loop as L  # noqa: E402


class _Burned:
    """A registry that records what it was asked to burn, and burns nothing by itself."""

    def __init__(self, already=()):
        self.already = set(already)
        self.added = []

    def __len__(self):
        return len(self.already)

    def filter_fresh(self, ids):
        return [i for i in ids if i and i not in self.already]

    def add(self, ids, **kw):
        self.added.append(list(ids))


def _spec(tmp_path, n, prefix="a__a-"):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps([{"instance_id": "%s%d" % (prefix, i)} for i in range(n)]),
                 encoding="utf-8")
    return str(p)


def _run(tmp_path, monkeypatch, spec_n, n, min_n, burned=None):
    monkeypatch.setattr(L, "SWEDIR", str(tmp_path))
    monkeypatch.setattr(L, "G", type("G", (), {"BurnedRegistry": lambda *a, **k: burned}))
    logged = []
    monkeypatch.setattr(L, "log", lambda m: logged.append(str(m)))
    out = L.validate(toggle="T", spec_path=_spec(tmp_path, spec_n), n=n, seed=1,
                     dataset_key="Verified", alpha=0.05, min_n=min_n, min_pp=1.0,
                     chunk=20, conc=3, turns=50, floor=7.0, dry_run=True,
                     burned_path="x")
    return out, "\n".join(logged)


# ── the refusal ───────────────────────────────────────────────────────────────────────────

def test_a_slice_smaller_than_min_n_is_refused_before_anything_burns(tmp_path, monkeypatch):
    """THE ONE THAT WASTES A SLICE FOR NOTHING. With fewer instances than min_n the verdict is
    `underpowered` by construction, no matter how perfectly the run goes."""
    b = _Burned()
    out, log = _run(tmp_path, monkeypatch, spec_n=50, n=50, min_n=100, burned=b)
    assert out["burned"] is False
    assert out["status"] == "refused_underpowered_by_construction"
    assert "REFUSING" in log and "guaranteed" in log
    assert b.added == [], "a refused configuration burned instances anyway"


def test_the_refusal_names_what_would_fix_it(tmp_path, monkeypatch):
    _, log = _run(tmp_path, monkeypatch, spec_n=50, n=50, min_n=100, burned=_Burned())
    assert "--n" in log and "--min-n" in log


# ── the warning ───────────────────────────────────────────────────────────────────────────

def test_zero_margin_is_allowed_but_named(tmp_path, monkeypatch):
    """n == min_n is the operator's call -- it is not arithmetically hopeless, only fragile.
    The warning quotes the attrition that was actually measured rather than inventing one."""
    out, log = _run(tmp_path, monkeypatch, spec_n=300, n=100, min_n=100, burned=_Burned())
    assert "WARNING" in log and "ZERO attrition" in log
    assert "20 of 100" in log, "the warning does not say what was actually lost"
    assert out.get("status") != "refused_underpowered_by_construction"


def test_a_healthy_margin_says_nothing_alarming(tmp_path, monkeypatch):
    _, log = _run(tmp_path, monkeypatch, spec_n=300, n=200, min_n=100, burned=_Burned())
    assert "REFUSING" not in log
    assert "ZERO attrition" not in log


# ── the pool ──────────────────────────────────────────────────────────────────────────────

def test_the_remaining_pool_is_reported_in_runs(tmp_path, monkeypatch):
    """"We ran out" should be a number chosen against, not discovered at the end of a night."""
    _, log = _run(tmp_path, monkeypatch, spec_n=300, n=100, min_n=100, burned=_Burned())
    assert "pool:" in log and "more run(s)" in log
    assert "200 fresh remain" in log, log


def test_the_pool_excludes_burned_ids_that_belong_to_this_spec_only(tmp_path, monkeypatch):
    """MEASURED WHILE WRITING THIS. Of 232 burned ids only 182 were in the spec, so
    len(spec) - len(registry) overstated the burn by 50 and understated the pool. The pool has
    to be computed by filtering the spec, not by subtracting a registry's size."""
    b = _Burned(already=["a__a-%d" % i for i in range(20)] + ["other__other-%d" % i
                                                              for i in range(50)])
    _, log = _run(tmp_path, monkeypatch, spec_n=300, n=100, min_n=100, burned=b)
    # 300 in the spec, 20 of them burned -> 280 fresh, 100 drawn -> 180 left
    assert "180 fresh remain" in log, log


def test_asking_how_much_is_left_does_not_take_any(tmp_path, monkeypatch):
    b = _Burned()
    _run(tmp_path, monkeypatch, spec_n=300, n=100, min_n=100, burned=b)
    assert b.added == [], "reporting the pool burned instances"


# ── the helper the report is built on ─────────────────────────────────────────────────────

def test_spec_ids_reads_both_shapes(tmp_path):
    as_dicts = tmp_path / "d.json"
    as_dicts.write_text(json.dumps([{"instance_id": "a__a-1"}, {"instance_id": "a__a-2"}]),
                        encoding="utf-8")
    assert L._spec_ids(str(as_dicts)) == ["a__a-1", "a__a-2"]

    as_strings = tmp_path / "s.json"
    as_strings.write_text(json.dumps(["a__a-1", "a__a-2"]), encoding="utf-8")
    assert L._spec_ids(str(as_strings)) == ["a__a-1", "a__a-2"]

    keyed = tmp_path / "k.json"
    keyed.write_text(json.dumps({"instance_ids": ["a__a-1"]}), encoding="utf-8")
    assert L._spec_ids(str(keyed)) == ["a__a-1"]


def test_an_unreadable_pool_does_not_stop_the_run(tmp_path, monkeypatch):
    """The pool line is a courtesy. A spec shape it cannot read must cost the log line, never
    the run -- an informational failure that blocks work is worse than no information."""
    b = _Burned()
    monkeypatch.setattr(L, "_spec_ids", lambda p: (_ for _ in ()).throw(ValueError("nope")))
    out, log = _run(tmp_path, monkeypatch, spec_n=300, n=200, min_n=100, burned=b)
    assert "REFUSING" not in log
    assert out is not None
