# -*- coding: utf-8 -*-
"""Append-only JSONL record of data openings for any analysis tool.

An *opening* is one event: an analysis tool opened a set of data items. The record
lets a later independence check see whether a confirmation reused items that an
earlier analysis had already opened. It stores facts; it relaxes no threshold and
makes no judgement of its own.

Each line of the file is one JSON object (a row). The file grows by appends only;
rows are never rewritten in place. A row carries:

    analysis_id   the analysis that opened the items
    items         the opened item ids (order-insensitive)
    items_digest  sha256 over the sorted, de-duplicated item ids
    fetched_at    when the items were fetched (caller-supplied, opaque string)
    columns       the columns that were read (order preserved as given)

The digest is what an independence check compares: two openings that touched the
same set of items produce the same digest regardless of the order they were listed
in. ``iter_openings`` streams the rows back, optionally narrowed to one analysis.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence


def items_digest(items: Iterable[str]) -> str:
    """sha256 of the sorted, de-duplicated item ids, as a hex string.

    Order does not matter and repeats do not matter: the digest is taken over the
    sorted set of distinct ids, so ['a1', 'item-001'] and ['item-001', 'a1', 'a1']
    hash to the same value. Each id is newline-joined after sorting, which keeps the
    boundary between ids unambiguous (an id cannot contain a newline in practice;
    were one to, the sort+join still maps distinct sets to distinct strings here
    because the ids are compared as whole tokens before joining).
    """
    ids = sorted({str(x) for x in items})
    joined = "\n".join(ids)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _normalise_items(items: Iterable[str]) -> List[str]:
    """The item ids as stored on the row: sorted and de-duplicated.

    Storing the normalised form means a row read back reproduces the same digest
    without the reader having to know the normalisation rule.
    """
    return sorted({str(x) for x in items})


def build_row(
    analysis_id: str,
    items: Iterable[str],
    fetched_at: str,
    columns: Sequence[str],
) -> Dict[str, Any]:
    """Build one opening row without touching any file.

    Separated from :func:`record_opening` so a caller can construct and inspect a
    row (and its digest) before deciding to persist it.
    """
    stored_items = _normalise_items(items)
    return {
        "analysis_id": str(analysis_id),
        "items": stored_items,
        "items_digest": items_digest(stored_items),
        "fetched_at": str(fetched_at),
        "columns": [str(c) for c in columns],
    }


def init_record(path: str) -> str:
    """Ensure an empty record file exists at *path*, creating parents as needed.

    Idempotent: an existing file is left untouched (its rows are preserved). Returns
    the path. The file is append-only JSONL; initialising it just guarantees the
    container is present so the first :func:`record_opening` does not have to.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(path):
        with io.open(path, "w", encoding="utf-8"):
            pass
    return path


def record_opening(
    path: str,
    analysis_id: str,
    items: Iterable[str],
    fetched_at: str,
    columns: Sequence[str],
) -> Dict[str, Any]:
    """Append one opening to the record at *path* and return the row written.

    Creates the file (and parents) if absent, so a caller may skip
    :func:`init_record`. One JSON object is written per line; the file is only ever
    appended to.
    """
    row = build_row(analysis_id, items, fetched_at, columns)
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    with io.open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return row


def read_records(path: str) -> List[Dict[str, Any]]:
    """Read every opening row from *path* in file order.

    A missing file reads as no rows. Blank lines are skipped so a trailing newline
    does not produce a phantom row.
    """
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with io.open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def iter_openings(
    path: str,
    analysis_id: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield opening rows from *path* in file order.

    When *analysis_id* is given, only rows recorded for that analysis are yielded;
    when it is ``None`` every row is yielded. Streams the file so a large record is
    not held in memory at once.
    """
    if not os.path.exists(path):
        return
    want = None if analysis_id is None else str(analysis_id)
    with io.open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if want is None or row.get("analysis_id") == want:
                yield row
