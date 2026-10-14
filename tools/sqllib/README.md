# tools/sqllib - a library of confirmed SQL examples

Natural-language-to-SQL accuracy depends on good documentation and on a growing set of
**question -> SQL examples that are known to work**. This package stores such examples, finds
the closest one for a new question, and tells the caller exactly how much to trust it.

It is generic: it knows nothing about any particular database or organisation. Tables, names
and examples live only in the operator's own store.

## Trust tiers

| Tier | Meaning | How it is reached |
|---|---|---|
| `draft` | Recorded only. Unverified. | Every save. |
| `validated` | **Auto-checked**: the SQL ran, returned rows, and a re-run gave the same result, N times in a row (default N = 2). | Automatically, inside `sql_example_use`. |
| `certified` | A person said it is right. Optional. | `sql_example_feedback(ok, source=user)` or the CLI `certify`. |
| `deprecated` | Retired. Never returned unless asked for. | CLI `deprecate`. |

Search results always say which one applies: `certified`, `validated (auto-checked ... business
logic NOT confirmed)`, `draft (unverified)`, or `no match`.

> **Automatic validation confirms that the SQL executes and repeats. It does not guarantee the
> business logic is correct.** The errors that remain in practice are mostly queries that run
> fine but compute the wrong thing. Treat `validated` as "works", and `certified` as "a person
> looked at the meaning".

Nothing requires certification; the library works with automatic validation alone.

```
              N identical successful runs (auto)           any person's OK
   draft  ------------------------------------>  validated ------------------>  certified
     ^                                               |                              |
     |        SQL (or its parameter declaration) edited -> back to draft            |
     +-----------------------------------------------+------------------------------+
   any tier --ng feedback--> needs_review flag, falls back to draft
   any tier --CLI deprecate--> deprecated
```

Confirmation is bound to the **content hash** (SQL text + parameter declaration, whitespace
insensitive). Editing it returns a `validated`/`certified` entry to `draft` and keeps the
previous version in `entries_history`. If the stored SQL is changed behind the API's back, the
entry reads as `draft` because its confirmed hash no longer matches.

### What counts as a qualifying run

`sql_example_use(id, outcome, rowcount, ms, result_hash, args_hash)`. A run qualifies when
`outcome == "ok"`, `rowcount > 0` and a `result_hash` (any stable fingerprint of the result,
computed by the caller) is given. A draft becomes `validated` when its most recent N runs of the
current SQL with the same `args_hash` all qualify and carry the same `result_hash`. An error, an
empty result, a missing fingerprint, or a different result resets the streak.

## Configuration

`MCP_SQLLIB_DB` - path of the SQLite file. **There is no default.** If it is unset, every
operation fails with an explicit error, so SQL is never accumulated in a shared default location
by accident. Optional: `MCP_SQLLIB_VALIDATE_N` (default 2).

## MCP tools (all require unlock, because they return stored SQL)

- `sql_examples_search(question, limit=5, threshold=None)` - matches with tier labels.
  Result: `verdict`, `label`, `matches[]` (`id`, `score`, `status`, `label`, `needs_review`,
  `question`, `sql`, `params`, `tables`, `notes`, `use_count`), plus a `caution` for
  `validated`/`draft`.
- `sql_example_save(question, sql, params=None, tables=None, notes=None)` - always a `draft`;
  there is no status argument.
- `sql_example_use(id, outcome, rowcount, ms, result_hash=None, args_hash=None)` - records a run
  and auto-promotes when the rule above is met.
- `sql_example_feedback(id, verdict, note=None, source="user")` - `ok` from a user certifies;
  `ng` sets `needs_review` and drops the entry to `draft`; `ok` from `auto` changes nothing.

## SQL checks

Only a single `SELECT` / `WITH` statement is accepted: no `;`-chained statements, no write or
DDL keywords (including `SELECT ... INTO`), and no comments (they can hide statements).
Bind variables are written `:name`; undeclared/unused parameters and `?` / `@name` placeholders
raise warnings. Hard-coded date or number literals are accepted but flagged, and the entry gets
a "parameterization recommended" note.

## Matching

Standard library only. Questions are NFKC-normalised (full/half width, case), relative periods
("past 1 month", "last month", "yesterday"), dates and numbers are replaced by shared
placeholders, and filler phrases and one-character particles are dropped. The score is
0.6 x character-bigram Dice + 0.4 x content-word Dice; default threshold 0.5.

## Operator CLI (optional)

```
python -m tools.sqllib list [--status draft]
python -m tools.sqllib show <id>
python -m tools.sqllib certify <id> --by <name>
python -m tools.sqllib deprecate <id> [--note ...]
python -m tools.sqllib edit <id> [--sql ... | --sql-file f.sql] [--question ...] [--notes ...]
python -m tools.sqllib import <file.jsonl> [--trust-status --by <name>]
python -m tools.sqllib export <file.jsonl> [--status certified]
```

`import` makes every record a draft unless `--trust-status` is given.

## Safety notes

- Stored SQL is never executed by this package. It only stores, inspects and ranks text.
- Anyone with write access to the SQLite file can change anything in it; keep it in a location
  with the access you intend.
- Tools are gated behind unlock. Auto-validation and user feedback are self-reported by the
  caller; they are signals, not a security boundary.
