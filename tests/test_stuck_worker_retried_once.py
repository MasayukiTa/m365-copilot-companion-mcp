# -*- coding: utf-8 -*-
"""One STUCK worker must not become several concurrent copies (2026-10-01: one job ran 3x).

Three causes, one test file:
  (1) an ambiguous fresh submit ended STUCK without `retryable_override = False`, so the runner's
      and the cockpit's retry (both keyed on the outcome string) re-queued it although its own
      reason said "not retried";
  (2) the cockpit re-queued the same terminal worker on every tick until a per-goal-text budget
      ran out, and also re-queued a worker the runner had already re-queued;
  (3) nothing in the runner refused a retry whose goal was already queued or running;
  (4) a recycle that overflowed the context on its very first turn repeated up to 8 times.
"""
import io
import os
import re
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


def _cs():
    return _read("ui", "FleetCockpit.cs")


def _method(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


# ---- (1) ambiguous fresh submit is not retryable, and the row says so ---------------------

def test_ambiguous_fresh_submit_sets_the_retry_override():
    src = _read("relay", "relay_fleet.py")
    block = _method(src, "except FreshSubmitAmbiguous as e:", "except ConversationClosed as e:")
    assert "self.retryable_override = False" in block


def test_status_rows_export_retryable_and_retry_queued():
    for rel in (("relay", "relay_fleet.py"), ("relay", "fleet_runner.py")):
        src = _read(*rel)
        assert '"retryable"' in src and '"retry_queued"' in src, rel


def test_cockpit_predicate_honours_retryable_false():
    helper = _method(_cs(), "static bool IsRetryableWorker(", "static bool IsInfraStuck(")
    assert 'TryGetValue("retryable"' in helper
    assert "!(bool)rv" in helper
    # the outcome set itself is untouched (tests/test_retry_sets_agree.py keeps it equal)
    assert 'IsRetryableOutcome(S(w, "outcome"))' in helper


# ---- (2) the cockpit re-queues a worker at most once --------------------------------------

def test_auto_retry_scan_remembers_workers_and_skips_covered_ones():
    src = _cs()
    auto = _method(src, "void AutoRetryScan(", "Dictionary<string, object> ReadStatus(")
    assert "RetryAlreadyCovered(root, w)" in auto
    assert "_autoRetriedWorkers.Add(RetryWorkerKey(w))" in auto
    # the check comes before the re-queue
    assert auto.index("RetryAlreadyCovered(root, w)") < auto.index("RetryGoal(w)")

    covered = _method(src, "bool RetryAlreadyCovered(", "static bool IsInfraStuck(")
    assert "_autoRetriedWorkers.Contains(RetryWorkerKey(w))" in covered
    assert '"retry_queued"' in covered          # the runner already re-queued it
    assert "IsTerminalWorker(x)" in covered      # an identical goal is live now

    key = _method(src, "static string RetryWorkerKey(", "bool RetryAlreadyCovered(")
    assert '"jid"' in key and '"run_id"' in key


def test_retry_all_shown_shares_the_per_worker_memory():
    bulk = _method(_cs(), "int RetryAllShown(", "static readonly string[] _retryableOutcomes")
    assert "RetryAlreadyCovered(null, w)" in bulk
    assert "_autoRetriedWorkers.Add(RetryWorkerKey(w))" in bulk


def test_retry_entries_are_tagged_as_retries():
    entry = _method(_cs(), "Dictionary<string, object> RetryEntry(", "void RetryGoal(")
    assert 'item["retry"] = true' in entry
    src = _read("relay", "relay_fleet.py")
    assert '"retry": True' in src and "_w.retry_queued = True" in src


# ---- (3) the runner refuses a duplicate retry ---------------------------------------------

def _w(goal, status):
    return types.SimpleNamespace(goal=goal, status=status)


def test_goal_is_live_only_for_non_terminal_workers_with_the_same_text():
    from relay.relay_fleet import _goal_is_live, TERMINAL

    done = TERMINAL[0] if isinstance(TERMINAL, (tuple, list)) else sorted(TERMINAL)[0]
    ws = [_w("job A", done), _w("job B", "running")]
    assert not _goal_is_live(ws, "job A")          # only a terminal copy: a real retry may run
    assert _goal_is_live(ws, "job B")
    assert _goal_is_live(ws, "  job B  ")
    assert not _goal_is_live(ws, "")
    assert not _goal_is_live(ws, "job C")
    assert not _goal_is_live(ws, "job B", exclude=ws[1])


def test_add_goal_path_refuses_only_tagged_retries():
    src = _read("relay", "relay_fleet.py")
    i = src.index("item = add_box.pop(0)")
    block = src[i:src.index("nw = _worker_for(len(workers), item)", i)]
    assert 'item.get("retry") and _goal_is_live(workers, item.get("text"))' in block
    assert "continue" in block                      # refused, with a mechanism row
    assert "_mt.record" in block


# ---- (4) a recycle that cannot make progress stops ----------------------------------------

def _recycle(worker, turn, exhausted=True):
    from relay.relay_fleet import _recycle_is_futile
    worker.turn = turn
    return _recycle_is_futile(worker, exhausted)


def test_two_consecutive_immediate_overflows_stop_the_recycling():
    w = types.SimpleNamespace(turn=0)
    assert not _recycle(w, 5)       # first overflow: recycle once
    assert not _recycle(w, 6)       # fresh conversation overflowed on its first reply
    assert _recycle(w, 7)           # ... and again: futile


def test_a_recycle_that_got_further_resets_the_streak():
    w = types.SimpleNamespace(turn=0)
    assert not _recycle(w, 5)
    assert not _recycle(w, 6)
    assert not _recycle(w, 20)      # made real progress before overflowing
    assert not _recycle(w, 21)
    assert _recycle(w, 22)


def test_non_exhausted_replies_never_count():
    w = types.SimpleNamespace(turn=0)
    assert not _recycle(w, 5, exhausted=False)
    assert not _recycle(w, 6, exhausted=False)
    assert not _recycle(w, 7, exhausted=False)


def test_futile_recycle_ends_non_retryable_with_a_reason():
    src = _read("relay", "relay_fleet.py")
    block = _method(src, "if _recycle_is_futile(self, conversation_exhausted(resp)):",
                    "self._heap_recycle_turn = self.turn")
    assert 'self.status, self.outcome = "stuck", "STUCK"' in block
    assert "self.retryable_override = False" in block
    assert "cannot make progress" in block
    assert re.search(r"\breturn\b", block)
