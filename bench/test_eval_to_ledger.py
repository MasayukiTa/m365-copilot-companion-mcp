"""Two shapes that are not the same, and the message that hid it.

The grader writes eval_results.json as one object of instance_id -> bool. calibration reads a
LEDGER: one JSON object per line with instance_id and a verdict string. Handing the first to
the second printed "no grade history yet" -- which reads as "nothing has ever been graded" and
was actually "this file is not that format".
"""
import io
import json

from bench.eval_to_ledger import convert, to_ledger_rows


def test_a_true_becomes_RESOLVED_and_anything_else_does_not():
    rows = to_ledger_rows({"a__a-1": True, "b__b-2": False})
    got = {r["instance_id"]: r["verdict"] for r in rows}
    assert got == {"a__a-1": "RESOLVED", "b__b-2": "UNRESOLVED"}


def test_evalerr_is_never_invented():
    """A bool cannot say 'the eval host broke'.

    calibration excludes EVALERR from the denominator because an infrastructure fault is not
    a competence signal. Emitting one from a False would move a real failure out of the
    denominator and quietly raise the score."""
    rows = to_ledger_rows({"a__a-1": False})
    assert rows[0]["verdict"] == "UNRESOLVED"
    assert all(r["verdict"] != "EVALERR" for r in rows)


def test_the_output_is_one_object_per_line(tmp_path):
    """Not a JSON array. The reader iterates lines and skips anything unparseable, so an
    array would be silently read as zero records -- the same empty-looking answer again."""
    ev = tmp_path / "eval.json"
    io.open(ev, "w", encoding="utf-8").write(json.dumps({"a__a-1": True, "b__b-2": False}))
    out = tmp_path / "ledger.jsonl"
    n = convert(str(ev), str(out))
    assert n == 2
    lines = [l for l in io.open(out, encoding="utf-8") if l.strip()]
    assert len(lines) == 2
    for l in lines:
        assert isinstance(json.loads(l), dict)


def test_calibration_can_read_what_this_writes(tmp_path):
    """The end-to-end claim, rather than two files that each look right."""
    from relay.selfimprove.calibration import calibration_report
    ev = tmp_path / "eval.json"
    io.open(ev, "w", encoding="utf-8").write(json.dumps(
        {"django__django-1": True, "django__django-2": False, "psf__requests-1": True}))
    out = tmp_path / "ledger.jsonl"
    convert(str(ev), str(out))
    rep = calibration_report(str(out))
    assert rep["n_records_read"] == 3
    assert rep["overall"]["n"] == 3 and rep["overall"]["resolved"] == 2
    assert rep["by_class"]["django"]["n"] == 2
    assert rep["by_class"]["psf"]["resolved"] == 1
