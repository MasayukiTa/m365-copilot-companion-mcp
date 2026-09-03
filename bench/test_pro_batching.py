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
import io
import json
import os

import pytest

from bench import pro_cycle as C


# -- how wide a batch may be -------------------------------------------------------------

def test_cheap_languages_run_several_at_a_time():
    """The intent stands; the example was wrong.

    This used python as "the cheap one" on an estimate of 120 MB. Measured, openlibrary cost
    about 1900 MB an instance -- python is the EXPENSIVE one, and js the cheaper. What the test
    is really for is that a cheap language still parallelises when there is room, or the whole
    per-language table is decoration.
    """
    assert C.concurrency_for(["js"], free=8.0) >= 3


def test_the_heavier_language_stays_narrower_at_the_same_free_space():
    """The point of the table: same disk, different width, because the cost differs.

    The direction is measured, not assumed. python (~1900 MB, openlibrary) is heavier than js
    (~1150 MB, element-web), so python must run narrower -- this test used to assert the
    reverse on an estimate that was wrong by a factor of sixteen.
    """
    free = 8.0
    assert C.concurrency_for(["python"], free) < C.concurrency_for(["js"], free)


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


def test_redoing_captured_work_has_to_be_asked_for(tmp_path, monkeypatch):
    """The rule this asserted has been made more precise, so the assertion follows it.

    It used to require the literal source `if redo_captured else captured_ids()`, pinning one
    line rather than the behaviour -- and that line was wrong: it skipped an instance because a
    patch FILE existed, graded or not. With the eval host down, graded_ids() was empty and that
    retired every instance after a single attempt; the run scored 59.0% where the same 39
    instances scored 70.0% when 21 of them got a second try.

    What must stay true is narrower, and is checked directly rather than through the source:
    anything already MEASURED is never re-run, and anything merely held is not mistaken for
    measured.
    """
    import json as _j
    monkeypatch.setattr(C, "RESULTS", str(tmp_path / "r.json"))
    monkeypatch.setattr(C, "PREDS", str(tmp_path / "p.json"))
    monkeypatch.setattr(C, "ATTEMPTS", str(tmp_path / "a.json"))
    open(str(tmp_path / "r.json"), "w", encoding="utf-8").write(
        _j.dumps({"graded_pass": True, "graded_fail": False}))
    open(str(tmp_path / "p.json"), "w", encoding="utf-8").write(
        _j.dumps([{"instance_id": "held", "patch": "diff"}]))

    graded = C.graded_ids()
    assert graded == {"graded_pass", "graded_fail"}, graded
    assert "held" not in graded, "an ungraded patch counted as measured"
    assert "held" not in C.exhausted_ids(), "an ungraded patch was retired unmeasured"


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


def test_the_widths_follow_the_table_rather_than_a_remembered_order():
    """Direction taken FROM the table, not asserted about particular languages.

    This used to say go runs narrower than python, which was true when python was costed at
    120 MB. Measured, python is the heaviest thing in the slice (~1900 MB, openlibrary) and go
    is lighter (~1360 MB), so the old assertion is now backwards -- the same staleness the
    ordering test above warns about in its own docstring.
    """
    cheap = min(C.LANG_DISK_MB, key=lambda k: C.LANG_DISK_MB[k])
    dear = max(C.LANG_DISK_MB, key=lambda k: C.LANG_DISK_MB[k])
    free = 12.0          # generous, so the two widths can actually differ
    assert C.concurrency_for([dear], free) <= C.concurrency_for([cheap], free), (
        "the heavier language (%s) is not running narrower than the lighter (%s)" % (dear, cheap))


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


# -- the gap between two floors, where a doomed batch lives --------------------------------

def test_a_batch_is_not_started_when_the_fleet_could_not_admit_anyone(monkeypatch):
    """MEASURED. This cycle's floor is checked BEFORE staging; staging is what spends the disk.
    Two js worktrees took free space from 4.51 GB to 2.91 GB -- under the FLEET's own 3.0 GB
    admission floor, a different and lower number. The fleet started, correctly refused to admit
    either worker, and sat there: both read `turn=0 pending` for half an hour, on course to burn
    a full one-hour timeout having run nothing.

    Nothing in any log said "disk". The gate is silent because deferring admission is its normal
    behaviour, which is what makes this worth a pre-flight question."""
    import relay.relay_fleet as RF
    import relay.fleet_runner as FR
    monkeypatch.setattr(FR, "settings_disk_floor", lambda *a, **k: 3.0)
    monkeypatch.setattr(RF, "disk_admission_ok", lambda **k: False)
    assert C._fleet_can_admit() is False
    monkeypatch.setattr(RF, "disk_admission_ok", lambda **k: True)
    assert C._fleet_can_admit() is True


def test_it_asks_the_fleets_own_predicate_rather_than_copying_it():
    """A second implementation of an admission rule drifts from the first, and the failure it
    produces -- a fleet that runs but admits nobody -- is silent by construction."""
    import inspect
    src = inspect.getsource(C._fleet_can_admit)
    assert "from relay.relay_fleet import disk_admission_ok" in src
    assert "settings_disk_floor" in src


def test_an_unaskable_predicate_does_not_stop_the_run(monkeypatch):
    """This is a pre-flight convenience. It must never become the thing that halts a benchmark
    on its own -- that would be a new failure introduced by a guard against an old one."""
    import relay.relay_fleet as RF
    monkeypatch.setattr(RF, "disk_admission_ok",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert C._fleet_can_admit() is True


def test_the_check_runs_after_staging_not_before():
    """SOURCE-LEVEL, stated as such. Asking before staging answers the wrong question: the
    disk it is protecting has not been spent yet at that point."""
    import inspect
    src = inspect.getsource(C.cycle)
    assert src.index("_write_contracts(group)") < src.index("_fleet_can_admit()")
    assert src.index("_fleet_can_admit()") < src.index('"--max-concurrent"')


# -- the units, which did not agree across a module boundary -------------------------------

def test_free_space_is_read_in_the_same_unit_the_fleet_uses():
    """THE DEFECT THAT STAGED TWO UNRUNNABLE BATCHES TONIGHT. This module divided by 1e9 and
    the fleet's predicate divides by 1024**3, so the same disk read 3.10 here and 2.89 there --
    a 7% gap, always optimistic. The cycle believed it was above the fleet's 3.0 floor while the
    fleet correctly refused to admit anyone, and the batch sat until it was killed.

    Both numbers were called "GB" and neither said which one."""
    import shutil
    from relay.relay_fleet import free_disk_gb
    assert abs(C.free_gb() - free_disk_gb()) < 0.01, (
        "pro_cycle reads %.3f, the fleet reads %.3f -- different units again"
        % (C.free_gb(), free_disk_gb()))
    decimal = shutil.disk_usage("C:/").free / 1e9
    assert C.free_gb() < decimal, "still reading decimal GB, not GiB"


def test_the_fleet_floor_is_read_from_the_fleet_not_restated():
    """A second copy of a number that already exists goes stale, and this one is compared
    against a reading taken here, so a mismatch would be silent."""
    import inspect
    src = inspect.getsource(C._fleet_floor_gib)
    assert "settings_disk_floor" in src


def test_concurrency_reserves_the_fleets_floor_not_this_cycles():
    """The fleet is what refuses to open a tab, so its floor is the one that decides whether a
    batch can run at all. Reserving only this cycle's floor is how a batch gets staged that the
    fleet will not touch."""
    import inspect
    src = inspect.getsource(C.concurrency_for)
    assert "FLEET_FLOOR_GIB" in src
    assert "1024.0" in src, "still computing headroom in decimal MB"


def test_a_disk_just_above_the_fleet_floor_yields_one_at_a_time():
    """Directly above the floor there is room for one and nothing else."""
    assert C.concurrency_for(["go"], C.FLEET_FLOOR_GIB + 0.2) == 1
    assert C.concurrency_for(["python"], C.FLEET_FLOOR_GIB + 0.2) == 1


def test_heavy_languages_go_serial_at_the_disk_this_machine_actually_has():
    """Measured on this machine: ~4.2 GiB free after a clean discard. Two teleport worktrees
    did not fit, twice."""
    free = 4.24
    # EVERYTHING is serial at this disk now, python included. The line that expected python to
    # run three-wide here came from costing it at 120 MB; openlibrary was then measured at
    # ~1900 MB an instance, and four of them took this machine from 8.19 GiB to 0.62 GiB free.
    # "Heavy languages go serial" was always the intent -- python simply turned out to be one.
    for lang in ("go", "js", "python"):
        assert C.concurrency_for([lang], free) == 1, (
            "%s is not serial at %.2f GiB free" % (lang, free))


# -- the reclaim that did not reclaim ------------------------------------------------------

def test_the_go_toolchain_is_actually_found():
    """THE DEFECT THAT MADE THE RECLAIM A NO-OP. The fallback path was written with
    backslashes and the file ended up holding a control byte where the "b" of "bin" belonged,
    so it named a path that does not exist. `go` is not on this process's PATH either, so the
    lookup found nothing by both routes -- and 1.46 GB of module cache went uncleared while the
    run stopped for lack of disk. Fixing it recovered 2,636 MB on the first real call."""
    import os
    import shutil as sh
    assert sh.which("go") or os.path.exists("C:/Program Files/Go/bin/go.exe"), \
        "neither PATH nor the fallback locates go; the reclaim would silently do nothing"


def test_the_source_holds_no_control_bytes():
    """A heredoc turned "\bin" into a backspace character once. It is invisible in a diff, it
    breaks a path silently, and the whole repository was checked: this was the only one."""
    import io
    import os
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pro_cycle.py"),
                  encoding="utf-8", newline="").read()
    bad = [hex(ord(c)) for c in src if ord(c) < 32 and c not in "\n\r\t"]
    assert not bad, "control bytes in the source: %s" % bad[:5]


def test_a_negative_delta_is_never_reported_as_freed(monkeypatch):
    """Other processes write to this disk while a cache is being emptied -- the live run's own
    worktrees above all. The first version subtracted the two readings and logged "npm cache
    freed -151 MB", which is not merely wrong but backwards."""
    seq = iter([10.0, 9.0, 9.0, 9.0])          # free space FALLS across the clear
    monkeypatch.setattr(C, "free_gb", lambda *a: next(seq))
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(C.shutil, "which", lambda exe: "C:/fake/%s.exe" % exe)
    for _name, mb in C._clear_toolchain_caches():
        assert mb is None or mb >= 0, "reported a negative amount freed"


def test_an_unreachable_toolchain_is_reported_not_omitted(monkeypatch):
    """"the go toolchain was not reachable" is the difference between a cache that was empty
    and one that was never touched -- and the second is why a run stopped with 1.46 GB in it."""
    monkeypatch.setattr(C.shutil, "which", lambda exe: None)
    monkeypatch.setattr(C.os.path, "exists", lambda p: False)
    out = dict(C._clear_toolchain_caches())
    assert out["go module cache"] is None
    assert out["npm cache"] is None


def test_a_refused_oversize_patch_is_not_retried_forever(tmp_path, monkeypatch):
    """A blanked patch looks identical to "produced nothing", but the worker produced far too
    much rather than nothing, and repeating it does not help: 3,054,501 bytes on the first
    attempt and 74,850,968 on the second, each costing a full batch slot on the quota that is
    the binding constraint here. Retrying it is now a deliberate act."""
    import json
    p = tmp_path / "preds.json"
    p.write_text(json.dumps([
        {"instance_id": "produced-nothing", "patch": ""},
        {"instance_id": "produced-too-much", "patch": "", "refused": "oversize: 74850968 bytes"},
        {"instance_id": "fine", "patch": "diff --git a/x b/x\n"},
    ]), encoding="utf-8")
    monkeypatch.setattr(C, "PREDS", str(p))
    got = C.captured_ids()
    assert "fine" in got
    assert "produced-too-much" in got, "an oversize refusal is still retried every run"
    assert "produced-nothing" not in got, "a genuine no-op must still be retried"


# -- the graded ledger, which was never actually read ---------------------------------------

def _results(tmp_path, monkeypatch, text):
    p = tmp_path / "results.json"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(C, "RESULTS", str(p))
    return p


def test_the_results_ledger_is_jsonl_and_is_read_as_such(tmp_path, monkeypatch):
    """THE DEFECT. swe_grade_batch appends one object per line; this read the file with
    json.load, which raises on line 2; _load caught ValueError and returned an empty dict.
    Measured: four graded rows on disk and graded_ids() returning zero. The "do not re-measure
    what is already scored" protection -- the one the module docstring names as how a benchmark
    number drifts upward without anybody deciding to cheat -- was a no-op the whole time, and
    it failed in the direction that looks like nothing is wrong."""
    import json
    _results(tmp_path, monkeypatch, "\n".join([
        json.dumps({"instance_id": "a", "verdict": "RESOLVED"}),
        json.dumps({"instance_id": "b", "verdict": "not"}),
    ]))
    assert C.graded_ids() == {"a", "b"}


def test_an_evalerr_row_does_not_retire_an_instance(tmp_path, monkeypatch):
    """EVALERR means the evaluation could not be RUN. Counting it would make the failure
    permanent: the instance is skipped forever and can never be scored once the cause is gone."""
    import json
    _results(tmp_path, monkeypatch, "\n".join([
        json.dumps({"instance_id": "a", "verdict": "RESOLVED"}),
        json.dumps({"instance_id": "b", "verdict": "EVALERR", "note": "launch failed"}),
    ]))
    assert C.graded_ids() == {"a"}


def test_a_torn_line_does_not_lose_the_rest_of_the_ledger(tmp_path, monkeypatch):
    import json
    _results(tmp_path, monkeypatch,
             json.dumps({"instance_id": "a", "verdict": "RESOLVED"}) + "\n"
             + '{"instance_id": "trunc\n'
             + json.dumps({"instance_id": "c", "verdict": "not"}) + "\n")
    assert C.graded_ids() == {"a", "c"}


def test_the_other_shapes_are_still_accepted(tmp_path, monkeypatch):
    """The file's format is not this function's decision to make, and a reader that only
    understands the shape it happens to see is how this started."""
    import json
    _results(tmp_path, monkeypatch, json.dumps({"a": "RESOLVED", "b": "EVALERR"}))
    assert C.graded_ids() == {"a"}
    _results(tmp_path, monkeypatch, json.dumps([{"instance_id": "x", "verdict": "not"}]))
    assert C.graded_ids() == {"x"}


def test_a_missing_ledger_means_nothing_is_graded(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "RESULTS", str(tmp_path / "absent.json"))
    assert C.graded_ids() == set()


# -- what is left when this slice is done ----------------------------------------------------

def _slice(tmp_path, name, ids):
    p = tmp_path / name
    p.write_text(json.dumps(list(ids)), encoding="utf-8")
    return str(p)


def test_a_finished_slice_names_the_slice_that_is_not_finished(tmp_path, monkeypatch):
    """The stall this closes. The fresh draw was split into a go file and a non-go file, each
    launched by hand; the non-go one printed "cycle end" and the pipeline sat idle for ninety
    minutes with fifteen instances never started. Nothing was broken -- the report was scoped to
    one file and the work was not."""
    monkeypatch.setattr(C, "SW", str(tmp_path))
    monkeypatch.setattr(C, "graded_ids", lambda: {"done1"})
    monkeypatch.setattr(C, "refused_ids", lambda: set())
    monkeypatch.setattr(C, "exhausted_ids", lambda: set())
    monkeypatch.setattr(C, "burned_ids", lambda: set())
    mine = _slice(tmp_path, "pro_slice_mine.json", ["done1"])
    _slice(tmp_path, "pro_slice_other.json", ["done1", "left1", "left2"])

    assert [(os.path.basename(p), n) for p, n in C.remaining_elsewhere(mine)] == [
        ("pro_slice_other.json", 2)
    ]


def test_a_burned_slice_is_not_offered_as_work(tmp_path, monkeypatch):
    """The bug this function shipped with.

    pro_slice50_full.json holds fifty instances, every one of them burned. The first version
    counted them as unfinished and printed the command to run them, which is a prompt to
    re-measure a slice that has already seen its own answers. Burned is not merely done; running
    it is the one thing a benchmark must never do quietly.
    """
    monkeypatch.setattr(C, "SW", str(tmp_path))
    monkeypatch.setattr(C, "graded_ids", lambda: set())
    monkeypatch.setattr(C, "refused_ids", lambda: set())
    monkeypatch.setattr(C, "exhausted_ids", lambda: set())
    monkeypatch.setattr(C, "burned_ids", lambda: {"b1", "b2"})
    mine = _slice(tmp_path, "pro_slice_mine.json", ["x"])
    _slice(tmp_path, "pro_slice_burned.json", ["b1", "b2"])
    _slice(tmp_path, "pro_slice_live.json", ["b1", "fresh1"])

    assert [(os.path.basename(p), n) for p, n in C.remaining_elsewhere(mine)] == [
        ("pro_slice_live.json", 1)
    ]


def test_an_unreadable_slice_does_not_break_the_finish_line(tmp_path, monkeypatch):
    """This runs after the work is done. A cycle that completed fifteen instances must not end
    in a traceback over a malformed file it was never using."""
    monkeypatch.setattr(C, "SW", str(tmp_path))
    monkeypatch.setattr(C, "graded_ids", lambda: set())
    monkeypatch.setattr(C, "refused_ids", lambda: set())
    monkeypatch.setattr(C, "exhausted_ids", lambda: set())
    monkeypatch.setattr(C, "burned_ids", lambda: set())
    (tmp_path / "pro_slice_bad.json").write_text("{not json", encoding="utf-8")
    _slice(tmp_path, "pro_slice_good.json", ["one"])

    assert [(os.path.basename(p), n)
            for p, n in C.remaining_elsewhere(str(tmp_path / "pro_slice_mine.json"))] == [
        ("pro_slice_good.json", 1)
    ]


def _cycle_fn():
    import ast
    src = io.open(os.path.join(os.path.dirname(__file__), "pro_cycle.py"),
                  encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "cycle":
            return node
    raise AssertionError("cycle() not found")


def test_every_exit_reports_what_is_left():
    """The report has to be on the SILENT exit too, and that is the one it was missing.

    "nothing ungraded in the slice -- done" is what a finished slice prints, and it returned
    without a word about the other slice file. That is the ending the ninety-minute stall was
    standing on: the run was correct, complete, and quiet.
    """
    import ast
    fn = _cycle_fn()
    exits = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    reports = [n for n in ast.walk(fn)
               if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "report_what_is_left"]
    assert len(reports) >= 2, (
        "report_what_is_left must be called at both endings; found %d call(s) for %d return(s)"
        % (len(reports), len(exits))
    )


def test_the_cycle_never_sends_anyone_to_the_lite_grader():
    """The advice line said "grade them with bench.swe_grade_batch". That is the Lite grader,
    and every verdict it returns for a Pro instance is EVALERR -- so following the advice
    reproduces the bug the run had just hit."""
    import ast
    src = io.open(os.path.join(os.path.dirname(__file__), "pro_cycle.py"),
                  encoding="utf-8").read()
    said = [n.value for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "grade them with" in n.value]
    assert said, "the advice line disappeared; this test is now measuring nothing"
    for line in said:
        assert "swe_grade_batch" not in line, line
        assert "pro_grade_remote" in line, line


# -- correcting a verdict --------------------------------------------------------------------

def test_a_later_row_corrects_an_earlier_one(tmp_path, monkeypatch):
    """A measurement that turns out to be false has to be retractable.

    The eval host's root filesystem came up read-only, docker could not pull, the harness
    returned None for fourteen instances and the wrapper recorded each as not-resolved -- all
    fourteen in 87 seconds. Appending EVALERR rows to retract them did nothing: graded_ids()
    added an instance the moment any row graded it and no later row could take it back, so the
    count went 62 -> 77 and stayed. An append-only log of statements about the same subject
    means the latest one.
    """
    results = tmp_path / "r.json"
    results.write_text(
        json.dumps({"instance_id": "a", "verdict": "not"}) + "\n"
        + json.dumps({"instance_id": "b", "verdict": "RESOLVED"}) + "\n"
        + json.dumps({"instance_id": "a", "verdict": "EVALERR"}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(C, "RESULTS", str(results))
    assert C.graded_ids() == {"b"}


def test_a_later_real_verdict_replaces_an_earlier_error(tmp_path, monkeypatch):
    """The correction runs both ways: an instance that errored and was then graded for real is
    graded. Otherwise a single bad session would retire an instance from the ledger."""
    results = tmp_path / "r.json"
    results.write_text(
        json.dumps({"instance_id": "a", "verdict": "EVALERR"}) + "\n"
        + json.dumps({"instance_id": "a", "verdict": "RESOLVED"}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(C, "RESULTS", str(results))
    assert C.graded_ids() == {"a"}


# -- the tree the verification needs ---------------------------------------------------------

def _cycle_statement_order():
    """Line numbers of the steps whose ORDER is the thing under test, inside cycle()."""
    import ast
    src = io.open(os.path.join(os.path.dirname(__file__), "pro_cycle.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cycle")
    marks = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name in ("_shadow_verify", "_discard") and name not in marks:
                marks[name] = node.lineno
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Constant) and sub.value == "pro_grade_remote.py"
                        and "grade" not in marks):
                    marks["grade"] = node.lineno
                if (isinstance(sub, ast.Constant) and sub.value == "--keep-worktrees"
                        and "keep" not in marks):
                    marks["keep"] = node.lineno
    return marks


def test_the_worktrees_survive_until_verification_has_run():
    """THE STEP HAD NEVER ONCE SEEN A WORKTREE.

    pro_capture deletes each tree as soon as it has the diff, and _shadow_verify then ran the
    acceptance commands with cwd pointing at that deleted directory. Measured across the whole
    history of the feature: 81 of 81 verification records carry an EMPTY tree hash, because
    there was nothing to hash. The commands fell back to the repository root, where
    `go test ./...` reports "directory prefix . does not contain main module" in about 0.15s --
    forty VERIFY_FAILED verdicts about code that was never executed, from the step this module
    calls the only place DONE is produced.
    """
    marks = _cycle_statement_order()
    assert "keep" in marks, (
        "pro_capture must be called with --keep-worktrees, or it deletes the worktree that "
        "the verification step is about to run in. NOT plain --keep: that also holds the "
        "routed CONTAINER, and forty of those fill a volume with 25 GB free -- two different "
        "resources behind one flag, which is how a correctness fix bought a disk regression.")


def test_the_trees_are_discarded_before_the_grade_not_after_it():
    """--keep must not turn into holding worktrees across the long step. The grade reads the
    captured patch file and talks to the eval host; it has no use for a tree, and it can run for
    three hours. Discarding after it would trade a correctness fix for a disk regression."""
    marks = _cycle_statement_order()
    assert marks["_shadow_verify"] < marks["_discard"] < marks["grade"], (
        "order must be: verify (needs the tree), discard, then grade -- got %s" % marks)


# -- the width is a reading, not a decision ---------------------------------------------------

def test_batch_width_follows_the_disk_as_it_changes(monkeypatch):
    """WIDTH WAS DECIDED ONCE PER LANGUAGE AND NEVER REVISITED.

    Measured on the re-run of 2026-09-03: the python group was entered at 17:44 with 4.55 GiB
    free, which fixed width=1. Three gigabytes were then reclaimed, free reached 7.53 GiB --
    comfortably enough for two -- and the cycle went on staging one instance at a time for the
    rest of the run. The batch lines even printed the new free figure while ignoring it.

    Free space moves by about 3 GB inside a batch as worktrees are created and discarded, so a
    width taken from one instant is a guess about every instant after it.
    """
    readings = iter([4.55, 7.53, 7.53, 7.53])
    monkeypatch.setattr(C, "free_gb", lambda *a, **k: next(readings, 7.53))
    monkeypatch.setattr(C, "lang_of", lambda i: "python")
    ids = ["i%d" % n for n in range(5)]
    sizes = [len(g) for g in C.batches(ids, 0)]
    assert sizes[0] == 1, "the first batch is sized by the disk at the time: %r" % sizes
    assert 2 in sizes[1:], (
        "later batches must re-read the disk; got %r, which is the defect this pins" % sizes)


def test_an_explicit_batch_size_still_wins(monkeypatch):
    """--batch N is an instruction, not a hint; the disk must not override it."""
    monkeypatch.setattr(C, "free_gb", lambda *a, **k: 3.2)
    monkeypatch.setattr(C, "lang_of", lambda i: "python")
    assert [len(g) for g in C.batches(["a", "b", "c"], 2)] == [2, 1]


def test_every_instance_is_yielded_exactly_once(monkeypatch):
    """The rewrite slices a list while iterating it -- the obvious way to drop or repeat work."""
    monkeypatch.setattr(C, "free_gb", lambda *a, **k: 7.53)
    monkeypatch.setattr(C, "lang_of", lambda i: "python")
    ids = ["i%d" % n for n in range(7)]
    out = [i for g in C.batches(ids, 0) for i in g]
    assert out == ids, out


# -- the log must not cut the diagnosis -------------------------------------------------------

def test_an_error_line_keeps_enough_to_be_actionable():
    """MEASURED TWICE ON 2026-09-03, BOTH TIMES UNNOTICED AT THE TIME.

    Fleet output was logged at ln[:160] whatever it said. The network outage that ended the
    first run was recorded as

        FETCH_FAIL: fatal: unable to access 'https://github.com/qutebrowser/qutebrowser.git/':

    cut off exactly where git names the cause, leaving DNS, TLS, proxy and auth
    indistinguishable. A worker's STUCK reason was cut mid-word at "ConnectionClosedError: no
    close frame r". Both full texts existed only in status.json, which the next batch
    overwrites.
    """
    err = ("FETCH_FAIL: fatal: unable to access 'https://github.com/x/y.git/': "
           + "schannel: failed to receive handshake, SSL/TLS connection failed " * 3)
    assert C._log_width(err) == 600
    # The property that matters is that nothing is cut, not that some length is exceeded --
    # the first version of this assertion tested the length of its own fixture and failed.
    assert err[:C._log_width(err)] == err, "the error must survive whole"
    assert len(err) > 160, "fixture must be long enough that the old 160 cap would have cut it"


def test_a_progress_line_stays_short():
    """The cap exists so a chatty fleet does not drown the log; that still holds."""
    assert C._log_width("w0   turn=3  running  (waiting on the previous turn)") == 160


def test_the_stuck_reason_that_was_cut_mid_word_now_survives():
    reason = ("reason: not re-sent: the turn may already have been delivered and this goal "
              "performs an action (delivery=unknown, ConnectionClosedError: no close frame "
              "received or sent)")
    kept = reason[:C._log_width(reason)]
    assert kept.endswith("received or sent)"), kept[-40:]
