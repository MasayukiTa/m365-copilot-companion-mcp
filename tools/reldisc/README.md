# reldisc - generic relationship discovery for SQL sources

Given a database, find which columns of which tables are join keys, without being told.
It profiles columns from bounded samples, proposes candidate column pairs, and measures
how well one column's values are contained in the other's. Everything is read-only and
budgeted; no table is ever scanned in full.

This is slice **R1**: profile, candidates, containment verification. It is not yet exposed
as an MCP tool (planned for R3).

## Usage

```
python -m tools.reldisc.run --source sqlite:<path> --out <store.sqlite> [--max-pairs N --budget-s S]
python -m tools.reldisc.run --source odbc:<NAME> --out <store.sqlite>     # uses MCP_DB_<NAME>
```

Options: `--pair-budget-s` (time per pair, default 10), `--pair-rows` (rows transferred per
pair, default 50000), `--sample-rows` (rows sampled per table, default 20000), `--seed`,
`--alias WORD=SYN[,SYN]` (extra name synonyms, repeatable, e.g. `--alias lot=ロット`), `--top`.

`--out` is the discovery store, a plain SQLite file you choose the location of. The program
prints a table of the best joins: confidence, the referencing side A, the key side B,
containment A in B and B in A, overlap (Jaccard), and cardinality (`N:1`, `1:1`, `N:M`).

From Python: `tools.reldisc.run.discover(source, store, ...)`.

## How it works

1. **Profile** (`profile.py`). One bounded sample per table (shared by all its columns).
   Per column: type, null rate, distinct count (exact for tables read in full, a Chao1-style
   estimate otherwise), min/max, length range, up to K representative values, and the *shape
   signature* of values: letters become `A`, digits `N`, full-width/CJK characters `W`,
   other symbols are kept (`T5-03-06NA-01` -> `AN-NN-NNAA-NN`). Results go to the store,
   keyed by table, column, declared type and a hash of the sampled content.
2. **Candidates** (`candidates.py`). Columns are bucketed by (type family, collapsed shape);
   only columns in the same bucket are compared, and inside a bucket only pairs that involve
   a key-like column (high distinct ratio, few NULLs) or share a non-generic name token. Pair
   score combines name similarity (tokens split on case, separators, digits and script
   changes, NFKC-normalised, extra synonyms via `aliases`), shape similarity, length/range
   compatibility and key-likeness. This avoids the all-pairs O(n^2) comparison.
3. **Verify** (`verify.py`). For each candidate (best first, up to `--max-pairs`) a sample
   of distinct values of A is tested against B with batched `IN (...)` queries, and the
   reverse. Output: A-in-B, B-in-A, an overlap estimate, a cardinality estimate and a
   confidence in 0..1. Confidence corrects for chance overlap (dense integer id ranges
   always overlap; only a related column name can lift them), scales with the number of
   values tested, and discounts tiny domains (a three-valued flag is contained in anything).
4. **Store** (`store.py`). Profiles, candidates and verifications in SQLite. A re-run
   re-profiles only columns whose sample hash changed and re-verifies only pairs where either
   side changed; the rest is reused. The sample is still read to compute the hash.

## Budgets and defaults

| Setting | Default |
| --- | --- |
| rows sampled per table | 20000 (a table with fewer rows is read in full) |
| exact distinct counts | only for tables read in full |
| per pair: wall-clock | 10 s |
| per pair: rows transferred | 50000 |
| per pair: distinct values tested per side | 2000, in `IN` batches of 500 |
| whole run | 600 s; profiling may use at most half |

A pair that exceeds its time or row budget is stored as *unverified* with the reason
(`budget_time`, `budget_rows`, `empty_sample`, `error`); it is never silently dropped or
reported as a join. Tables not reached before the run budget ends are listed as skipped.
Sampling is deterministic for SQLite (seeded random rowids); results are reproducible
for the same data and seed.

## Safety

* Read-only: SQLite is opened with `mode=ro` and `query_only`; ODBC uses the existing
  read-only named connection. Only `SELECT` statements are generated.
* No full scans: row counts come from catalogue statistics (`sys.dm_db_partition_stats`
  on SQL Server, `max(rowid)` on SQLite); samples are `TOP n` over a `TABLESAMPLE` page
  sample (no `ORDER BY NEWID()`) or seeded random rowid lookups. The source counts every
  row it transfers, and the tests assert those counts stay far below the table size.
* Timeouts: each query gets the remaining pair budget as its timeout.
* The store contains profile statistics and sampled representative values. Choose its
  location accordingly.

## Known limits of R1

* **No value transformations.** A suffix on one side (`A-01` vs `A`), full-width versus
  half-width characters, upper versus lower case, leading zeros or different separators
  give a low raw containment, and shapes that differ keep such pairs from being candidates
  at all. R1 reports them as non-joins. R2 will try a small dictionary of transformations
  and keep the one with the best containment.
* **No time windows.** A key re-used across years looks like a clean join. R2 will cut
  by a date column to detect reuse.
* **No graph or path search.** R3 will store joins as a graph and choose paths by
  confidence and cost, with reach statistics.
* **No semantic labels** (names/descriptions for tables and columns).
* Under a case-insensitive collation the database may match values that differ in case;
  the count reported is then a lower bound of the matched sent values.
* Sampled distinct counts and ranges are estimates; very skewed columns can be
  mis-estimated.
* Only base tables are profiled (views are skipped). The ODBC path builds SQL Server
  syntax and has been unit-tested for SQL generation only.
