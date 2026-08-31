# -*- coding: utf-8 -*-
"""How many benchmark instances may run at once, and why it is not a constant.

THE MEASUREMENT THAT FORCED THIS. The fresh forty is 16 python, 11 go, 11 js, 2 ts. A staged
checkout is 19-22 MB whatever the language; the cost that matters arrives afterwards, when the
worker installs dependencies. A NodeBB worktree reached 564 MB that way. An ansible one did not
grow. Batching in slice order made every instance pay the price of the heaviest one in its
batch, so 27 cheap instances ran at the pace set by node_modules.

WHAT IS NOT ALLOWED AS A FIX. Lowering the fleet's 3.0 GB disk floor to force more through. The
floor exists because this machine has run out of disk mid-benchmark before, and the standing
rule is to free space, not to move the line. So concurrency here is derived FROM the floor: what
is left after reserving it, divided by what one instance of this language costs.
"""
import pytest

from bench import pro_cycle as C


# -- how wide a batch may be -------------------------------------------------------------

def test_cheap_languages_run_several_at_a_time():
    """python is the biggest group in the slice and the cheapest to run. If it does not
    parallelise, nothing does, and the whole change is decoration."""
    assert C.concurrency_for(["python"], free=5.0) >= 3


def test_node_stays_narrower_than_python_at_the_same_free_space():
    """The point of the table. Same disk, different width, because the cost differs."""
    free = 5.0
    assert C.concurrency_for(["js"], free) < C.concurrency_for(["python"], free)


def test_a_tight_disk_falls_back_to_one_at_a_time():
    """Just above the floor there is room for the run and nothing else. One is progress;
    zero is a hang, and widening by lowering the floor is the forbidden move."""
    assert C.concurrency_for(["js"], free=3.10) == 1
    assert C.concurrency_for(["python"], free=3.06) == 1


def test_below_the_floor_it_still_says_one_rather_than_zero():
    """This function does not decide whether to run -- the fleet's own floor does, and it
    refuses. Returning 0 here would instead produce an empty batch and silent no-progress."""
    assert C.concurrency_for(["python"], free=2.0) == 1
    assert C.concurrency_for(["python"], free=0.0) == 1


def test_it_is_capped_even_with_a_huge_disk():
    """Disk is not the only limit. Every extra concurrent worker raises the chance the shared
    tool-planner refuses -- median 35 concurrent replies at a refusal against 5 at a recovery."""
    assert C.concurrency_for(["python"], free=500.0) <= 4


def test_an_unknown_language_is_costed_as_expensive():
    """Fail closed. A language absent from the table is one nobody measured, and guessing it
    is cheap is the assumption that fills the disk."""
    assert C.concurrency_for(["fortran"], 5.0) <= C.concurrency_for(["python"], 5.0)


def test_a_mixed_batch_pays_the_heaviest_price():
    assert C.concurrency_for(["python", "js"], 5.0) == C.concurrency_for(["js"], 5.0)


# -- the grouping ------------------------------------------------------------------------

def test_batches_never_mix_languages(monkeypatch):
    """Mixing puts a cheap instance in a batch sized for an expensive one, which is the
    defect this replaces."""
    lang = {"a": "python", "b": "js", "c": "python", "d": "go"}
    monkeypatch.setattr(C, "lang_of", lambda i: lang[i])
    for group in C.batches(list(lang), 2):
        assert len({lang[i] for i in group}) == 1


def test_every_instance_appears_exactly_once(monkeypatch):
    """Grouping must not silently drop work. A benchmark that skips instances reports a
    pass rate for a set nobody chose."""
    ids = ["i%02d" % n for n in range(40)]
    lang = {i: ["python", "go", "js", "ts"][n % 4] for n, i in enumerate(ids)}
    monkeypatch.setattr(C, "lang_of", lambda i: lang[i])
    out = [i for group in C.batches(ids, 3) for i in group]
    assert sorted(out) == sorted(ids)
    assert len(out) == len(set(out))


def test_cheap_languages_go_first(monkeypatch):
    """Order matters when a run is cut short -- and this one has been, twice. Front-loading
    the cheap instances means an interrupted run still graded most of the slice."""
    lang = {"n1": "js", "p1": "python", "g1": "go"}
    monkeypatch.setattr(C, "lang_of", lambda i: lang[i])
    order = [i for group in C.batches(list(lang), 1) for i in group]
    assert order.index("p1") < order.index("n1")
    assert order.index("g1") < order.index("n1")


def test_an_explicit_size_overrides_the_disk_calculation(monkeypatch):
    """--batch 1 has to keep meaning one. It is how a run is made watchable, and it was the
    setting that got the last slice through a 2.1 GB disk."""
    monkeypatch.setattr(C, "lang_of", lambda i: "python")
    monkeypatch.setattr(C, "free_gb", lambda *a: 500.0)
    assert all(len(g) == 1 for g in C.batches(["a", "b", "c"], 1))


def test_size_zero_asks_the_disk(monkeypatch):
    monkeypatch.setattr(C, "lang_of", lambda i: "python")
    monkeypatch.setattr(C, "free_gb", lambda *a: 3.06)
    assert all(len(g) == 1 for g in C.batches(["a", "b", "c"], 0))
    monkeypatch.setattr(C, "free_gb", lambda *a: 50.0)
    assert max(len(g) for g in C.batches(["a", "b", "c"], 0)) > 1


def test_an_empty_slice_yields_no_batches(monkeypatch):
    monkeypatch.setattr(C, "lang_of", lambda i: "python")
    assert list(C.batches([], 0)) == []


# -- the table is about the real slice ---------------------------------------------------

def test_the_languages_in_the_real_slice_are_all_costed():
    """A language in the corpus but not in the table falls to the pessimistic default, which
    is safe but wastes the whole point. This names the gap when the corpus changes."""
    from bench import pro_stage_goals as G
    present = {(r.get("repo_language") or "") for r in G.FULL}
    missing = {l for l in present if l and l not in C.LANG_DISK_MB}
    assert not missing, "uncosted languages fall back to pessimistic serial: %s" % sorted(missing)


def test_the_fleet_is_told_the_batch_size_not_a_constant():
    """SOURCE-LEVEL, stated as such: running the fleet needs a browser. Sizing the batch
    against the disk and then opening a fixed number of workers spends RAM and admission
    slots on tabs with no instance behind them."""
    import io
    import os
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pro_cycle.py"),
                  encoding="utf-8").read()
    assert '"--max-concurrent", str(len(group))' in src, \
        "the fleet's width no longer follows the batch it was given"
