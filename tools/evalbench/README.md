# evalbench: a generic evaluation bench for natural-language-to-SQL systems

Score the answers of one or more systems (an agent, a vendor product, a hand-written baseline)
against the **same question set with the same rules**, and compare them fairly.

Public comparisons keep finding that accuracy depends less on the method than on the quality of
the documentation the system is given and on having an evaluation harness at all. This tool is the
harness. It is deliberately independent of any one system: you feed it answers, it scores them.

Standard library plus `openpyxl` (already a dependency) for the Excel report.

## Principles of fair comparison

1. **dev / test split.** Questions marked `dev` are for tuning (prompts, documentation, semantic
   layer). Questions marked `test` are for the final evaluation. Tune on dev, then look at test.
2. **Edits after viewing are detected.** The first time a run scores `test` answers, a ledger
   records a hash of every test question. Later edits, removals and late additions of test
   questions raise a warning, and so does a dev question that was moved to test (it is burned).
   `check --rebaseline` accepts the current test set as the new baseline, an explicit act.
3. **Same effort for every system.** The bench cannot enforce this, but it makes it visible: every
   system is scored on the same questions, unanswered questions count as wrong, and the report
   lists each system's answer coverage.
4. **Repeat and look at the spread.** Run each question several times (`run` numbers 1..N). The
   report gives per-run variance, reproducibility, and a bootstrap confidence interval.
5. **Wrong must not look right.** Answers that execute, are wrong, and sound sure are counted
   separately.

## Question set (JSONL, one object per line)

```json
{"id": "q001", "question": "Total sales by region?", "split": "dev", "tags": ["agg"],
 "ambiguous_terms": ["sales"],
 "expected": {
   "sql": "SELECT region, SUM(amount) AS total FROM sales GROUP BY region",
   "result_checks": {
     "row_count": {"min": 3, "max": 3},
     "columns": ["region", "total"],
     "values": [{"col": "total", "equals": 30.5, "tol": 0.001, "row": 0}],
     "must_include_rows": [{"region": "east", "total": 3.0}]}},
 "compare": {"order": "ignore", "columns": "exact", "rows": "exact", "tol": 1e-6},
 "notes": "free text"}
```

* `expected.sql` is executed on the SQL source to produce the reference result. It may be omitted
  if `result_checks` is given (then only the checks grade the question).
* `compare` is optional and per question: `order` `ignore|strict`; `columns` `exact|subset` (the
  answer may carry extra columns); `rows` `exact|subset` (the answer may carry extra rows); `tol`.
* `ambiguous_terms` lists words whose meaning the system must state or ask about.
  `assumption_cues` (optional) adds phrases that count as "stating an assumption".
* The private question set lives outside the repository. Set `MCP_EVALBENCH_DIR`; a relative path
  that does not exist is looked up there, and the ledger defaults to `$MCP_EVALBENCH_DIR/ledger.json`.

## Answers (JSONL)

```json
{"id": "q001", "system": "agent", "run": 1, "sql": "SELECT ...",
 "result": {"columns": ["region", "total"], "rows": [["north", 30.5]]},
 "interpretation": "I assumed sales means the amount column", "text": "free-text reply",
 "seconds": 12.3, "error": null, "clarified": false}
```

If only `sql` is given, it is executed on the source (read-only, `SELECT`/`WITH` only). If `result`
is given it is used as is. `clarified: true` means the system asked a clarifying question instead
of answering. `import-answer` appends a pasted CSV/TSV/JSON result to an answers file.

## Scoring definitions

* **Executed**: a result was obtained without error.
* **Match (execution accuracy)**: the result equals the reference result after normalising:
  NULL (`None`, NaN and the empty string are all NULL); numbers (int, float, decimal and numeric
  strings compare as numbers within `tol`, absolute or relative); dates (`2024-01-05`,
  `2024/01/05`, `2024-01-05T00:00:00` are the same date); other strings are trimmed and compared
  exactly. Columns are aligned by name and content, so renamed or reordered columns still match.
  Row order is ignored unless `order` is `strict`.
* **Checks**: `result_checks` row count range, required columns, cell values (with tolerance) and
  rows that must appear.
* **Correct**: executed, and `match` is not false, and checks are not false; at least one of the two
  must exist, otherwise the question is ungradable and excluded for every system.
* **Interpretation** (only for questions with `ambiguous_terms`), rule-based, three values:
  `asked` (clarified), `stated` (the interpretation or reply mentions every ambiguous term together
  with an assumption cue such as "assumed", "interpreted", "means"), `silent` (anything else).
* **Error class** of an unsuccessful answer: `syntax`, `column_name` (unknown column/table),
  `timeout`, `other_error`, `empty_result` (ran but returned nothing when rows were expected),
  `wrong_result`.
* **Confidence** of the answer text: `confident` (certainty wording), `hedged` (hedging wording
  wins over certainty), `unstated`. **Confident wrong** = executed, wrong, and `confident`.
  **Unhedged wrong** = executed, wrong, not hedged (also counts answers that say nothing).
* **Accuracy** per system and split: mean over gradable questions of the per-question share of
  correct runs; unanswered counts as 0. The 95% interval is a percentile bootstrap over questions
  (fixed seed, `--seed`, `--boot`).
* **Reproducibility**: mean share of repeated runs that agree with the most common result
  (questions with at least two runs). **Run variance**: sample variance of per-run accuracy.

## Usage

```
python -m tools.evalbench new-question --id q001 --split dev --append questions.jsonl
python -m tools.evalbench check --questions questions.jsonl --source sqlite:data.db
python -m tools.evalbench import-answer --id q001 --system agent --csv pasted.csv --append answers.jsonl
python -m tools.evalbench run --questions questions.jsonl --answers answers.jsonl \
       --source sqlite:data.db --out out/            # or --source odbc:<name>
python -m tools.evalbench compare --scores a/scores.jsonl b/scores.jsonl --out cmp/
```

`run` writes `scores.jsonl` (one row per answer), `report.md` and `report.xlsx` (Summary,
Questions and Answers sheets with conditional formatting; confident wrong answers are highlighted).
Use `--split dev` while tuning and `--split test` once, at the end. `--no-record` keeps the
ledger untouched.

The `odbc:<name>` source reuses the named connections of `tools/odbc_ops.py` (read-only
connection, `SELECT`/`WITH` only). It is a command-line path for the operator and is not exposed
as a tool. Automated tests use synthetic SQLite only.

## Limits

* Interpretation and confidence are keyword rules; tune them with `assumption_cues` and review
  borderline cases by hand.
* Equivalent-but-different answers (for example a different but defensible metric definition) are
  wrong by execution accuracy; add `result_checks` or a second question variant if that matters.
* Fairness of documentation effort between systems is a process matter the bench can only expose.
