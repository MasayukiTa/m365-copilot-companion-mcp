# -*- coding: utf-8 -*-
"""One definition of "this row is not a measurement", and a guard against a sixth copy.

WHAT THIS IS ABOUT. The rule was written five times as a literal comparison against the string
EVALERR. Adding NOPATCH to the vocabulary broke four of the five, and in the worst available
way: pro_cycle.graded_ids() correctly left a NOPATCH instance outstanding so the cycle would try
it again, while pro_grade_remote.ingest() treated that same row as a verdict already held and
DISCARDED the real one when it arrived. The instance would have been re-run for ever and never
recorded -- and both halves would have looked reasonable in isolation.

That is the identical defect that had just been fixed for EVALERR, reintroduced within the hour
by adding one value to a rule that lived in five places.
"""
import ast
import io
import json
import os

from bench import verdicts as V

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# -- the vocabulary --------------------------------------------------------------------------

def test_a_verdict_about_a_patch_is_a_measurement():
    assert V.is_measurement("RESOLVED") and V.is_measurement("not")


def test_a_row_where_nothing_was_evaluated_is_not():
    assert not V.is_measurement("EVALERR")
    assert not V.is_measurement("NOPATCH")
    assert not V.is_measurement("")
    assert not V.is_measurement(None)


def test_a_boolean_false_is_a_grade_not_an_absence():
    """`str(v or "")` collapses False to "", which is in the set -- so an instance that was
    graded and did NOT resolve read as never graded and was re-run. That is a benchmark
    re-rolling its own failures, and it fails in the direction that looks like a better score."""
    assert V.is_measurement(False) and V.normalise(False) == "not"
    assert V.is_resolved(True)


# -- the defect codex found ------------------------------------------------------------------

def test_a_nopatch_row_does_not_swallow_the_real_verdict(tmp_path):
    """THE ONE THAT WAS LIVE.

    ingest() asked `verdict != "EVALERR"` to decide what it already knew, so a NOPATCH row --
    "no patch was produced, nothing was evaluated" -- counted as a verdict in hand. graded_ids()
    disagreed and left the instance outstanding, so it would be run again, graded again, and the
    result thrown away here every time.
    """
    from bench import pro_grade_remote as G
    led = tmp_path / "results.json"
    led.write_text(json.dumps({"instance_id": "a", "verdict": "NOPATCH"}) + "\n",
                   encoding="utf-8")
    assert G.ingest({"a": True}, str(led)) == 1, (
        "a NOPATCH row must not block the real verdict from being recorded")
    rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert rows[-1]["verdict"] == "RESOLVED"


def test_the_cycle_and_the_grader_agree_about_nopatch():
    """They disagreed, and the disagreement was silent in both directions."""
    from bench import pro_cycle
    from bench import pro_ledger_report
    assert "NOPATCH" in pro_cycle._NOT_A_GRADE
    assert "NOPATCH" in pro_ledger_report.NOT_A_MEASUREMENT
    assert pro_cycle._NOT_A_GRADE is V.NOT_A_MEASUREMENT
    assert pro_ledger_report.NOT_A_MEASUREMENT is V.NOT_A_MEASUREMENT


# -- no sixth copy ---------------------------------------------------------------------------

#: Files that may compare against the literal, with the reason each is allowed:
#:   verdicts.py         defines the rule
#:   swe_check_remote.py PRODUCES the verdict from a remote runner's output
#:   pro_ledger_report   buckets rows BY NAME for the report; an unknown verdict falls into
#:                       "other" and is printed, so it fails towards saying so
#: Tests are exempt as a class: asserting that a row says EVALERR is the point of some of them,
#: and a test cannot silently mis-skip an instance in a run.
_MAY_COMPARE = {"verdicts.py", "swe_check_remote.py", "pro_ledger_report.py"}


def _hardcoded_evalerr_comparisons(path):
    """Comparisons against the literal "EVALERR". Parsed, not grepped: every one of these files
    explains the incident in prose that necessarily contains the word, so a text search matches
    the explanation and passes while the comparison is still there."""
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except (SyntaxError, OSError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for side in [node.left] + list(node.comparators):
            if isinstance(side, ast.Constant) and side.value == "EVALERR":
                out.append(node.lineno)
    return out


def test_no_module_decides_this_with_a_literal_of_its_own():
    """The guard that makes the fix permanent rather than momentary.

    Adding NOPATCH to five hand-written lists would have reset the clock, not stopped it: the
    sixth copy is written the next time someone adds a verdict. A comparison against the literal
    is the shape of the defect, so the shape is what is banned.
    """
    offenders = []
    for folder in ("bench", "relay"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(REPO, folder)):
            dirnames[:] = [d for d in dirnames if d not in
                           {"__pycache__", ".git", "node_modules", "selfimprove_archive"}]
            for name in filenames:
                if (not name.endswith(".py") or name in _MAY_COMPARE
                        or name.startswith("test_")):
                    continue
                path = os.path.join(dirpath, name)
                for line in _hardcoded_evalerr_comparisons(path):
                    # THE REAL PATH. Joining folder+name hid which of two same-named modules
                    # was at fault -- "relay/targeting.py" does not exist; the offender was
                    # relay/selfimprove/targeting.py, and the edit meant for it had silently
                    # never been written.
                    offenders.append("%s:%d"
                                     % (os.path.relpath(path, REPO).replace(chr(92), "/"), line))
    assert not offenders, (
        "these compare against the EVALERR literal instead of asking "
        "bench.verdicts.is_measurement(); a new verdict will break them silently: %s"
        % ", ".join(sorted(offenders)))
