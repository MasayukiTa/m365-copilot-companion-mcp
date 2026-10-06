# -*- coding: utf-8 -*-
"""The reconciler could see 2 of 1557 transcripts and said nothing about the other 1555.

MEASURED 2026-09-13 against the live `.fleet/transcripts`:

    .jsonl        :    2
    .jsonl.gz     : 1555
    load_completions() -> 2 goals          (692 after the fix)

`load_completions` globbed `pattern + ".jsonl"` and opened with `io.open`. The retention job
gzips transcripts as they age, so a run's record fell out of this function's view a few days
after it finished -- silently. It does not raise; the dict just gets smaller, and a reconciler
that compares a worker's CLAIM against its TRANSCRIPT has no transcript to compare against.

`relay/fleet_retention.py::open_maybe_gz` was written for exactly this and had no caller:

    "Readers call this instead of open() so compression is invisible to them -- a reader that
    has to know is a reader that will one day be added without knowing."

It had since been written twice more, independently: `selfimprove/quality.py::_open_transcript`
(whose docstring records the same incident, "16 of 16 recorded transcripts had already become
.jsonl.gz") and inline in `scripts/win/capture_budget.py::read_log`. Three implementations of
one rule, and the reader that received none of them is the one that checks claims against
evidence.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fleet_reconcile as FR  # noqa: E402


def _rows(goal, final):
    return [{"meta": True, "goal": goal},
            {"turn": 1, "role": "user", "text": goal},
            {"turn": 1, "role": "assistant", "text": final}]


def _write(path, goal, final, compressed=False):
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in _rows(goal, final))
    if compressed:
        with gzip.open(str(path) + ".gz", "wt", encoding="utf-8") as fh:
            fh.write(body)
    else:
        with io.open(str(path), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)


# ── the defect ────────────────────────────────────────────────────────────────────────────

def test_a_gzipped_transcript_is_read(tmp_path):
    """THE DEFECT. Every transcript older than the retention window was invisible."""
    d = tmp_path / "transcripts"
    d.mkdir()
    _write(d / "rAAA_a0_w0.jsonl", "圧縮された用事", "やりました DONE", compressed=True)
    out = FR.load_completions(transcripts=str(d))
    assert out, "gz になった時点でこの読み手からは記録が消える"
    # KEYED BY A GOAL HASH, not by the goal text -- the text is in the tuples. The first
    # version of this searched the keys and passed vacuously on an empty generator.
    goals = [goal for rows in out.values() for _worker, goal, _final in rows]
    assert any("圧縮された用事" in g for g in goals), goals


def test_both_forms_are_read_together(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    _write(d / "rAAA_a0_w0.jsonl", "まだ平文の用事", "やりました DONE")
    _write(d / "rBBB_a0_w0.jsonl", "圧縮された用事", "やりました DONE", compressed=True)
    out = FR.load_completions(transcripts=str(d))
    assert len(out) == 2, sorted(out)


def test_a_run_that_has_both_files_is_read_once(tmp_path):
    """A just-compressed run briefly has both. Reading both would double a worker's claim in
    the reconciliation, and `open_maybe_gz` prefers the plain file, so the names are collapsed
    before opening rather than after."""
    d = tmp_path / "transcripts"
    d.mkdir()
    _write(d / "rAAA_a0_w0.jsonl", "両方ある用事", "やりました DONE")
    _write(d / "rAAA_a0_w0.jsonl", "両方ある用事", "やりました DONE", compressed=True)
    out = FR.load_completions(transcripts=str(d))
    claims = [c for v in out.values() for c in v]
    assert len(claims) == 1, "同じ run を2回読んでいる: %r" % (claims,)


def test_the_run_filter_still_narrows(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    _write(d / "rAAA_a0_w0.jsonl", "Aの用事", "DONE", compressed=True)
    _write(d / "rBBB_a0_w0.jsonl", "Bの用事", "DONE", compressed=True)
    out = FR.load_completions(run="rAAA", transcripts=str(d))
    goals = [goal for rows in out.values() for _worker, goal, _final in rows]
    assert len(out) == 1 and any("Aの用事" in g for g in goals), goals


def test_an_empty_directory_is_not_an_error(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    assert FR.load_completions(transcripts=str(d)) == {}


def test_it_goes_through_the_shared_opener():
    """Three implementations of this rule already exist in the repository. A fourth written
    here would be the same mistake with a different author."""
    src = open(os.path.join(REPO, "relay", "fleet_reconcile.py"), encoding="utf-8").read()
    assert "open_maybe_gz" in src, "共有の opener を通していない"
    # SCOPED TO THE FUNCTION, not the file: the word appears in the comment explaining why the
    # shared opener is used, and forbidding that would push the explanation out of the code.
    i = src.index("def load_completions(")
    body = src[i:src.index("\ndef ", i + 10)]
    assert "import gzip" not in body and "gzip.open" not in body, (
        "この読み手が自前で gzip を開いている（4つ目の実装）")
