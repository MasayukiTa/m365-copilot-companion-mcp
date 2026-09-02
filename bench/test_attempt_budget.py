"""An ungraded patch is not a finished instance.

THE MEASUREMENT THIS ENCODES, on the same 39 instances:

    2026-08-30    7 got 1 attempt -> 42.9% resolved
                 21 got 2         -> 81.0%              28/40 = 70.0%
    2026-09-01   37 got 1 attempt
                  3 got 2                               23/39 = 59.0%

Per-attempt quality did not fall; the second attempt stopped happening. `captured_ids()` --
"a patch file exists" -- was unioned into the skip set whether or not that patch had ever been
graded, and with the eval host down `graded_ids()` was empty, so having a file was the only
gate and every instance retired after one try.
"""
import io
import json
import os

import pytest

from bench import pro_cycle as C


@pytest.fixture(autouse=True)
def _files(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "PREDS", str(tmp_path / "preds.json"))
    monkeypatch.setattr(C, "RESULTS", str(tmp_path / "results.json"))
    monkeypatch.setattr(C, "ATTEMPTS", str(tmp_path / "attempts.json"))
    monkeypatch.setattr(C, "MAX_ATTEMPTS", 2)
    return tmp_path


def _preds(tmp_path, rows):
    io.open(str(tmp_path / "preds.json"), "w", encoding="utf-8").write(
        json.dumps(rows, ensure_ascii=False))


def _attempts(tmp_path, d):
    io.open(str(tmp_path / "attempts.json"), "w", encoding="utf-8").write(
        json.dumps(d, ensure_ascii=False))


def test_a_captured_but_ungraded_patch_is_not_finished(_files):
    # THE REGRESSION. One attempt, a patch on disk, nobody has checked whether it works.
    _preds(_files, [{"instance_id": "i1", "patch": "diff --git a/x b/x"}])
    _attempts(_files, {"i1": 1})
    assert "i1" not in C.exhausted_ids()
    assert "i1" not in C.refused_ids()


def test_it_is_finished_once_the_attempts_are_spent(_files):
    _preds(_files, [{"instance_id": "i1", "patch": "diff"}])
    _attempts(_files, {"i1": 2})
    assert "i1" in C.exhausted_ids()


def test_a_refused_diff_is_never_retried(_files):
    # Measured: 3,054,501 bytes on the first attempt and 74,850,968 on the second. Retrying an
    # over-large diff spends a batch slot to get a worse answer.
    _preds(_files, [{"instance_id": "big", "refused": True, "patch": ""}])
    _attempts(_files, {"big": 1})
    assert "big" in C.refused_ids()


def test_a_refusal_is_not_confused_with_a_capture(_files):
    _preds(_files, [{"instance_id": "big", "refused": True, "patch": ""},
                    {"instance_id": "ok", "patch": "diff"}])
    assert C.refused_ids() == {"big"}
    assert "ok" in C.captured_ids()
    assert "ok" not in C.refused_ids()


def test_the_counter_increments(_files):
    C.note_attempts(["a", "b"])
    C.note_attempts(["a"])
    counts = C.attempt_counts()
    assert counts["a"] == 2 and counts["b"] == 1


def test_the_cap_is_reached_only_by_real_attempts(_files):
    C.note_attempts(["a"])
    assert C.exhausted_ids() == set()
    C.note_attempts(["a"])
    assert C.exhausted_ids() == {"a"}


def test_a_missing_counter_file_is_not_an_error(_files):
    assert C.attempt_counts() == {}
    assert C.exhausted_ids() == set()


def test_the_counter_never_raises_on_an_unwritable_path(monkeypatch, _files):
    # Losing the counter must not stop a benchmark run. Undercounting costs an extra attempt;
    # raising costs the whole cycle.
    monkeypatch.setattr(C, "ATTEMPTS", "Z:/definitely/not/a/place/attempts.json")
    C.note_attempts(["a"])          # must not raise


def test_a_graded_instance_is_still_never_re_run(_files):
    # The anti-drift rule the module is built around: a measured instance is not measured
    # again, whatever its verdict. Widening the retry must not have widened this.
    io.open(str(_files / "results.json"), "w", encoding="utf-8").write(
        json.dumps({"instance_id": "g1", "verdict": "not"}) + "\n")
    assert "g1" in C.graded_ids()


def test_an_exhausted_instance_is_skipped_even_with_no_patch(_files):
    # An instance that burns its attempts producing nothing must also stop, or a worker that
    # reliably crashes would be retried for ever.
    _attempts(_files, {"crashy": 2})
    assert "crashy" in C.exhausted_ids()
    assert "crashy" not in C.captured_ids()


def test_a_results_file_with_one_row_is_read_as_a_row(_files):
    # ONE JSONL ROW IS ALSO VALID JSON, so it parsed as a dict and was read as an
    # {instance_id: verdict} mapping -- graded_ids() returned the FIELD NAMES
    # {"instance_id", "verdict"} and the instance that had actually been graded was missing,
    # so it got re-run. Every results file looks like this after the first grading.
    io.open(str(_files / "results.json"), "w", encoding="utf-8").write(
        json.dumps({"instance_id": "g1", "verdict": "RESOLVED"}) + "\n")
    got = C.graded_ids()
    assert got == {"g1"}, got


def test_several_rows_still_work(_files):
    io.open(str(_files / "results.json"), "w", encoding="utf-8").write(
        json.dumps({"instance_id": "g1", "verdict": "RESOLVED"}) + "\n"
        + json.dumps({"instance_id": "g2", "verdict": "not"}) + "\n")
    assert C.graded_ids() == {"g1", "g2"}


def test_the_mapping_shape_still_works(_files):
    # The grader writes {instance_id: bool}; both shapes have to keep working, which is the
    # whole reason the reader accepts more than one.
    io.open(str(_files / "results.json"), "w", encoding="utf-8").write(
        json.dumps({"a1": True, "a2": False}))
    assert C.graded_ids() == {"a1", "a2"}


def test_the_cycle40_condition_no_longer_retires_everything(_files):
    """The regression, reproduced as a condition rather than as a story.

    cycle40 ran while the eval host's docker was down, so the results ledger stayed empty and
    39 of 40 instances had a patch on disk from their first attempt. Measured against the real
    gate under exactly that state: the old rule left ONE instance to run and retired the other
    39 unmeasured, holding whatever they had produced. This asserts the shape of that, so the
    next person who unions "a file exists" into the skip set has to argue with a number.
    """
    ids = ["i%02d" % n for n in range(40)]
    # grading unavailable -> results file absent -> graded_ids() empty
    _preds(_files, [{"instance_id": i, "patch": "diff --git a/x b/x"} for i in ids[:39]])

    old_gate = C.graded_ids() | C.captured_ids()
    assert len([i for i in ids if i not in old_gate]) == 1, (
        "the old gate is not being reproduced; this test no longer measures the regression")

    new_gate = C.graded_ids() | C.refused_ids() | C.exhausted_ids()
    assert len([i for i in ids if i not in new_gate]) == 40, (
        "an ungraded patch is being treated as a finished instance again")


def test_the_cap_still_ends_it(_files):
    # The other half: retrying must not become endless. Two attempts and the same condition
    # stops the run rather than spending the quota for ever.
    ids = ["i%02d" % n for n in range(40)]
    _preds(_files, [{"instance_id": i, "patch": "diff"} for i in ids])
    C.note_attempts(ids)
    C.note_attempts(ids)
    left = [i for i in ids if i not in (C.graded_ids() | C.refused_ids() | C.exhausted_ids())]
    assert left == [], "the attempt cap did not stop the retries: %d left" % len(left)


def test_a_graded_instance_is_untouched_by_the_widening(_files):
    # Widening the retry must not have widened re-measurement. A graded instance stays graded
    # whatever its verdict, which is the rule that keeps the number honest.
    ids = ["g1", "g2", "u1"]
    io.open(str(_files / "results.json"), "w", encoding="utf-8").write(
        json.dumps({"g1": True, "g2": False}))
    _preds(_files, [{"instance_id": i, "patch": "diff"} for i in ids])
    gate = C.graded_ids() | C.refused_ids() | C.exhausted_ids()
    assert [i for i in ids if i not in gate] == ["u1"]


# ------------------------------------------- a refusing gate must end the run, not be ignored


def test_a_gate_refusal_ends_the_cycle():
    """WHAT IT COST WHEN IT DID NOT. The companion Edge lost its context mid-run, and every
    later fleet_runner printed "[gate] REFUSING TO START ... no browser window headed" and
    exited. The driver discarded fleet_runner's return value, so it ran THIRTEEN more batches --
    about nineteen instances -- each finishing in under a minute against a normal twenty-five to
    thirty, each capturing an empty patch that was then graded "not (empty patch)".

    The gate refused to measure a broken stack. The driver produced the measurements anyway,
    one layer up, as zeroes.
    """
    import inspect
    src = inspect.getsource(C.cycle)
    assert "REFUSING TO START" in src, "the driver does not look for a gate refusal at all"
    assert "fleet_ok" in src, "fleet_runner's result is still discarded"


def test_the_refusal_is_not_retried_or_repaired():
    # A driver that restarts the thing it is measuring is how a run comes to measure something
    # other than what it reports. The condition the gate names does not fix itself between
    # batches, so there is nothing to wait for.
    import inspect
    src = inspect.getsource(C.cycle)
    i = src.index("REFUSING TO START")
    block = src[i:i + 1400]
    for forbidden in ("start_companion_edge", "edge_recover", "surface(", "restart"):
        assert forbidden not in block, (
            "the gate branch tries to repair the browser (%s); measuring must not restart what "
            "it measures" % forbidden)


def test_the_unattempted_instances_stay_retryable():
    # The point of stopping early is that the remainder is untouched: their attempt counts are
    # not incremented, so the next run picks them up rather than treating them as spent.
    import inspect
    src = inspect.getsource(C.cycle)
    i = src.index("REFUSING TO START")
    block = src[i:i + 1400]
    assert "attempt counts are unchanged" in block or "not attempted" in block, (
        "the operator is not told whether the remaining instances are lost or retryable")


def test_a_gate_refused_batch_does_not_spend_an_attempt(_files):
    """MEASURED CONSEQUENCE. After one run 40 instances carried an attempt, 14 of them at the
    cap of 2, and 23 had spent one while producing no patch -- because the count is taken
    before the batch and the gate then refused to start it. Fourteen instances were one line
    from being skipped for ever as "spent" without ever having run.
    """
    group = ["a", "b"]
    C.note_attempts(group)
    assert C.attempt_counts() == {"a": 1, "b": 1}
    C.refund_attempts(group)
    assert C.attempt_counts() == {}, "the refund did not give the attempts back"


def test_a_refund_never_goes_negative(_files):
    C.refund_attempts(["never_seen"])
    assert C.attempt_counts().get("never_seen", 0) == 0


def test_a_refund_only_gives_back_one(_files):
    # Two real attempts followed by one refused batch must leave one real attempt standing.
    C.note_attempts(["x"]); C.note_attempts(["x"]); C.note_attempts(["x"])
    C.refund_attempts(["x"])
    assert C.attempt_counts()["x"] == 2


def test_the_refund_is_wired_to_the_gate_branch():
    import inspect
    src = inspect.getsource(C.cycle)
    i = src.index("REFUSING TO START")
    assert "refund_attempts" in src[i:i + 900], (
        "the gate branch stops the cycle but still charges the batch for a run that never began")


def test_the_gate_check_sees_the_refusal_even_behind_noise():
    """IT DID NOT, AND THAT IS WHY THE ABORT NEVER FIRED.

    run() returned only the last SIX lines of output. The gate prints its refusal and the
    process then emits several library deprecation warnings, which pushed "[gate] REFUSING TO
    START" out of that window -- so `"REFUSING TO START" in fleet_tail` was False, the cycle
    carried on, and it captured another empty patch. The guard was present, correct, and looking
    at a window the evidence had already scrolled out of.
    """
    import inspect
    src = inspect.getsource(C.run)
    assert "tail[-6:]" not in src.split("return ")[-1], (
        "run() still matches on six lines; the refusal can scroll out of it again")
    # A realistic tail: the refusal followed by the warnings that displaced it.
    noisy = "\n".join(
        ["[gate] REFUSING TO START -- the stack is not in a state where results would mean anything",
         "       no browser window headed: copilot-companion-edge",
         "       Fix it, or pass --force to run anyway."]
        + ["AuthlibDeprecationWarning: authlib.jose is deprecated"] * 10)
    assert "REFUSING TO START" in "\n".join(noisy.splitlines()[-60:])
    assert "REFUSING TO START" not in "\n".join(noisy.splitlines()[-6:]), (
        "the six-line window would have caught it, so this test proves nothing")


def test_the_gate_check_does_not_depend_on_the_exit_code():
    """MEASURED: a gate refusal returns 0.

    The guard was written as `if not fleet_ok and "REFUSING TO START" in tail`. Since the
    refusal exits 0, `not fleet_ok` was always False and the branch was never reached -- the
    guard was added, tested, committed, and did nothing on three separate runs while the cycle
    carried on capturing empty patches. A refusal is identified by what it says.
    """
    import inspect
    src = inspect.getsource(C.cycle)
    i = src.index("REFUSING TO START")
    line_start = src.rindex(chr(10), 0, i) + 1
    line = src[line_start:src.index(chr(10), i)]
    assert "fleet_ok" not in line, (
        "the gate check is gated on an exit code again; the refusal returns 0, so that "
        "condition is never true: %s" % line.strip())


# ------------------------------------------------- the disk cost model, measured not guessed


def test_the_language_costs_are_not_below_what_was_measured():
    """MEASURED 2026-09-02, free space at batch start against the low point during it:

        openlibrary (python) 4x  8.19 -> 0.62 GiB  ~1900 MB each   the table said 120
        vuls        (go)     2x  4.08 -> 1.36 GiB  ~1360 MB each   the table said 700
        element-web (js)     2x  8.20 -> 5.96 GiB  ~1150 MB each   the table said 560

    concurrency_for() divides the headroom by these, so an optimistic entry admits more work
    than fits. At 120 MB the python entry admits four of anything, and openlibrary alone took a
    machine from 8 GiB to 620 MB free.
    """
    measured = {"python": 1900, "go": 1360, "js": 1150, "ts": 1150}
    for lang, mb in measured.items():
        assert C.LANG_DISK_MB.get(lang, 0) >= mb * 0.95, (
            "%s is costed at %s MB but was measured at ~%d MB; the sizing arithmetic cannot "
            "hold a floor on an estimate below what the work actually uses"
            % (lang, C.LANG_DISK_MB.get(lang), mb))


def test_a_thin_disk_admits_one_instance_and_not_more():
    # The floor of the function is one -- a batch of zero makes no progress -- but at 4.5 GiB
    # nothing should be running two of anything.
    for lang in ("python", "go", "js"):
        assert C.concurrency_for([lang], 4.5) == 1, lang


def test_an_explicit_batch_size_bypasses_this_entirely():
    """WHY THAT MATTERS, since it is what actually went wrong.

    batches(ids, size) uses `size if size else concurrency_for(...)`, so passing --batch 2
    replaces the disk-aware width with a constant. At 4.08 GiB free the sizing would have
    chosen ONE go instance; --batch 2 ran two, and free space went to 1.36 GiB. The override is
    a legitimate escape hatch, but it is not a disk safeguard -- it removes the one there is.
    """
    import inspect
    src = inspect.getsource(C.batches)
    assert "if size else" in src.replace("  ", " "), (
        "batches() no longer takes an explicit size; update this test's premise")
