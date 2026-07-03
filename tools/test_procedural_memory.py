"""Hermetic tests for procedural memory (tools/procedural_memory.py).

Covers the book-grounded (SS17 / SS28.16 taxonomy) addition of a PROCEDURAL
memory store -- reusable success snippets / learned workflows -- distinct from
the repo's existing SEMANTIC store (memory_ops.py) and from the EPISODIC store
(runlog_ops.py, unchanged).

Hermetic: STATE_FILE is monkeypatched to a tmp_path file (same technique
test_layer1_security.py uses for _ENV_FILE) and require_unlocked is
monkeypatched to a no-op (same technique used throughout for gated tools),
so this runs under plain pytest with no HTTP request context and no real
state-file writes into the repo.

Run: pytest -q tools\\test_procedural_memory.py
"""
from __future__ import annotations

import pytest

from tools import procedural_memory as pm


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    """Redirect the module's state file to a throwaway tmp path and make the
    write-gate inert, so tests never touch the real .procedural_memory.json
    and never need an HTTP request context."""
    monkeypatch.setattr(pm, "STATE_FILE", tmp_path / ".procedural_memory.json")
    monkeypatch.setattr(pm, "require_unlocked", lambda: None)
    yield


# ===========================================================================
# save + search
# ===========================================================================


def test_save_then_search_finds_by_intent():
    pm.procedural_memory_save(
        intent="restart prod backend",
        snippet="ssh the eval host && sudo systemctl restart backend",
        tags="the eval host,ops",
        context="use when config changed but code didn't",
    )
    result = pm.procedural_memory_search("restart backend")
    assert "restart_prod_backend" in result
    assert "no matches" not in result.lower()


def test_search_finds_by_tag():
    pm.procedural_memory_save(
        intent="rotate api key",
        snippet="python rotate_key.py --service foo",
        tags="security,rotation",
    )
    result = pm.procedural_memory_search("rotation")
    assert "rotate_api_key" in result


def test_search_finds_by_snippet_token():
    pm.procedural_memory_save(
        intent="clear docker cache",
        snippet="docker system prune -af --volumes",
        tags="docker",
    )
    result = pm.procedural_memory_search("prune")
    assert "clear_docker_cache" in result


def test_search_finds_by_context_token():
    pm.procedural_memory_save(
        intent="fix pagefile wall",
        snippet="reduce .wslconfig memory to 6GB",
        context="hit this on the 16GB laptop during long WSL runs",
    )
    result = pm.procedural_memory_search("laptop")
    assert "fix_pagefile_wall" in result


def test_search_ranks_better_match_higher(monkeypatch):
    """_score counts DISTINCT query tokens matched, so a multi-token query is
    needed to exercise ranking (a single repeated token always scores 1).
    updated_at is pinned equal for both entries so the score itself -- not the
    newest-first tiebreak -- is what's under test."""
    fixed_time = [5000.0]
    monkeypatch.setattr(pm.time, "time", lambda: fixed_time[0])

    # Only "docker" matches here (1 of 2 query tokens).
    pm.procedural_memory_save(
        intent="alpha task",
        snippet="just docker",
        tags="",
    )
    # Both "docker" and "rebuild" match here (2 of 2 query tokens) -> higher score.
    pm.procedural_memory_save(
        intent="docker rebuild",
        snippet="docker compose build --no-cache",
        tags="docker",
    )
    result = pm.procedural_memory_search("docker rebuild")
    idx_first = result.index("[docker_rebuild]")
    idx_second = result.index("[alpha_task]")
    assert idx_first < idx_second, result


def test_search_newest_first_tiebreak(monkeypatch):
    """Two entries with equal token-match score: the more recently updated one
    must be listed first."""
    fake_time = [1000.0]
    monkeypatch.setattr(pm.time, "time", lambda: fake_time[0])

    pm.procedural_memory_save(intent="widget task one", snippet="do widget stuff")
    fake_time[0] = 2000.0
    pm.procedural_memory_save(intent="widget task two", snippet="do widget stuff")

    result = pm.procedural_memory_search("widget")
    idx_two = result.index("[widget_task_two]")
    idx_one = result.index("[widget_task_one]")
    assert idx_two < idx_one, result


# ===========================================================================
# delete
# ===========================================================================


def test_delete_removes_entry():
    pm.procedural_memory_save(intent="temp task", snippet="temp snippet body")
    assert "no matches" not in pm.procedural_memory_search("temp snippet").lower()

    result = pm.procedural_memory_delete("temp_task")
    assert "deleted" in result.lower()

    assert pm.procedural_memory_search("temp snippet") == "(no matches)"


def test_delete_accepts_raw_intent_not_just_slug():
    pm.procedural_memory_save(intent="Another Temp Task", snippet="body text here")
    result = pm.procedural_memory_delete("Another Temp Task")
    assert "deleted" in result.lower()


def test_delete_unknown_is_clean_error():
    result = pm.procedural_memory_delete("no-such-procedure-exists")
    assert "no entry" in result.lower()


# ===========================================================================
# empty store / malformed state / idempotent-ish save
# ===========================================================================


def test_search_on_empty_store_returns_clean_no_matches():
    result = pm.procedural_memory_search("anything")
    assert result == "(no matches)"


def test_search_empty_query_does_not_raise():
    result = pm.procedural_memory_search("")
    assert "no matches" in result.lower()


def test_save_same_intent_twice_updates_and_bumps_revision():
    r1 = pm.procedural_memory_save(intent="repeat task", snippet="v1 snippet")
    assert "rev #1" in r1
    r2 = pm.procedural_memory_save(intent="repeat task", snippet="v2 snippet")
    assert "rev #2" in r2

    result = pm.procedural_memory_search("v2 snippet")
    assert "repeat_task" in result
    # only one entry should exist for this slug, not two
    state = pm._load()
    assert len(state["procedures"]) == 1
    assert state["procedures"]["repeat_task"]["snippet"] == "v2 snippet"


def test_malformed_state_file_is_tolerated(tmp_path, monkeypatch):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{ this is not valid json", encoding="utf-8")
    monkeypatch.setattr(pm, "STATE_FILE", bad_file)

    # _load must not raise, and search over the recovered-empty store is clean.
    result = pm.procedural_memory_search("anything")
    assert result == "(no matches)"

    # save must still work afterward (overwrites the malformed file cleanly).
    save_result = pm.procedural_memory_save(intent="after bad file", snippet="works fine")
    assert "saved procedure" in save_result


def test_missing_state_file_is_tolerated(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "STATE_FILE", tmp_path / "does_not_exist.json")
    result = pm.procedural_memory_search("anything")
    assert result == "(no matches)"


def test_wrong_shape_state_file_is_tolerated(tmp_path, monkeypatch):
    """A JSON file that parses but isn't the expected {"procedures": {...}} shape
    (e.g. a list, or missing the key) must not raise."""
    odd_file = tmp_path / "odd.json"
    odd_file.write_text('["not", "a", "dict"]', encoding="utf-8")
    monkeypatch.setattr(pm, "STATE_FILE", odd_file)
    result = pm.procedural_memory_search("anything")
    assert result == "(no matches)"


# ===========================================================================
# validation errors never raise
# ===========================================================================


def test_save_rejects_empty_intent_without_raising():
    result = pm.procedural_memory_save(intent="", snippet="body")
    assert "error" in result.lower()


def test_save_rejects_empty_snippet_without_raising():
    result = pm.procedural_memory_save(intent="some intent", snippet="")
    assert "error" in result.lower()


def test_save_rejects_oversized_snippet_without_raising():
    huge = "x" * (pm.MAX_SNIPPET_CHARS + 1)
    result = pm.procedural_memory_save(intent="huge", snippet=huge)
    assert "exceeds" in result.lower()


# ===========================================================================
# gating: save/delete require unlock; search does not
# ===========================================================================


def test_save_is_gated_by_require_unlocked(monkeypatch):
    monkeypatch.setattr(pm, "require_unlocked", lambda: "[locked] no.")
    result = pm.procedural_memory_save(intent="gated task", snippet="body")
    assert result == "[locked] no."


def test_delete_is_gated_by_require_unlocked(monkeypatch):
    monkeypatch.setattr(pm, "require_unlocked", lambda: "[locked] no.")
    result = pm.procedural_memory_delete("whatever")
    assert result == "[locked] no."


def test_search_does_not_call_require_unlocked_gate(monkeypatch):
    """search must work even when require_unlocked would deny -- it's read-only
    and must never be gated."""
    monkeypatch.setattr(pm, "require_unlocked", lambda: "[locked] no.")
    result = pm.procedural_memory_search("anything")
    assert result == "(no matches)"  # ran normally, not blocked
