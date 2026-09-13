# -*- coding: utf-8 -*-
"""The runtime half of "nothing new is built without a caller".

`tools/test_nothing_new_is_built_without_a_caller.py` holds an inventory of functions the source
never calls. This holds the same inventory for RECORDS: a key that every row of a ledger carries
and no row ever fills. Both are instruments with no value, and the second kind is harder to see,
because the file is there, the rows are there, and the column is there — only the answer is not.

MEASURED 2026-09-13, the first time anything asked (`.fleet/*.jsonl`):

    mechanisms.jsonl  5629 rows   artifact_hash, attempt, execution_error, goal_hash
    judge.jsonl       9361 rows   human_approved

Two of those were live defects rather than unused columns. `goal_hash` was empty because
`RelayWorker.__init__` and `_spawn_children` referenced `_mt` with no import in scope and the
NameError went into an `except Exception: pass` — so the two telemetry rungs that identify a
GOAL were never written at all (relay/test_a_swallowed_record_is_no_record.py). `human_approved`
is empty because `tools/judge_backend.py::ask_human_async` has no caller, which the unreached
inventory has been carrying separately all along: the same hole, seen from the two ends.

SKIPPED WHERE THERE IS NO LEDGER. `.fleet` is gitignored, so CI and a fresh checkout have
nothing to read and a skip is honest — the question cannot be asked there. It is asked on the
machine that actually runs the fleet, which is the only machine where a dead column can mislead
anybody. Same reasoning as ui/test_deployed_cockpit_matches_its_source.py.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import ledger_health as LH  # noqa: E402

FLEET = os.path.join(REPO, ".fleet")


#: Fields measured empty on 2026-09-13 and left that way ON PURPOSE, each with the reason.
#: "<ledger>::<field>". THIS MAY SHRINK AND NEVER GROW -- a new dead column has to be argued
#: for here, which is the whole mechanism.
#:
#: An entry is NOT a verdict that the column is useless. It is a record that somebody looked,
#: and what they found.
KNOWN_EMPTY = {
    "mechanisms.jsonl::artifact_hash":
        "the join key to the grader. Structurally open, not forgotten: the row is written by "
        "the worker as it settles and the patch is captured afterwards, out of process, by "
        "bench/pro_capture.py -- at neither moment do both exist. Written up in "
        "docs/unreached_burndown.md under 'Wired, and still not answering the question it was "
        "built for'",
    # `mechanisms.jsonl::goal_hash` WAS HERE AND RETIRED ITSELF, which is the mechanism
    # working rather than a gap. It was empty on all 5629 historical rows because the two
    # sites carrying a goal identity raised NameError into a bare except and wrote nothing
    # (relay/test_a_swallowed_record_is_no_record.py). Once the fix landed, 67 rows arrived
    # with the field filled and test_a_column_that_started_being_written_leaves_the_list
    # failed on the entry until it came off. A fix that never reaches the record is a fix
    # nobody can see, and this is how the list is made to notice rather than be told.
    "mechanisms.jsonl::attempt":
        "no call site passes it. The attempt number is recoverable from the transcript key, so "
        "this is a column nobody has needed rather than an answer nobody can get",
    "mechanisms.jsonl::execution_error":
        "set only when a mechanism's own execution raises. Empty across 5629 rows is the "
        "outcome this field reports, not evidence that it cannot report it -- the staircase "
        "above it (`executed`) IS populated, which is what distinguishes the two",
    "judge.jsonl::human_approved":
        "tools/judge_backend.py::ask_human_async has no caller, so no judgement has ever been "
        "put to a person. Carried in the unreached inventory as the same hole seen from the "
        "writing end; closing it is a decision about turning on a gate, not a repair",
}


def _survey():
    if not os.path.isdir(FLEET):
        pytest.skip("no .fleet here: the question cannot be asked on this machine")
    found = LH.survey(FLEET)
    if not found and not any(n.endswith(".jsonl") for n in os.listdir(FLEET)):
        pytest.skip("no ledgers to read")
    return found


def _keys(found):
    return {"%s::%s" % (name, field) for name, (_n, fields) in found.items() for field in fields}


# ── the ratchet ───────────────────────────────────────────────────────────────────────────

def test_no_new_column_goes_unwritten():
    """A field every row declares and nothing fills is an answer the analysis will never get,
    and it reads exactly like a question nobody asked."""
    new = sorted(_keys(_survey()) - set(KNOWN_EMPTY))
    assert not new, (
        "these are carried on almost every row and filled on none -- find what should write "
        "them, or record why nothing does: %s" % ", ".join(new))


def test_a_column_that_started_being_written_leaves_the_list():
    """THE HALF THAT MAKES IT A RATCHET. Without it the list becomes a historical document the
    next reader takes for the current state -- the failure the unreached inventory names too."""
    stale = sorted(set(KNOWN_EMPTY) - _keys(_survey()))
    assert not stale, (
        "these now carry values (or the ledger is gone) -- take them off the list: %s"
        % ", ".join(stale))


def test_every_entry_says_why():
    """A list of names with no reasons is the form this turns into if nobody is asked for one."""
    thin = sorted(k for k, why in KNOWN_EMPTY.items() if len((why or "").strip()) < 40)
    assert not thin, "no reason recorded: %s" % ", ".join(thin)


# ── the scanner itself ────────────────────────────────────────────────────────────────────

def test_a_false_value_is_an_answer(tmp_path):
    """`False` and `0` are findings, not blanks. Counting them as empty would have reported
    every boolean rung of the telemetry staircase as a dead column."""
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(
        '{"a": false, "b": 0, "c": null, "d": ""}' for _ in range(LH.MIN_ROWS)),
        encoding="utf-8")
    n, dead = LH.always_empty(str(p))
    assert n == LH.MIN_ROWS
    assert dead == ["c", "d"], dead


def test_an_optional_key_is_not_a_declared_one(tmp_path):
    """A key on a handful of rows says nothing by being empty on them; only a key the writer
    emits every time is making a promise."""
    lines = ['{"always": null, "sometimes": null}'] * 5
    lines += ['{"always": null}'] * 95
    (tmp_path / "x.jsonl").write_text("\n".join(lines), encoding="utf-8")
    _n, dead = LH.always_empty(str(tmp_path / "x.jsonl"))
    assert dead == ["always"], dead


def test_too_few_rows_is_not_evidence(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(['{"a": null}'] * 5), encoding="utf-8")
    n, dead = LH.always_empty(str(p))
    assert (n, dead) == (5, [])


def test_a_compressed_ledger_is_still_a_ledger(tmp_path):
    """Retention gzips these as they age, and a survey that stops seeing a ledger the week it
    is compressed reports a clean bill of health it did not earn."""
    import gzip

    p = tmp_path / "x.jsonl.gz"
    with gzip.open(str(p), "wt", encoding="utf-8") as fh:
        fh.write("\n".join(['{"a": null, "b": 1}'] * LH.MIN_ROWS))
    _n, dead = LH.always_empty(str(p))
    assert dead == ["a"], dead


def test_a_broken_line_does_not_stop_the_survey(tmp_path):
    """These files are appended to by processes that can be killed mid-write."""
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(['{"a": null}'] * LH.MIN_ROWS + ["{not json"]), encoding="utf-8")
    n, dead = LH.always_empty(str(p))
    assert n == LH.MIN_ROWS and dead == ["a"]
