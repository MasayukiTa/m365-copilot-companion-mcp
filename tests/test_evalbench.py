"""Tests for the generic evaluation bench (tools/evalbench). Synthetic SQLite and questions only."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.evalbench import fairness  # noqa: E402
from tools.evalbench.aggregate import summarize  # noqa: E402
from tools.evalbench.answers import (  # noqa: E402
    Answer, answer_from_paste, load_answers, result_from_csv, result_from_json)
from tools.evalbench.cli import main as cli_main  # noqa: E402
from tools.evalbench.normalize import CompareConfig, Result, canonical_signature, compare_results  # noqa: E402
from tools.evalbench.questions import load_questions, parse_question, validate_question  # noqa: E402
from tools.evalbench.score import (  # noqa: E402
    E_COLUMN, E_EMPTY, E_SYNTAX, E_TIMEOUT, E_WRONG, I_ASKED, I_NA, I_SILENT, I_STATED,
    classify_error, classify_interpretation, evaluate_checks, score_answer)
from tools.evalbench.sources import SourceError, SqliteSource, open_source  # noqa: E402
from tools.evalbench.stats import bootstrap_ci, modal_share  # noqa: E402


# ------------------------------------------------------------------ fixtures

@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "synthetic.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sales (region TEXT, product TEXT, amount REAL, qty INTEGER, sold_on TEXT)")
    con.executemany("INSERT INTO sales VALUES (?,?,?,?,?)", [
        ("north", "widget", 10.5, 2, "2024-01-05"),
        ("north", "gadget", 20.0, 1, "2024-01-06"),
        ("south", "widget", 7.25, 3, "2024-02-01"),
        ("south", "gadget", None, 0, "2024-02-02"),
        ("east", "widget", 3.0, 1, "2024-03-09"),
    ])
    con.commit()
    con.close()
    return str(path)


def R(cols, rows):
    return Result(cols, rows)


def Q(**kw):
    d = {"id": "q1", "question": "Total by region?", "split": "dev",
         "expected": {"sql": "SELECT region, SUM(amount) AS total FROM sales GROUP BY region"}}
    d.update(kw)
    return parse_question(d)


# --------------------------------------------------------------- normalisation

BASE = R(["region", "total"], [["north", 30.5], ["south", 7.25], ["east", 3.0]])


def test_row_order_is_ignored_by_default_and_enforced_when_strict():
    shuffled = R(["region", "total"], [["east", 3.0], ["north", 30.5], ["south", 7.25]])
    assert compare_results(BASE, shuffled)[0]
    assert not compare_results(BASE, shuffled, CompareConfig(order="strict"))[0]
    assert compare_results(BASE, BASE, CompareConfig(order="strict"))[0]


def test_column_order_and_alias_do_not_matter():
    swapped = R(["sum_amount", "area"], [[30.5, "north"], [7.25, "south"], [3.0, "east"]])
    assert compare_results(BASE, swapped)[0]


def test_types_integer_float_and_numeric_string():
    a = R(["n"], [[3], [4]])
    assert compare_results(a, R(["n"], [[3.0], ["4"]]))[0]
    assert compare_results(a, R(["n"], [[3.0], [5.0]]))[0] is False


def test_date_strings_are_normalised():
    a = R(["d"], [["2024-01-05"], ["2024-01-06 00:00:00"]])
    b = R(["d"], [["2024/01/05"], ["2024-01-06T00:00:00"]])
    assert compare_results(a, b)[0]
    assert not compare_results(a, R(["d"], [["2024-01-05"], ["2024-01-07"]]))[0]


def test_float_tolerance():
    a = R(["x"], [[0.1 + 0.2]])
    assert compare_results(a, R(["x"], [[0.3]]))[0]
    assert not compare_results(a, R(["x"], [[0.31]]))[0]
    assert compare_results(a, R(["x"], [[0.31]]), CompareConfig(tol=0.05))[0]


def test_null_handling():
    a = R(["a", "b"], [[1, None], [2, "x"]])
    assert compare_results(a, R(["a", "b"], [[1, ""], [2, "x"]]))[0]
    assert not compare_results(a, R(["a", "b"], [[1, 0], [2, "x"]]))[0]
    assert not compare_results(a, R(["a", "b"], [[1, "NULL"], [2, "x"]]))[0]


def test_column_subset_and_row_subset_tolerances():
    extra_cols = R(["region", "total", "note"], [["north", 30.5, "a"], ["south", 7.25, "b"], ["east", 3.0, "c"]])
    assert not compare_results(BASE, extra_cols)[0]
    assert compare_results(BASE, extra_cols, CompareConfig(columns="subset"))[0]
    extra_rows = R(["region", "total"], BASE.rows + [["west", 1.0]])
    assert not compare_results(BASE, extra_rows)[0]
    assert compare_results(BASE, extra_rows, CompareConfig(rows="subset"))[0]
    missing = R(["region", "total"], BASE.rows[:2])
    assert not compare_results(BASE, missing, CompareConfig(rows="subset"))[0]


def test_wrong_value_and_wrong_row_count_are_reported():
    ok, why = compare_results(BASE, R(["region", "total"], [["north", 99.0], ["south", 7.25], ["east", 3.0]]))
    assert not ok and "differ" in why
    ok, why = compare_results(BASE, R(["region", "total"], BASE.rows[:1]))
    assert not ok and "row count" in why


def test_signature_ignores_order_and_names():
    a = canonical_signature(BASE)
    b = canonical_signature(R(["z", "y"], [[3, "east"], [30.5, "north"], [7.25, "south"]]))
    assert a == b
    assert a != canonical_signature(R(["region", "total"], [["north", 1.0]]))


# --------------------------------------------------------------------- source

def test_sqlite_source_is_read_only(db):
    src = SqliteSource(db)
    assert len(src.execute("SELECT * FROM sales").rows) == 5
    with pytest.raises(SourceError) as e:
        src.execute("DELETE FROM sales")
    assert e.value.kind == "rejected"
    with pytest.raises(SourceError):
        src.execute("SELECT nope FROM sales")
    assert len(src.execute("SELECT * FROM sales").rows) == 5
    assert isinstance(open_source("sqlite:" + db), SqliteSource)
    with pytest.raises(ValueError):
        open_source("mysql:x")


def test_sqlite_timeout(db):
    src = SqliteSource(db)
    slow = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 300000000) "
            "SELECT count(*) FROM c")
    with pytest.raises(SourceError) as e:
        src.execute(slow, timeout=0.2)
    assert e.value.kind == "timeout"


# ---------------------------------------------------------------------- scoring

def _baseline(db, q):
    return SqliteSource(db).execute(q.expected_sql)


def test_correct_answer_via_sql_executed_on_source(db):
    q = Q()
    src = SqliteSource(db)
    row = score_answer(q, _baseline(db, q), Answer("q1", "sysA", 1,
                       sql="SELECT region AS r, SUM(amount) FROM sales GROUP BY region ORDER BY 1 DESC"), src)
    assert row.executed and row.match and row.correct
    assert row.error_class == "none" and not row.confident_wrong


def test_error_classification(db):
    assert classify_error("near \"FORM\": syntax error") == E_SYNTAX
    assert classify_error("no such column: foo") == E_COLUMN
    assert classify_error("Invalid object name 'x'") == E_COLUMN
    assert classify_error("query timeout after 30 s") == E_TIMEOUT
    assert classify_error("disk on fire") == "other_error"
    q = Q()
    base = _baseline(db, q)
    src = SqliteSource(db)
    assert score_answer(q, base, Answer("q1", "s", 1, sql="SELEC 1"), src).error_class in (E_SYNTAX, "other_error")
    r = score_answer(q, base, Answer("q1", "s", 1, sql="SELECT foo FROM sales"), src)
    assert r.error_class == E_COLUMN and r.correct is False and not r.executed
    empty = score_answer(q, base, Answer("q1", "s", 1, result=R(["region", "total"], [])), src)
    assert empty.error_class == E_EMPTY and empty.executed
    wrong = score_answer(q, base, Answer("q1", "s", 1, result=R(["region", "total"], [["x", 1]])), src)
    assert wrong.error_class == E_WRONG
    nores = score_answer(q, base, Answer("q1", "s", 1), None)
    assert nores.correct is False and nores.error


def test_confident_wrong_is_separated_from_hedged_and_silent(db):
    q = Q()
    base = _baseline(db, q)
    bad = R(["region", "total"], [["north", 1.0]])
    conf = score_answer(q, base, Answer("q1", "s", 1, result=bad, text="The answer is definitely 1.0 for north."))
    hedged = score_answer(q, base, Answer("q1", "s", 1, result=bad, text="This is approximate, please verify."))
    quiet = score_answer(q, base, Answer("q1", "s", 1, result=bad))
    good = score_answer(q, base, Answer("q1", "s", 1, result=base, text="Definitely correct."))
    assert conf.confident_wrong and conf.unhedged_wrong
    assert not hedged.confident_wrong and not hedged.unhedged_wrong
    assert not quiet.confident_wrong and quiet.unhedged_wrong
    assert not good.confident_wrong and good.correct


def test_interpretation_three_way_classification():
    q = Q(ambiguous_terms=["revenue"])
    assert classify_interpretation(q, Answer("q1", clarified=True)) == I_ASKED
    assert classify_interpretation(q, Answer("q1", interpretation="I assumed revenue means net amount")) == I_STATED
    assert classify_interpretation(q, Answer("q1", interpretation="Here is the table")) == I_SILENT
    assert classify_interpretation(q, Answer("q1", interpretation="I assumed something")) == I_SILENT
    assert classify_interpretation(q, Answer("q1")) == I_SILENT
    assert classify_interpretation(Q(), Answer("q1")) == I_NA
    q2 = Q(ambiguous_terms=["revenue", "active"], assumption_cues=["working definition"])
    assert classify_interpretation(q2, Answer("q1", interpretation="working definition of revenue; active = recent")) == I_STATED
    assert classify_interpretation(q2, Answer("q1", interpretation="working definition of revenue")) == I_SILENT


def test_result_checks_alone_can_grade(db):
    q = Q(expected={"result_checks": {
        "row_count": {"min": 3, "max": 3}, "columns": ["region"],
        "values": [{"col": "total", "equals": 30.5, "row": 0}],
        "must_include_rows": [{"region": "east", "total": 3}]}})
    good = R(["Region", "total"], BASE.rows)
    ok, fails = evaluate_checks(q.result_checks, good)
    assert ok, fails
    bad_rows = R(["region", "total"], [["north", 30.5]])
    ok, fails = evaluate_checks(q.result_checks, bad_rows)
    assert not ok and any("row_count" in f for f in fails) and any("required row" in f for f in fails)
    row = score_answer(q, None, Answer("q1", "s", 1, result=good), None)
    assert row.graded and row.correct and row.match is None and row.checks_pass
    assert score_answer(q, None, Answer("q1", "s", 1, result=bad_rows), None).correct is False


def test_question_without_reference_is_ungraded():
    q = parse_question({"id": "x", "question": "?", "expected": {}})
    assert validate_question(q)
    row = score_answer(q, None, Answer("x", "s", 1, result=R(["a"], [[1]])), None)
    assert not row.graded and row.correct is None


# ------------------------------------------------------------- repeat variance

def test_reproducibility_and_confidence_interval_are_deterministic():
    assert modal_share(["a", "a", "b", "a"]) == 0.75
    assert modal_share(["a"]) is None
    vals = [1.0] * 14 + [0.0] * 6
    ci1 = bootstrap_ci(vals, n_boot=500, seed=7)
    ci2 = bootstrap_ci(vals, n_boot=500, seed=7)
    assert ci1 == ci2
    mean = sum(vals) / len(vals)
    assert ci1[0] <= mean <= ci1[1] and 0.0 <= ci1[0] and ci1[1] <= 1.0
    assert bootstrap_ci([1.0, 1.0, 1.0], n_boot=200, seed=1) == (1.0, 1.0)


def test_repeat_runs_in_summary(db):
    q = Q()
    base = _baseline(db, q)
    good = base
    bad = R(["region", "total"], [["north", 1.0]])
    answers = [Answer("q1", "s", 1, result=good), Answer("q1", "s", 2, result=good),
               Answer("q1", "s", 3, result=bad), Answer("q1", "s", 4, result=good)]
    rows = [score_answer(q, base, a, None) for a in answers]
    agg = summarize(rows, {"q1": "dev"}, n_boot=200, seed=3)
    s = agg["summary"][0]
    assert s["accuracy"] == 0.75 and s["reproducibility"] == 0.75 and s["runs"] == 4
    assert s["run_variance"] == pytest.approx(0.25)


# ------------------------------------------------------------------ fairness

def _tq(qid, text, split="test"):
    return parse_question({"id": qid, "question": text, "split": split,
                           "expected": {"sql": "SELECT 1"}})


def test_test_edit_after_viewing_is_flagged(tmp_path):
    led_path = tmp_path / "ledger.json"
    qs = [_tq("d1", "dev q", "dev"), _tq("t1", "test q one"), _tq("t2", "test q two")]
    ledger = fairness.load_ledger(led_path)
    assert fairness.check_changes(qs, ledger) == []
    fairness.save_ledger(led_path, fairness.record_run(qs, ledger, test_scored=True))
    ledger = fairness.load_ledger(led_path)
    assert fairness.check_changes(qs, ledger) == []
    edited = [_tq("d1", "dev q EDITED", "dev"), _tq("t1", "test q one EDITED"), _tq("t2", "test q two")]
    warns = fairness.check_changes(edited, ledger)
    assert len(warns) == 1 and "t1" in warns[0] and "edited" in warns[0]
    # dev edits are free
    assert fairness.check_changes([_tq("d1", "other", "dev")] + qs[1:], ledger) == []
    # late additions and removals
    warns = fairness.check_changes(qs + [_tq("t3", "late")], ledger)
    assert any("t3" in w and "added" in w for w in warns)
    warns = fairness.check_changes(qs[:2], ledger)
    assert any("t2" in w and "removed" in w for w in warns)


def test_dev_question_promoted_to_test_is_flagged_and_baseline_is_first_view(tmp_path):
    ledger = fairness.record_run([_tq("a", "alpha", "dev")], fairness.empty_ledger(), test_scored=False)
    warns = fairness.check_changes([_tq("a", "alpha", "test")], ledger)
    assert any("burned" in w for w in warns)
    # the baseline is the FIRST viewed hash: re-running after an edit does not erase the warning
    led = fairness.record_run([_tq("t", "v1")], fairness.empty_ledger(), True)
    led = fairness.record_run([_tq("t", "v2")], led, True)
    assert any("edited" in w for w in fairness.check_changes([_tq("t", "v2")], led))
    led = fairness.rebaseline([_tq("t", "v2")], led)
    assert fairness.check_changes([_tq("t", "v2")], led) == []


def test_notes_and_tags_do_not_change_the_hash():
    a = parse_question({"id": "t", "question": "q", "split": "test", "expected": {"sql": "SELECT 1"}})
    b = parse_question({"id": "t", "question": "q", "split": "test", "expected": {"sql": "SELECT 1"},
                        "notes": "n", "tags": ["x"]})
    assert a.content_hash() == b.content_hash()


# ------------------------------------------------------------ import helpers

def test_import_helpers():
    r = result_from_csv("region,total\nnorth,30.5\nsouth,7.25\n")
    assert r.columns == ["region", "total"] and len(r.rows) == 2
    t = result_from_csv("a\tb\n1\t2\n")
    assert t.columns == ["a", "b"]
    j = result_from_json('[{"a": 1, "b": 2}, {"a": 3, "b": 4}]')
    assert j.columns == ["a", "b"] and j.rows == [[1, 2], [3, 4]]
    j2 = result_from_json('{"columns": ["a"], "rows": [[1]]}')
    assert j2.rows == [[1]]
    j3 = result_from_json('{"data": [[1, 2]]}')
    assert j3.columns == ["col1", "col2"]
    ans = answer_from_paste("q1", "sysA", csv_text="a\n1\n", sql="SELECT 1")
    assert ans.result.rows == [["1"]]
    assert compare_results(R(["a"], [[1]]), ans.result)[0]


# ----------------------------------------------------------- reports and CLI

def _write_set(tmp_path, db):
    qs = [
        {"id": "d1", "question": "Total by region", "split": "dev", "tags": ["agg"],
         "expected": {"sql": "SELECT region, SUM(amount) AS total FROM sales GROUP BY region"}},
        {"id": "d2", "question": "How many widgets sold in revenue terms?", "split": "dev",
         "ambiguous_terms": ["revenue"],
         "expected": {"sql": "SELECT SUM(qty) AS n FROM sales WHERE product='widget'"}},
        {"id": "t1", "question": "Rows per product", "split": "test",
         "expected": {"sql": "SELECT product, COUNT(*) AS n FROM sales GROUP BY product"}},
        {"id": "t2", "question": "Largest single sale", "split": "test",
         "expected": {"result_checks": {"row_count": {"min": 1, "max": 1},
                                        "values": [{"col": "amount", "equals": 20.0}]}}},
    ]
    qpath = tmp_path / "questions.jsonl"
    qpath.write_text("\n".join(json.dumps(q) for q in qs) + "\n", encoding="utf-8")

    def A(i, system, run, **kw):
        d = {"id": i, "system": system, "run": run, "seconds": 2.0}
        d.update(kw)
        return d

    good_total = "SELECT region, SUM(amount) FROM sales GROUP BY region"
    bad_total = "SELECT region, AVG(amount) FROM sales GROUP BY region"
    answers = [
        A("d1", "agent", 1, sql=good_total, interpretation="Summed amount per region"),
        A("d1", "agent", 2, sql=good_total),
        A("d2", "agent", 1, sql="SELECT SUM(qty) FROM sales WHERE product='widget'",
          interpretation="I assumed revenue means quantity sold"),
        A("t1", "agent", 1, sql="SELECT product, COUNT(*) FROM sales GROUP BY product"),
        A("t2", "agent", 1, sql="SELECT MAX(amount) AS amount FROM sales"),
        A("d1", "other", 1, sql=bad_total, text="This is definitely the right answer."),
        A("d1", "other", 2, sql=bad_total, text="This is definitely the right answer."),
        A("d2", "other", 1, sql="SELECT SUM(qty) FROM sales", text="Done."),
        A("t1", "other", 1, sql="SELECT product, COUNT(*) FROM sales GROUP BY product"),
        A("t2", "other", 1, sql="SELECT MIN(amount) AS amount FROM sales"),
    ]
    apath = tmp_path / "answers.jsonl"
    apath.write_text("\n".join(json.dumps(a) for a in answers) + "\n", encoding="utf-8")
    return qpath, apath


def test_cli_run_end_to_end_and_comparison_report(tmp_path, db, capsys):
    qpath, apath = _write_set(tmp_path, db)
    out = tmp_path / "out"
    rc = cli_main(["run", "--questions", str(qpath), "--answers", str(apath), "--source", "sqlite:" + db,
                   "--out", str(out), "--boot", "200", "--ledger", str(tmp_path / "led.json")])
    assert rc == 0
    for name in ("scores.jsonl", "report.md", "report.xlsx"):
        assert (out / name).is_file()
    rows = [json.loads(line) for line in (out / "scores.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 10
    by = {(r["system"], r["id"], r["run"]): r for r in rows}
    assert by[("agent", "d1", 1)]["correct"] is True
    assert by[("other", "d1", 1)]["correct"] is False and by[("other", "d1", 1)]["confident_wrong"] is True
    assert by[("other", "d2", 1)]["correct"] is False  # all rows, not widget only
    assert by[("other", "t2", 1)]["correct"] is False and by[("agent", "t2", 1)]["correct"] is True
    assert by[("agent", "d2", 1)]["interpretation"] == "stated"
    assert by[("other", "d2", 1)]["interpretation"] == "silent"

    md = (out / "report.md").read_text(encoding="utf-8")
    assert "## Split: dev" in md and "## Split: test" in md and "agent" in md and "other" in md

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.load_workbook(out / "report.xlsx")
    ws = wb["Summary"]
    head = [c.value for c in ws[1]]
    table = {(r[head.index("split")], r[head.index("system")]): dict(zip(head, r))
             for r in ws.iter_rows(min_row=2, values_only=True)}
    assert table[("dev", "agent")]["accuracy"] == pytest.approx(1.0)
    assert table[("dev", "other")]["accuracy"] == pytest.approx(0.0)
    assert table[("dev", "other")]["confident_wrong"] == 2
    assert table[("test", "agent")]["accuracy"] == pytest.approx(1.0)
    assert table[("test", "other")]["accuracy"] == pytest.approx(0.5)
    assert table[("dev", "agent")]["reproducibility"] == pytest.approx(1.0)
    assert wb["Questions"].max_row == 5 and wb["Answers"].max_row == 11
    assert wb["Summary"].conditional_formatting  # conditional formats were written

    # the test split has now been viewed: editing a test question is flagged on the next run
    lines = qpath.read_text(encoding="utf-8").splitlines()
    edited = [json.loads(x) for x in lines]
    edited[2]["question"] = "Rows per product (reworded)"
    qpath.write_text("\n".join(json.dumps(x) for x in edited) + "\n", encoding="utf-8")
    capsys.readouterr()
    rc = cli_main(["check", "--questions", str(qpath), "--source", "sqlite:" + db,
                   "--ledger", str(tmp_path / "led.json")])
    printed = capsys.readouterr().out
    assert rc == 0 and "t1" in printed and "edited after the test split was viewed" in printed
    assert cli_main(["check", "--questions", str(qpath), "--source", "sqlite:" + db,
                     "--ledger", str(tmp_path / "led.json"), "--strict"]) == 1

    # compare merges two independent score files into one report
    agent_only = tmp_path / "agent.jsonl"
    other_only = tmp_path / "other.jsonl"
    agent_only.write_text("\n".join(json.dumps(r) for r in rows if r["system"] == "agent") + "\n", encoding="utf-8")
    other_only.write_text("\n".join(json.dumps(r) for r in rows if r["system"] == "other") + "\n", encoding="utf-8")
    cmp_out = tmp_path / "cmp"
    assert cli_main(["compare", "--scores", str(agent_only), str(other_only), "--out", str(cmp_out),
                     "--boot", "200"]) == 0
    wb2 = openpyxl.load_workbook(cmp_out / "report.xlsx")
    head2 = [c.value for c in wb2["Summary"][1]]
    vals = {(r[0], r[1]): r[head2.index("accuracy")] for r in wb2["Summary"].iter_rows(min_row=2, values_only=True)}
    assert vals == {k: pytest.approx(v["accuracy"]) for k, v in table.items()}
    q_head = [c.value for c in wb2["Questions"][1]]
    diff = {r[0]: r[2] for r in wb2["Questions"].iter_rows(min_row=2, values_only=True)}
    assert diff["d1"] == "yes" and diff["t1"] == "no"


def test_cli_check_finds_duplicates_and_bad_sql(tmp_path, db, capsys):
    lines = [
        {"id": "a", "question": "q", "expected": {"sql": "SELECT 1"}},
        {"id": "a", "question": "q again", "expected": {"sql": "SELECT 2"}},
        {"id": "b", "question": "broken", "expected": {"sql": "SELECT * FROM missing_table"}},
        {"id": "c", "question": "no reference", "expected": {}},
        {"id": "d", "question": "bad split", "split": "prod", "expected": {"sql": "SELECT 1"}},
    ]
    p = tmp_path / "q.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
    rc = cli_main(["check", "--questions", str(p), "--source", "sqlite:" + db, "--ledger", str(tmp_path / "l.json")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "duplicate id a" in out and "expected SQL does not run" in out
    assert "nothing to grade" in out and "split must be" in out and "invalid JSON" in out


def test_cli_new_question_and_import_answer(tmp_path, capsys):
    qf = tmp_path / "q.jsonl"
    assert cli_main(["new-question", "--id", "q9", "--split", "test", "--append", str(qf)]) == 0
    qs, problems = load_questions(qf)
    assert not problems and qs[0].id == "q9" and qs[0].split == "test"
    assert not validate_question(qs[0])
    csvf = tmp_path / "r.csv"
    csvf.write_text("a,b\n1,2\n", encoding="utf-8")
    af = tmp_path / "a.jsonl"
    assert cli_main(["import-answer", "--id", "q9", "--system", "x", "--csv", str(csvf),
                     "--append", str(af), "--seconds", "1.5", "--clarified"]) == 0
    answers, problems = load_answers(af)
    assert not problems and answers[0].clarified and answers[0].result.rows == [["1", "2"]]


def test_environment_directory_is_used_for_relative_paths(tmp_path, monkeypatch):
    (tmp_path / "set.jsonl").write_text(json.dumps({"id": "z", "question": "q", "expected": {"sql": "SELECT 1"}}) + "\n",
                                        encoding="utf-8")
    monkeypatch.setenv("MCP_EVALBENCH_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path.parent)
    qs, _ = load_questions("set.jsonl")
    assert qs[0].id == "z"


def test_module_entry_point_runs_and_imports_nothing_from_a_production_tree(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "tools.evalbench", "new-question", "--id", "m1"],
                          cwd=str(ROOT), capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["id"] == "m1"
    import tools.evalbench as pkg
    assert Path(pkg.__file__).resolve().parent.parent.parent == ROOT
