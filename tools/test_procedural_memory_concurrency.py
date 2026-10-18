"""Concurrency / torn-read regression tests for tools/procedural_memory.py.

Incident (measured): the production .procedural_memory.json shrank from 725
entries to 90 while several processes saved at once. Mechanism: _load() turned
any read/parse failure into an EMPTY state and _save() rewrote the whole file
non-atomically with no lock, so (1) concurrent writers lost each other's entries
and (2) a reader that hit a half-written file saved a one-entry state over it.

Hermetic: STATE_FILE is redirected to a tmp_path file in every process.

Run: pytest -q tools\\test_procedural_memory_concurrency.py
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os

import pytest

from tools import procedural_memory as pm

N_PROCS = 6
SAVES_PER_PROC = 12


def _count(path) -> int:
    return len(json.loads(path.read_text(encoding="utf-8"))["procedures"])


def _worker(state_path: str, worker_id: int, saves: int) -> None:
    from pathlib import Path

    from tools import procedural_memory as wpm

    wpm.STATE_FILE = Path(state_path)
    wpm.require_unlocked = lambda: None
    filler = "x" * 3000  # make the file big enough that a write takes real time
    for i in range(saves):
        wpm.procedural_memory_save(
            intent=f"worker {worker_id} entry {i}", snippet=f"{filler} {worker_id}/{i}"
        )


def test_concurrent_processes_lose_no_entry(tmp_path):
    state = tmp_path / ".procedural_memory.json"
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_worker, args=(str(state), w, SAVES_PER_PROC))
        for w in range(N_PROCS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    assert _count(state) == N_PROCS * SAVES_PER_PROC


def _seed(tmp_path, monkeypatch, n=50):
    state = tmp_path / ".procedural_memory.json"
    monkeypatch.setattr(pm, "STATE_FILE", state)
    monkeypatch.setattr(pm, "require_unlocked", lambda: None)
    for i in range(n):
        pm.procedural_memory_save(intent=f"seed entry {i}", snippet=f"body {i}")
    # a second round so at least one generation backup holds the full set
    pm.procedural_memory_save(intent="seed extra", snippet="body extra")
    return state


@pytest.mark.parametrize(
    "torn",
    ['{"procedures": {"a": {"intent": "x", "snip', "", "[1, 2, 3]", "\x00" * 64],
    ids=["truncated", "empty", "wrong-shape", "nul-bytes"],
)
def test_unreadable_file_is_never_replaced_by_a_smaller_state(
    tmp_path, monkeypatch, torn
):
    state = _seed(tmp_path, monkeypatch)
    before = _count(state)
    state.write_text(torn, encoding="utf-8")  # a writer is "half-way through"
    result = pm.procedural_memory_save(intent="late arrival", snippet="new body")
    raw = state.read_text(encoding="utf-8", errors="replace")
    try:
        after = len(json.loads(raw)["procedures"])
    except Exception:
        after = None  # file left untouched/unreadable == save refused: acceptable
    assert after is None or after >= before, (
        f"file shrank {before} -> {after}; save said {result!r}"
    )


def test_refuses_save_when_nothing_valid_exists(tmp_path, monkeypatch):
    state = tmp_path / ".procedural_memory.json"
    monkeypatch.setattr(pm, "STATE_FILE", state)
    monkeypatch.setattr(pm, "require_unlocked", lambda: None)
    state.write_text("{ torn", encoding="utf-8")
    result = pm.procedural_memory_save(intent="x", snippet="y")
    assert "error" in result.lower()
    assert state.read_text(encoding="utf-8") == "{ torn"


def test_save_that_would_shrink_the_store_is_refused(tmp_path, monkeypatch):
    state = _seed(tmp_path, monkeypatch)
    from tools import state_store

    with pytest.raises(state_store.StateShrinkRefused):
        state_store.update_json(
            state,
            lambda s: s["procedures"].clear(),
            validate=pm._valid_state,
            count=pm._entry_count,
            default=lambda: {"procedures": {}},
        )
    assert _count(state) == 51


def test_explicit_delete_may_shrink(tmp_path, monkeypatch):
    state = _seed(tmp_path, monkeypatch)
    out = pm.procedural_memory_delete("seed entry 3")
    assert "deleted" in out
    assert _count(state) == 50


def test_backup_is_used_and_logged(tmp_path, monkeypatch, caplog):
    state = _seed(tmp_path, monkeypatch)
    state.write_text("{ torn", encoding="utf-8")
    with caplog.at_level("WARNING"):
        out = pm.procedural_memory_save(intent="after tear", snippet="b")
    assert "saved procedure" in out
    assert any("backup" in r.getMessage().lower() for r in caplog.records)
    assert _count(state) == 52
    assert os.path.exists(str(state) + ".bak.1")


def test_auto_save_hook_cannot_shrink_or_replace_a_damaged_store(tmp_path, monkeypatch):
    """data_memory_hook (ON by default) saves through procedural_memory_save, so
    a damaged store with no valid backup is left untouched and a healthy one
    only ever grows."""
    from tools import data_memory_hook as hook

    monkeypatch.delenv("MCP_DATA_MEMORY_AUTO", raising=False)
    assert hook._auto_enabled() is True  # the in-code default is ON
    state = tmp_path / ".procedural_memory.json"
    monkeypatch.setattr(pm, "STATE_FILE", state)
    monkeypatch.setattr(pm, "require_unlocked", lambda: None)
    state.write_text("{ torn", encoding="utf-8")
    hook.record_query("conn", "SELECT 1 FROM SOME_TABLE_X", "col\n1\n2\n3")
    assert state.read_text(encoding="utf-8") == "{ torn"

    state.unlink()
    for i in range(30):
        pm.procedural_memory_save(intent=f"keep {i}", snippet="b")
    hook.record_query("conn", "SELECT 1 FROM SOME_TABLE_X", "col\n1\n2\n3")
    assert _count(state) == 31
