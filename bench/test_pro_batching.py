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
    """Order matters when a run is cut short -- and this one has been, three times now.
    Front-loading the cheap instances means an interrupted run still captured most of the
    slice.

    ASSERTED AGAINST THE TABLE, not against a fixed order. The first version of this test
    hardcoded go ahead of js, which was true only while go was mis-costed at 200 MB; when the
    real figure turned out to be ~670 MB the test failed for being right about the old number.
    A test that pins a measurement it does not own goes stale the moment the measurement is
    corrected."""
    lang = {"n1": "js", "p1": "python", "g1": "go"}
    monkeypatch.setattr(C, "lang_of", lambda i: lang[i])
    order = [i for group in C.batches(list(lang), 1) for i in group]
    cost = [C.LANG_DISK_MB[lang[i]] for i in order]
    assert cost == sorted(cost), "batches are not ordered cheapest-first: %s" % list(zip(order, cost))
    assert order[0] == "p1", "python is the cheapest and must run first"


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


# -- discarding a batch, which was failing silently ---------------------------------------

def test_a_worktree_with_read_only_files_is_actually_deleted(tmp_path, monkeypatch):
    """THE DEFECT THIS REPLACES. Windows marks the files under .git read-only and rmtree
    cannot unlink those. With ignore_errors=True each batch left 4-8 MB behind and said
    nothing; eight batches in, eight directories were still there and the log had never
    mentioned it once. A cleanup whose failure mode is silence is not a cleanup."""
    import os
    import stat
    work = tmp_path / "work" / "p00_abc" / ".git" / "objects"
    work.mkdir(parents=True)
    f = work / "pack"
    f.write_bytes(b"x" * 100)
    os.chmod(str(f), stat.S_IREAD)
    monkeypatch.setattr(C, "SW", str(tmp_path))
    logged = []
    monkeypatch.setattr(C, "log", logged.append)

    C._discard()

    assert not (tmp_path / "work" / "p00_abc").exists()
    assert not any("could not delete" in str(m) for m in logged)


def test_what_survives_deletion_is_reported(tmp_path, monkeypatch):
    """A directory a live process still holds cannot be removed, and that is exactly the case
    worth naming -- it means a worker outlived its batch."""
    import os
    work = tmp_path / "work" / "p01_held"
    work.mkdir(parents=True)
    (work / "f").write_bytes(b"x")
    monkeypatch.setattr(C, "SW", str(tmp_path))
    monkeypatch.setattr(C.shutil, "rmtree", lambda *a, **k: None)
    logged = []
    monkeypatch.setattr(C, "log", logged.append)

    C._discard()

    assert any("could not delete 1 worktree dir(s)" in str(m) and "p01_held" in str(m)
               for m in logged)


# -- making room instead of moving the line ------------------------------------------------

def test_a_tight_disk_reclaims_before_it_gives_up(monkeypatch):
    """The standing rule is to free space, not to lower the floor. The cycle must therefore
    read the floor again AFTER reclaiming, and only stop if it is still short."""
    seen = {"trimmed": 0}

    def fake_trim():
        seen["trimmed"] += 1
        return 609.0, ["copilot-eval-edge: freed 609 MB"]

    import relay.edge_recover as E
    monkeypatch.setattr(E, "trim_profile_caches", fake_trim)
    monkeypatch.setattr(C, "free_gb", lambda *a: 3.9)
    monkeypatch.setattr(C, "log", lambda *a: None)

    assert C._reclaim(3.0) == 3.9
    assert seen["trimmed"] == 1


def test_a_failing_reclaim_does_not_kill_the_run(monkeypatch):
    """A benchmark must not die trying to make room for itself."""
    import relay.edge_recover as E
    monkeypatch.setattr(E, "trim_profile_caches",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(C, "log", lambda *a: None)
    assert C._reclaim(3.0) == 3.0


def test_the_floor_itself_is_never_relaxed_to_get_a_batch_through():
    """SOURCE-LEVEL, stated as such. Lowering the floor is the one remedy this repository
    has a standing rule against, and it would look exactly like a small edit here."""
    import io
    import os
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pro_cycle.py"),
                  encoding="utf-8").read()
    body = src[src.index("for n, group in enumerate(batches"):]
    body = body[:body.index("log(\"-\" * 72)")]
    assert "DISK_FLOOR_GB =" not in body, "the floor is being reassigned to fit a batch"
    assert body.count("_reclaim(") == 1


# -- not redoing work whose result is already on disk --------------------------------------

def test_an_instance_with_a_captured_patch_is_not_run_again(tmp_path, monkeypatch):
    """GRADING IS A SEPARATE, OFFLINE STEP and it can be down for reasons that have nothing to
    do with the work. The eval host's docker was down for a whole run, so every batch returned
    EVALERR, the graded set stayed empty, and a restart re-ran instances whose patch was
    already on disk -- about eighty minutes of the tenant quota that is the binding constraint
    here, spent reproducing a file that already existed."""
    import json
    p = tmp_path / "preds.json"
    p.write_text(json.dumps([
        {"instance_id": "a", "patch": "diff --git a/x b/x\n"},
        {"instance_id": "b", "model_patch": "diff --git a/y b/y\n"},
    ]), encoding="utf-8")
    monkeypatch.setattr(C, "PREDS", str(p))
    assert C.captured_ids() == {"a", "b"}


def test_an_empty_patch_does_not_count_as_captured(tmp_path, monkeypatch):
    """An instance that ran and produced nothing is a result worth retrying, not one worth
    keeping. Counting it would quietly retire the failures."""
    import json
    p = tmp_path / "preds.json"
    p.write_text(json.dumps([{"instance_id": "a", "patch": ""},
                             {"instance_id": "b", "patch": "   \n"},
                             {"instance_id": "c", "patch": "diff --git a/z b/z\n"}]),
                 encoding="utf-8")
    monkeypatch.setattr(C, "PREDS", str(p))
    assert C.captured_ids() == {"c"}


def test_a_missing_or_broken_preds_file_means_nothing_is_captured(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "PREDS", str(tmp_path / "absent.json"))
    assert C.captured_ids() == set()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(C, "PREDS", str(bad))
    assert C.captured_ids() == set()


def test_redoing_captured_work_has_to_be_asked_for():
    """SOURCE-LEVEL, stated as such: cycle() runs a fleet. Skipping is the default and
    re-running is the deliberate act, not the other way round."""
    import inspect
    src = inspect.getsource(C.cycle)
    assert "redo_captured" in src
    assert "if redo_captured else captured_ids()" in src.replace("\n", " ").replace("  ", " ")


# -- the worktree map, which grew without bound --------------------------------------------

def _merge(prev, batch, isdir):
    """The merge rule as pro_stage_goals applies it, exercised directly.

    Extracted rather than reimplemented in the assertions: the rule is three lines and the
    interesting part is which entries survive, which is what these tests are about.
    """
    merged = {k: v for k, v in prev.items() if k in batch or isdir(str(v))}
    merged.update(batch)
    return merged


def test_a_discarded_worktree_is_dropped_from_the_map():
    """Measured: 15 entries of which 11 pointed at directories that were gone. Every later
    capture re-walked them and every ledger lookup for them returned nothing. A worktree that
    has been discarded cannot be diffed or attributed to, and dropping it loses nothing
    recoverable -- the directory is what the patch would have been read from."""
    prev = {"old1": "/gone/a", "old2": "/gone/b", "kept": "/here/c"}
    out = _merge(prev, {}, isdir=lambda p: p == "/here/c")
    assert set(out) == {"kept"}


def test_earlier_batches_that_still_exist_are_kept():
    """THE REASON THE MAP MERGES AT ALL. A map holding only the last batch made every earlier
    batch's evidence unreachable, so the recorder reported fifty measured while the join could
    cover four."""
    prev = {"batch1": "/here/a", "batch2": "/here/b"}
    out = _merge(prev, {"batch3": "/here/c"}, isdir=lambda p: True)
    assert set(out) == {"batch1", "batch2", "batch3"}


def test_this_batchs_entries_survive_even_before_their_directories_exist():
    """They are about to be created. Testing existence on them would delete the map entries
    for the batch that is starting."""
    out = _merge({}, {"new": "/not/yet"}, isdir=lambda p: False)
    assert out == {"new": "/not/yet"}


def test_a_re_staged_instance_takes_the_new_path():
    out = _merge({"x": "/old/path"}, {"x": "/new/path"}, isdir=lambda p: True)
    assert out == {"x": "/new/path"}


def test_the_pruning_rule_is_the_one_in_the_stager():
    """SOURCE-LEVEL, stated as such: the stager clones repositories, so the rule above is
    exercised as a copy. This catches the copy and the original drifting apart."""
    import io
    import os
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "pro_stage_goals.py"), encoding="utf-8").read()
    assert "if k in wtmap or os.path.isdir(str(v))" in src


# -- the cost that lives outside the worktree ----------------------------------------------

def test_go_is_costed_by_its_module_cache_not_its_checkout():
    """THE MEASUREMENT THAT WAS WRONG. go was costed at 200 MB, which is what a go WORKTREE
    weighs. The cost that matters is the module cache `go test ./...` fills in ~/go/pkg/mod,
    outside the worktree, where the per-batch discard cannot see it. Three go instances put
    2.01 GB there in ninety minutes and drove the run into the disk floor."""
    assert C.LANG_DISK_MB["go"] >= 600, "go is costed as if only its checkout mattered"


def test_go_now_runs_narrower_than_python_at_the_same_disk():
    assert C.concurrency_for(["go"], 4.7) < C.concurrency_for(["python"], 4.7)


def test_toolchain_caches_are_cleared_only_when_still_short(monkeypatch):
    """They cost a re-download to rebuild, so clearing them between every batch would trade
    the disk problem for a network one. As a last resort before STOPPING, it is plainly worth
    it -- it recovered 2.05 GB and let a stalled run continue."""
    called = []
    monkeypatch.setattr(C, "_clear_toolchain_caches",
                        lambda: called.append(1) or [("go module cache", 2051.0)])
    import relay.edge_recover as E
    monkeypatch.setattr(E, "trim_profile_caches", lambda: (0.0, []))
    monkeypatch.setattr(C, "log", lambda *a: None)

    monkeypatch.setattr(C, "free_gb", lambda *a: C.DISK_FLOOR_GB + 1.0)
    C._reclaim(2.0)
    assert called == [], "cleared the toolchain caches while there was room"

    monkeypatch.setattr(C, "free_gb", lambda *a: C.DISK_FLOOR_GB - 0.5)
    C._reclaim(2.0)
    assert called == [1], "did not clear them even though the run was about to stop"


def test_clearing_the_caches_never_raises(monkeypatch):
    """A benchmark must not die trying to make room for itself, and these shell out to
    toolchains that may not be installed."""
    monkeypatch.setattr(C.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no go")))
    assert isinstance(C._clear_toolchain_caches(), list)
