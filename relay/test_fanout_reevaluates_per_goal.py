# -*- coding: utf-8 -*-
"""codex-plan item 6's separately-named concrete bug (2026-09-09): a goal added mid-run via
add_box/add_goal_to_live_fleet was built through the SAME `_worker_for` closure as the
launch-time goals, which closes over the run's single `fanout` boolean -- so every worker,
whenever it was created, just inherited whatever the launch-time flag happened to be, and was
never independently judged against its OWN goal text. `_wants_fanout` (task_router.py) only
ever decided the batch-level flag; nothing re-evaluated per goal downstream of it.

The fix moved the real per-goal judgment (relay.splittability.judge) into
RelayWorker.__init__ itself, which every worker -- launch-time or mid-run, constructed through
`_worker_for` either way -- passes through. These tests prove the fix at the level the bug
actually lived at: two RelayWorker instances built with the IDENTICAL `fanout=True` run-wide
flag must reach DIFFERENT `.fanout` outcomes when their own goal text differs in
splittability, and a mid-run-shaped construction (the same call `_worker_for` makes for an
add_box item) gets the same live judgment as a launch-time one.
"""
from relay import relay_fleet as rf


def _worker(text, fanout=True, depth=0):
    return rf.RelayWorker({"text": text, "depth": depth}, "w0", fanout=fanout)


def test_the_run_wide_flag_alone_no_longer_decides_every_workers_fanout():
    """The bug's own shape: same `fanout=True`, same depth, different goal text -- and,
    before this fix, an identical `.fanout` outcome regardless of which."""
    splittable = _worker("2026年1月〜4月のメールを一覧化してください。")
    not_splittable = _worker("1+1はいくつですか。数字だけ答えてください。")
    assert splittable.fanout is True
    assert not_splittable.fanout is False


def test_a_mid_run_shaped_construction_gets_the_same_live_judgment_as_launch_time():
    """relay_fleet.py's `_worker_for` is the ONE function that builds both the launch-time
    `workers` list and every mid-run `add_box` arrival (`nw = _worker_for(len(workers),
    item)`) -- both paths construct a RelayWorker the identical way, through the identical
    constructor, so proving the constructor judges independently proves both call sites do."""
    # A launch-time-shaped goal (index 0, part of the initial batch)
    launch_time = _worker("2026年1月〜4月のメールを一覧化してください。")
    # A goal shaped like what add_box hands to `_worker_for(len(workers), item)` mid-run --
    # same constructor, same call shape, arriving later than index 0. The goal text alone
    # decides splittability, not WHEN the worker was constructed.
    mid_run = _worker("2026年1月〜4月のメールを一覧化してください。")
    assert launch_time.fanout == mid_run.fanout == True  # noqa: E712 (clarity over brevity here)


def test_fanout_true_run_wide_is_necessary_but_not_sufficient():
    """The run-wide flag still gates: turning it off must still turn every worker off,
    regardless of how splittable the goal looks -- this fix adds a second, independent
    condition, it does not replace the first."""
    w = _worker("2026年1月〜4月のメールを一覧化してください。", fanout=False)
    assert w.fanout is False


def test_depth_still_forbids_a_child_from_splitting_even_with_a_splittable_goal():
    """Structural gate, unchanged: a child (depth>0) must not split again, however
    independent its own slice of text looks."""
    w = _worker("2026年1月〜4月のメールを一覧化してください。", fanout=True, depth=1)
    assert w.fanout is False


def test_a_judging_failure_falls_back_to_not_splitting_not_to_the_old_permissiveness():
    """Same "failure is not permission" rule tools/command_judge.py states for
    JudgeUnavailable: if the splittability judgment itself breaks, RelayWorker must not fall
    back to the OLD unconditional "fanout=True at depth 0" behaviour -- that would silently
    resurrect the very over-permissive proxy this fix replaces."""
    from relay import splittability as sp

    real_judge = sp.judge

    def _broken(_text):
        raise RuntimeError("simulated judging failure")

    sp.judge = _broken
    try:
        w = _worker("2026年1月〜4月のメールを一覧化してください。", fanout=True)
        assert w.fanout is False
    finally:
        sp.judge = real_judge
