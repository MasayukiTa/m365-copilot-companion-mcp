# -*- coding: utf-8 -*-
"""The digest that joins an attempt to its verdict, computed in two places.

`relay/mechanism_telemetry.py::patch_hash` says what it is for:

    The join key to the grader. A patch is the artefact that gets graded, so its hash is what
    lets an attempt's telemetry meet its verdict without guessing.

It had no caller. `bench/pro_capture.py::_emit` -- the one place a captured patch is written
down -- computed the identical digest inline, in a function whose own docstring makes the
argument against exactly that:

    Factored out so the routed path records EXACTLY what the local path records. Written twice,
    these drift, and the drift shows up as a scoring difference between two runs that were
    supposed to differ only in where the work happened.

MEASURED 2026-09-13 on `.fleet/mechanisms.jsonl`: 5614 records, 2265 carrying
self_report_outcome and **0 carrying artifact_hash**. Half of the join key the module exists to
provide has never been written by anything. Wiring the digest to one implementation does not
close that -- see docs/unreached_burndown.md, where it is recorded as measured and open --
because the patch is captured out-of-process, after the worker that would have written the
telemetry row has already settled.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bench import pro_capture as PC  # noqa: E402
from relay.mechanism_telemetry import patch_hash  # noqa: E402


# ── one implementation ────────────────────────────────────────────────────────────────────

def test_the_snapshot_digest_is_the_shared_one(tmp_path, capsys):
    """THE WIRING, checked by running it rather than by reading it."""
    PC.SW = str(tmp_path)
    preds = []
    PC._emit(preds, set(), "astropy__astropy-1234", "diff --git a/x b/x\n", "pfx")
    capsys.readouterr()

    snaps = list((tmp_path / "attempts").glob("*.json"))
    assert len(snaps) == 1, snaps
    row = json.loads(snaps[0].read_text(encoding="utf-8"))
    assert row["patch_sha256_16"] == patch_hash("diff --git a/x b/x\n")


def test_the_capture_no_longer_hashes_by_hand():
    """A second implementation added back would pass the test above and still drift."""
    src = io.open(os.path.join(REPO, "bench", "pro_capture.py"), encoding="utf-8").read()
    assert "hashlib" not in src, "自前で digest を計算する実装が戻っている"
    assert "patch_hash" in src


def test_an_empty_patch_still_gets_a_digest():
    """An empty patch is a real outcome -- a worker that changed nothing -- and must be
    joinable like any other, not indistinguishable from a capture that failed."""
    assert patch_hash("") == patch_hash("")
    assert len(patch_hash("")) == 16


# ── the import that was not allowed to fail quietly ───────────────────────────────────────

def test_it_still_runs_as_a_script():
    """`python bench/pro_capture.py` puts bench/ on sys.path[0], where `import relay` raises
    ImportError. That failure mode already cost this file one silent zero-scoring run, so the
    new import is checked in the invocation form that breaks it, not only under pytest."""
    r = subprocess.run([sys.executable, os.path.join("bench", "pro_capture.py"), "--help"],
                       cwd=REPO, capture_output=True, text=True, errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr
    assert "--preds" in r.stdout


def test_it_still_imports_as_a_module():
    """The other form, which the tests use. Both, because fixing one broke the other here
    before."""
    r = subprocess.run([sys.executable, "-c",
                        "import bench.pro_capture as m; print(m.patch_hash('x'))"],
                       cwd=REPO, capture_output=True, text=True, errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == patch_hash("x")
