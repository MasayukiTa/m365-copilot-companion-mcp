"""Stage 0's replay: does it measure what the plan says, and does it refuse when it cannot.

The refusals matter as much as the measurement. A trace recorded in the ordinary debugging
mode can be summarised into a truncation number, and that number would describe a population
of slow turns rather than the early accepts the endpoint is about -- a confident answer to a
different question, which is the failure this package exists to avoid.
"""
from __future__ import annotations

import io
import json
import os
import tempfile

import pytest

from relay import settle_replay as SR


def _write(rows):
    path = os.path.join(tempfile.mkdtemp(prefix="trace_"), "settle_trace.jsonl")
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _poll(turn, text, generating=False):
    return {"turn_id": turn, "text": text, "generating": generating,
            "text_len": len(text), "phase": "stable"}


# ---- the refusals -----------------------------------------------------------------------

def test_a_debug_mode_trace_is_refused_rather_than_summarised():
    """turn_id も全文も無いトレースからでも数字は出せる。その数字は
    別の母集団についての答えなので、出さないほうが正しい。"""
    path = _write([{"ts": 1, "age_s": 61.0, "phase": "stable", "text_len": 58,
                    "text_tail": "...", "generating": False, "stable_count": 3}])
    with pytest.raises(SR.NotReplayable) as exc:
        SR.load_turns(path)
    assert "collect mode" in str(exc.value)
    assert "60-second gate" in str(exc.value)


def test_an_empty_trace_is_refused_and_the_refusal_says_why_that_is_interesting():
    path = _write([])
    with pytest.raises(SR.NotReplayable) as exc:
        SR.load_turns(path)
    assert "empty trace is a real finding" in str(exc.value)


def test_a_missing_trace_is_refused():
    with pytest.raises(SR.NotReplayable):
        SR.load_turns(os.path.join(tempfile.mkdtemp(), "absent.jsonl"))


# ---- the two implementations ---------------------------------------------------------------

def test_the_legacy_predicate_accepts_on_two_identical_reads():
    polls = [_poll("t1", "partial"), _poll("t1", "partial"), _poll("t1", "partial and more")]
    assert SR.accept_index_legacy(polls) == 1


def test_the_sampled_predicate_waits_for_a_sample_floor():
    """同じ内容の2回読みは、まだ終わっていないストリームの一時停止の中に
    両方とも収まりうる。それが truncated capture の作られ方。"""
    polls = [_poll("t1", "partial"), _poll("t1", "partial"), _poll("t1", "partial and more"),
             _poll("t1", "partial and more"), _poll("t1", "partial and more")]
    assert SR.accept_index_legacy(polls) == 1        # accepts the pause
    assert SR.accept_index_sampled(polls) == 3       # waits, and gets the whole thing


def test_generation_resets_stability_in_both():
    polls = [_poll("t1", "a"), _poll("t1", "a", generating=True), _poll("t1", "a")]
    assert SR.accept_index_legacy(polls) == -1
    assert SR.accept_index_sampled(polls) == -1


def test_a_turn_that_never_stabilises_is_counted_as_never_rather_than_truncated():
    polls = [_poll("t1", "a"), _poll("t1", "b"), _poll("t1", "c")]
    assert SR.accept_index_legacy(polls) == -1


# ---- the comparison --------------------------------------------------------------------------

def test_truncation_is_measured_against_the_turns_final_text():
    """計画が定める正解ラベルそのもの。受理点が末尾と違えば truncated。"""
    turns = SR.load_turns(_write([
        _poll("t1", "half"), _poll("t1", "half"),
        _poll("t1", "half and the rest"), _poll("t1", "half and the rest"),
        _poll("t1", "half and the rest"),
    ]))
    out = SR.replay(turns)
    assert out["per_implementation"]["legacy"]["truncated"] == 1
    assert out["per_implementation"]["sampled"]["truncated"] == 0


def test_the_discordant_pairs_are_reported_because_the_test_consumes_them():
    """2つの率だけ出すと対応が消え、検出力の大半が一緒に消える。"""
    rows = []
    for i in range(3):
        t = "t%d" % i
        rows += [_poll(t, "half"), _poll(t, "half"),
                 _poll(t, "half and rest"), _poll(t, "half and rest"),
                 _poll(t, "half and rest")]
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["discordant"]["legacy_worse"] == 3
    assert out["discordant"]["sampled_worse"] == 0
    assert out["discordant_total"] == 3


def test_an_underpowered_run_says_so_where_the_number_is_produced():
    """計画は不一致対およそ40を要求している。それ未満の差は証拠ではない。"""
    rows = [_poll("t1", "a"), _poll("t1", "a"), _poll("t1", "ab"), _poll("t1", "ab"),
            _poll("t1", "ab")]
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["sufficiently_powered"] is False
    assert "UNDERPOWERED" in SR.report(out)


def test_both_arms_are_scored_by_the_same_imperfect_label():
    """末尾を正解とするのは下限の推定。両腕を同じ物差しで測る限り対応比較は成立する。"""
    import inspect
    src = inspect.getsource(SR.replay)
    assert 'final = (polls[-1].get("text")' in src
    assert src.count("truncated = accepted != final") == 1
