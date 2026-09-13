# -*- coding: utf-8 -*-
"""A field every row declares and nothing ever fills — the runtime twin of an unreached function.

`tools/unreached.py` answers "was this written and never called?" by reading source. This answers
the same question about a RECORD: a key that appears on every row and is empty on every row is a
column that exists so an analysis can join or filter on it, and cannot be joined or filtered on.
Nothing fails; the analysis is simply narrower than the person running it believes.

MEASURED 2026-09-13, the first time this was asked, over `.fleet/*.jsonl`:

    mechanisms.jsonl  5629 rows   artifact_hash, attempt, execution_error, goal_hash
    judge.jsonl       9361 rows   human_approved

`goal_hash` turned out to be two defects at once: `RelayWorker.__init__` and `_spawn_children`
used `_mt` with no import in scope, so both raised NameError into an `except Exception: pass` and
the two rungs that carry a goal identity were never written at all. See
relay/test_a_swallowed_record_is_no_record.py. `artifact_hash` is the join key to the grader and
is structurally open; see docs/unreached_burndown.md.

DERIVED, NOT DECLARED. What counts as "promised" is taken from the data -- a key present on
almost every row -- rather than from a schema somebody would have to remember to update. A schema
that is maintained by hand records what its author believed; the rows record what the writer did.
"""
from __future__ import annotations

import gzip
import io
import json
import os

#: A key must appear on at least this share of rows to count as one the writer always emits.
#: Below it the key is optional and its absence says nothing.
DECLARED_SHARE = 0.9

#: Enough rows for "never" to mean anything. A ledger with fewer is not evidence either way.
MIN_ROWS = 50

#: Read no more than this many rows. tool_events.jsonl is 67MB; a field that is empty in the
#: first 40,000 rows and filled later is a different finding from the one this looks for.
MAX_ROWS = 40000

#: Values that count as "not filled". `0` and `False` are NOT here: they are answers.
EMPTY = (None, "", [], {})


def rows(path: str, limit: int = MAX_ROWS):
    """Every JSON object in a .jsonl, compressed or not, up to `limit`. Never raises."""
    op = gzip.open if path.endswith(".gz") else io.open
    try:
        with op(path, "rt", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= limit:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    yield row
    except Exception:
        return


def always_empty(path: str) -> tuple[int, list[str]]:
    """(rows read, keys the writer always emits and never fills), sorted.

    Returns (n, []) for a ledger too small to judge, so a caller can tell "nothing found" from
    "nothing to look at" by the count.
    """
    seen: dict[str, int] = {}
    filled: dict[str, int] = {}
    n = 0
    for row in rows(path):
        n += 1
        for key, value in row.items():
            seen[key] = seen.get(key, 0) + 1
            if value not in EMPTY:
                filled[key] = filled.get(key, 0) + 1
    if n < MIN_ROWS:
        return n, []
    return n, sorted(k for k, c in seen.items()
                     if c >= n * DECLARED_SHARE and filled.get(k, 0) == 0)


def survey(directory: str) -> dict[str, tuple[int, list[str]]]:
    """{ledger name: (rows, dead fields)} for every .jsonl in a directory that has any."""
    out = {}
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
            continue
        n, dead = always_empty(os.path.join(directory, name))
        if dead:
            out[name] = (n, dead)
    return out


def main(argv=None) -> int:
    """`python -m tools.ledger_health [dir]` -- ask the question without running the suite.

    The test that ratchets on this runs where the ledgers are, which is the operator's machine
    and not CI, and it reports only the DIFFERENCE from a known list. This prints what is
    actually there, which is what somebody wants when they are looking at a number in a report
    and wondering whether the column behind it was ever filled.
    """
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("directory", nargs="?",
                    default=os.path.join(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__))), ".fleet"),
                    help="directory of .jsonl ledgers (default: the repository's .fleet)")
    args = ap.parse_args(argv)

    found = survey(args.directory)
    if not found:
        print("no always-empty declared fields in %s" % args.directory)
        return 0
    for name in sorted(found):
        n, dead = found[name]
        print("%-32s rows=%-7d always empty: %s" % (name, n, ", ".join(dead)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
