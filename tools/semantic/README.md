# Semantic layer

A generic mechanism that lets an agent look words up before it writes SQL:

* a **glossary** (terms, synonyms, what each resolves to),
* **metric definitions** (one governed SQL per metric, with named parameters),
* a **catalogue** of tables / views (columns, value meanings, joins, pitfalls, performance notes).

The mechanism is in this repository. The **content is not**: it lives in a folder outside the
repository, named by an environment variable.

## Configuration

| Variable | Meaning |
|---|---|
| `MCP_SEMANTIC_DIR` | Folder holding the JSON files below. If it is unset, every tool answers "not configured" (it never pretends the dictionary is empty). |

Files are re-read automatically when their modification time or size changes; no restart is
needed after editing.

## File formats (JSON, UTF-8, no BOM)

Only `term` / `name` are required. Every other field may be omitted. Unknown keys are kept and
ignored, so newer files work with older code.

`glossary.json`

```json
{"terms": [{
  "term": "sales", "synonyms": ["revenue"], "meaning": "Invoiced amount",
  "resolves_to": {"kind": "filter|column|table|metric|note", "table": "orders",
                  "column": "amount", "expr": "status = 'X'"},
  "status": "draft|certified", "caveats": ["excludes tax"]}]}
```

`metrics.json`

```json
{"metrics": [{
  "name": "total_sales", "description": "Sales per region",
  "sql": "SELECT region, SUM(amount) FROM orders WHERE d >= :start AND d < :end GROUP BY region",
  "params": [{"name": "start", "type": "date", "default": null},
             {"name": "end",   "type": "date", "default": null}],
  "dimensions": ["region"], "tables": ["orders"], "synonyms": ["sales total"],
  "status": "certified", "caveats": []}]}
```

Parameter types: `date`, `int`, `float`, `string`, `bool`. A `null` default means required.

`catalog/<any name>.json` (one table or view per file)

```json
{"name": "orders", "kind": "table|view", "description": "...", "row_estimate": 0,
 "columns": [{"name": "status", "type": "char", "meaning": "...",
              "values": {"OPEN": "not shipped"}, "notes": "..."}],
 "keys": ["order_id"],
 "joins": [{"to": "customers", "on": "orders.customer_id = customers.id",
            "note": "...", "confidence": 0.9}],
 "pitfalls": ["..."], "performance": "...", "status": "draft|certified"}
```

A join declared in one file is usable in both directions.

### Broken files

A file that is not valid JSON, not UTF-8, or has the wrong shape is skipped **on its own**; the
other files keep loading. Each problem is reported with the file, the location (line and column
for JSON errors, or an entry path such as `terms[3].synonyms`) and a message, in
`store_problems` of `semantic_resolve` results and in the store status.

## Tools

All five tools are gated by `require_unlocked()` (they return the operator's business
definitions) and are reachable through the `call_tool` gateway. They are **read-only** and
**never execute SQL**. Results are JSON text.

| Tool | Arguments | Result |
|---|---|---|
| `semantic_resolve` | `text`, `today=""` | `matches` (term or metric, score, `match`, `defined`, `resolves_to`, `caveats`, `status`), `undefined` (words with no entry), `periods` (relative dates resolved), `ambiguous`, `warnings` |
| `metric_sql` | `name`, `params={}`, `period=""`, `today=""` | `sql` with `?`, `bind` values in order, `resolved_dates`, `caveats`, `status`, `executed: false` |
| `catalog_search` | `query`, `limit=10` | ranked tables with `score`, `coverage` and the places that matched |
| `catalog_describe` | `table` | columns, value meanings, joins (both directions), pitfalls, performance |
| `catalog_joins` | `a`, `b`, `max_hops=3` | known direct joins, plus multi-hop paths found breadth first, with per-hop and combined confidence |

### Undefined words

`semantic_resolve` returns the words of the request that have **no dictionary entry** in
`undefined`. The caller must not invent a definition for them: ask the owner or add the term to
the glossary. A word that is only part of, or similar to, a defined word is returned as a
`near` suggestion (`defined: false`), never as a definition.

### Metric SQL and dates

* Named parameters (`:start`) are rewritten to `?`; values come back in `bind`. Values are
  never concatenated into the SQL text. Parameters inside string literals, quoted identifiers
  and comments are left alone.
* Only a single `SELECT` / `WITH` statement is produced. Anything else (other statements,
  several statements, `SELECT ... INTO`, `EXEC`, ...) is refused.
* `period` accepts English or Japanese phrases and fills the `start` / `end` pair. Ranges are
  **start-inclusive, end-exclusive**; write the SQL as `>= :start AND < :end`. The base date
  is `today` (default: the real date) and is echoed in `resolved_dates`.

| Phrase | Range for today = 2026-10-14 |
|---|---|
| past 1 month (`過去1か月`) | 2026-09-15 to 2026-10-15 (N units ending today, today included) |
| last 7 days (`直近7日`) | 2026-10-08 to 2026-10-15 |
| last month (`先月`) | 2026-09-01 to 2026-10-01 |
| this month (`今月`) | 2026-10-01 to 2026-11-01 (the whole month) |

Also understood: yesterday / today, this / last week (Monday start), this / last year, the
month before last, past half year. Explicit `params` always win over `period`.

## Matching

Text is NFKC-normalised and case-folded (full-width and half-width agree). English words are
compared after a light plural / `-ing` / `-ed` stem. Japanese is compared as substrings and by
character bigrams. There is no morphological analyser, and none is needed as a dependency.

## Extending

* New term, metric or table: add it to the JSON files; no code change.
* New relative-date phrase: add a pattern to `dates.py` (`_PATTERNS`) and a handler branch.
* New field: add it to the JSON and read it where needed; unknown keys are already preserved.
  Add it to `store.py` only if it needs validation.
* New tool: add a gated function to `ops.py` (first statement `locked = require_unlocked()`),
  import it in `main.py`, and add its name to the data group in `relay/fleet_toolset.py`.

## Safety

* Read-only. No file is written and no SQL is executed by this package.
* Every tool is behind the unlock gate.
* The content stays outside the repository; this repository holds only the mechanism and
  synthetic test data.
