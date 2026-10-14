"""Command line: run | compare | new-question | check | import-answer."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import fairness
from .aggregate import summarize
from .answers import Answer, answer_from_paste, append_answer, load_answers
from .normalize import Result
from .questions import ENV_DIR, Question, load_questions, resolve_path, template, validate_question
from .report import render_markdown, write_excel
from .score import ScoreRow, score_answer
from .sources import SourceError, SqlSource, open_source


def default_ledger_path(questions_path: Path) -> Path:
    env = os.environ.get(ENV_DIR)
    if env:
        return Path(env) / "ledger.json"
    return questions_path.with_suffix(".ledger.json")


def compute_baselines(qs: list[Question], source: Optional[SqlSource], timeout: float
                      ) -> tuple[dict[str, Result], list[str]]:
    base: dict[str, Result] = {}
    warns: list[str] = []
    for q in qs:
        if not q.expected_sql:
            continue
        if source is None:
            warns.append("question %s has expected.sql but no --source was given; "
                         "only its result_checks (if any) are graded" % q.id)
            continue
        try:
            base[q.id] = source.execute(q.expected_sql, timeout=timeout)
        except SourceError as e:
            warns.append("question %s: expected SQL failed (%s: %s); only its result_checks "
                         "(if any) are graded" % (q.id, e.kind, e.message))
    return base, warns


def score_all(qs: list[Question], answers: list[Answer], source: Optional[SqlSource],
              timeout: float) -> tuple[list[ScoreRow], list[str], dict[str, str]]:
    baselines, warns = compute_baselines(qs, source, timeout)
    by_id = {q.id: q for q in qs}
    rows: dict[tuple, ScoreRow] = {}
    for a in answers:
        q = by_id.get(a.id)
        if q is None:
            warns.append("answer for unknown question id %r ignored" % a.id)
            continue
        key = (a.id, a.system, a.run)
        if key in rows:
            warns.append("duplicate answer for %s / %s / run %d: the later one is used" % key)
        rows[key] = score_answer(q, baselines.get(q.id), a, source, timeout)
    gradable = {q.id: q.split for q in qs if q.id in baselines or q.result_checks}
    for q in qs:
        if q.id not in gradable:
            warns.append("question %s cannot be graded (no usable reference) and is excluded" % q.id)
    ordered = sorted(rows.values(), key=lambda r: (r.system, r.id, r.run))
    return ordered, warns, gradable


def write_outputs(out: Path, rows: list[ScoreRow], agg: dict, warnings: list[str], title: str,
                  notes: list[str]) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "scores.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
    (out / "report.md").write_text(render_markdown(agg, warnings, title, notes), encoding="utf-8")
    write_excel(out / "report.xlsx", agg, rows, warnings)
    return [out / "scores.jsonl", out / "report.md", out / "report.xlsx"]


def _load_qs(path: str) -> list[Question]:
    qs, problems = load_questions(path)
    for p in problems:
        print("question file: " + p, file=sys.stderr)
    return qs


def cmd_run(a) -> int:
    qpath = resolve_path(a.questions)
    qs = _load_qs(a.questions)
    answers, aprob = load_answers(a.answers)
    source = open_source(a.source)
    rows, warns, gradable = score_all(qs, answers, source, a.timeout)
    warns = [("answers file: " + p) for p in aprob] + warns
    if a.split != "all":
        rows = [r for r in rows if r.split == a.split]
        gradable = {k: v for k, v in gradable.items() if v == a.split}
    ledger_path = Path(a.ledger) if a.ledger else default_ledger_path(qpath)
    ledger = fairness.load_ledger(ledger_path)
    warns = fairness.check_changes(qs, ledger) + warns
    agg = summarize(rows, gradable, n_boot=a.boot, seed=a.seed)
    files = write_outputs(Path(a.out), rows, agg, warns, a.title, [])
    test_scored = any(r.split == "test" for r in rows)
    if not a.no_record:
        fairness.save_ledger(ledger_path, fairness.record_run(qs, ledger, test_scored))
    for w in warns:
        print("WARNING: " + w, file=sys.stderr)
    for s in agg["summary"]:
        print("%-5s %-14s accuracy=%s answered=%d/%d confident_wrong=%d" % (
            s["split"], s["system"], "n/a" if s["accuracy"] is None else "%.3f" % s["accuracy"],
            s["answered"], s["questions"], s["confident_wrong"]))
    for f in files:
        print("wrote " + str(f))
    return 0


def cmd_compare(a) -> int:
    rows: dict[tuple, ScoreRow] = {}
    for p in a.scores:
        with open(resolve_path(p), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = ScoreRow.from_dict(json.loads(line))
                    rows[(r.system, r.id, r.run)] = r
    ordered = sorted(rows.values(), key=lambda r: (r.system, r.id, r.run))
    gradable = {r.id: r.split for r in ordered if r.graded}
    warns: list[str] = []
    if a.questions:
        known = {q.id for q in _load_qs(a.questions)}
        for qid in sorted(gradable):
            if qid not in known:
                warns.append("scored question %s is not in the question file" % qid)
    systems = {r.system for r in ordered}
    ids_by_sys = {s: {r.id for r in ordered if r.system == s} for s in systems}
    allids = set().union(*ids_by_sys.values()) if ids_by_sys else set()
    for s, ids in sorted(ids_by_sys.items()):
        if ids != allids:
            warns.append("system %s answered %d of %d questions; the missing ones count as wrong" % (
                s, len(ids), len(allids)))
    agg = summarize(ordered, gradable, n_boot=a.boot, seed=a.seed)
    files = write_outputs(Path(a.out), ordered, agg, warns, a.title, [])
    for w in warns:
        print("WARNING: " + w, file=sys.stderr)
    for f in files:
        print("wrote " + str(f))
    return 0


def cmd_new_question(a) -> int:
    line = json.dumps(template(a.id, a.split), ensure_ascii=False)
    if a.append:
        with open(a.append, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print("appended template %s to %s" % (a.id, a.append))
    else:
        print(line)
    return 0


def cmd_check(a) -> int:
    qpath = resolve_path(a.questions)
    qs, problems = load_questions(a.questions)
    errors = list(problems)
    warns: list[str] = []
    seen: dict[str, int] = {}
    for q in qs:
        seen[q.id] = seen.get(q.id, 0) + 1
        errors += ["%s: %s" % (q.id or "(no id)", e) for e in validate_question(q)]
    errors += ["duplicate id %s (%d times)" % (k, n) for k, n in seen.items() if n > 1]
    source = open_source(a.source)
    if source is not None:
        for q in qs:
            if not q.expected_sql:
                continue
            try:
                res = source.execute(q.expected_sql, timeout=a.timeout)
                if not res.rows:
                    warns.append("%s: expected SQL returns no rows" % q.id)
            except SourceError as e:
                errors.append("%s: expected SQL does not run (%s: %s)" % (q.id, e.kind, e.message))
    elif any(q.expected_sql for q in qs):
        warns.append("no --source given: expected SQL was not executed")
    if a.answers:
        answers, aprob = load_answers(a.answers)
        errors += ["answers file: " + p for p in aprob]
        ids = {q.id for q in qs}
        for x in answers:
            if x.id not in ids:
                errors.append("answer refers to unknown question id %r" % x.id)
    ledger_path = Path(a.ledger) if a.ledger else default_ledger_path(qpath)
    ledger = fairness.load_ledger(ledger_path)
    if a.rebaseline:
        fairness.save_ledger(ledger_path, fairness.rebaseline(qs, ledger))
        print("test baseline reset in %s" % ledger_path)
        ledger = fairness.load_ledger(ledger_path)
    warns += fairness.check_changes(qs, ledger)
    n_dev = sum(1 for q in qs if q.split == "dev")
    n_test = sum(1 for q in qs if q.split == "test")
    print("%d questions (%d dev, %d test)" % (len(qs), n_dev, n_test))
    for e in errors:
        print("ERROR: " + e)
    for w in warns:
        print("WARNING: " + w)
    if not errors and not warns:
        print("ok")
    return 1 if errors or (a.strict and warns) else 0


def cmd_import_answer(a) -> int:
    def _read(p: Optional[str]) -> Optional[str]:
        return Path(p).read_text(encoding="utf-8-sig") if p else None

    ans = answer_from_paste(
        a.id, a.system, run=a.run, csv_text=_read(a.csv), json_text=_read(a.json),
        sql=_read(a.sql), interpretation=a.interpretation or "", text=a.text or "",
        seconds=a.seconds, clarified=a.clarified, error=a.error)
    append_answer(a.append, ans)
    rows = len(ans.result.rows) if ans.result else 0
    print("appended answer %s / %s / run %d (%d rows) to %s" % (a.id, a.system, a.run, rows, a.append))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.evalbench",
                                description="Generic NL-to-SQL evaluation bench")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="score answers and write scores.jsonl, report.md, report.xlsx")
    r.add_argument("--questions", required=True)
    r.add_argument("--answers", required=True)
    r.add_argument("--source", help="sqlite:<path> or odbc:<name>")
    r.add_argument("--out", required=True)
    r.add_argument("--split", choices=["all", "dev", "test"], default="all")
    r.add_argument("--ledger")
    r.add_argument("--no-record", action="store_true", help="do not update the fairness ledger")
    r.add_argument("--timeout", type=float, default=30.0)
    r.add_argument("--boot", type=int, default=2000)
    r.add_argument("--seed", type=int, default=12345)
    r.add_argument("--title", default="Evaluation report")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="merge scores.jsonl files into one cross-system report")
    c.add_argument("--scores", nargs="+", required=True)
    c.add_argument("--questions")
    c.add_argument("--out", required=True)
    c.add_argument("--boot", type=int, default=2000)
    c.add_argument("--seed", type=int, default=12345)
    c.add_argument("--title", default="System comparison")
    c.set_defaults(func=cmd_compare)

    n = sub.add_parser("new-question", help="print (or append) a question template")
    n.add_argument("--id", default="q001")
    n.add_argument("--split", choices=["dev", "test"], default="dev")
    n.add_argument("--append", help="JSONL file to append the template to")
    n.set_defaults(func=cmd_new_question)

    k = sub.add_parser("check", help="validate a question set")
    k.add_argument("--questions", required=True)
    k.add_argument("--source")
    k.add_argument("--answers")
    k.add_argument("--ledger")
    k.add_argument("--timeout", type=float, default=30.0)
    k.add_argument("--strict", action="store_true", help="warnings also fail the check")
    k.add_argument("--rebaseline", action="store_true",
                   help="accept the current test questions as the new baseline")
    k.set_defaults(func=cmd_check)

    i = sub.add_parser("import-answer", help="append a pasted CSV/JSON result as an answer")
    i.add_argument("--id", required=True)
    i.add_argument("--system", required=True)
    i.add_argument("--run", type=int, default=1)
    i.add_argument("--csv")
    i.add_argument("--json")
    i.add_argument("--sql", help="file containing the SQL the system produced")
    i.add_argument("--interpretation")
    i.add_argument("--text")
    i.add_argument("--seconds", type=float)
    i.add_argument("--clarified", action="store_true")
    i.add_argument("--error")
    i.add_argument("--append", required=True, help="answers JSONL to append to")
    i.set_defaults(func=cmd_import_answer)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, SourceError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
