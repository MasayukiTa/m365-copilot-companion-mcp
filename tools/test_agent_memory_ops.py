"""Hermetic tests for the agent_memory/ WRITE engine (tools/agent_memory_ops.py).

Mirrors tools/test_procedural_memory.py's technique: monkeypatch the module's
storage root (here: MEM_DIR) to a throwaway tmp_path directory and make the
write-gate (require_unlocked) inert, so this runs under plain pytest with no
HTTP request context and NEVER touches the real agent_memory/ directory.

All fixture data below is SYNTHETIC (fake topic ids, fake host "host.example",
fake report format) -- no real company/user/email strings anywhere.

Run: pytest -q tools\\test_agent_memory_ops.py
"""
from __future__ import annotations

import json

import pytest

from tools import agent_memory_ops as amo


@pytest.fixture(autouse=True)
def _tmp_mem_dir(tmp_path, monkeypatch):
    """Redirect the module's storage root to a throwaway tmp path and make the
    write-gate inert. Every path helper in agent_memory_ops.py derives from
    the MEM_DIR global looked up fresh at call time, so patching just this one
    attribute redirects topics/, facts/, sessions/ and index.json together."""
    monkeypatch.setattr(amo, "MEM_DIR", tmp_path / "agent_memory")
    monkeypatch.setattr(amo, "require_unlocked", lambda: None)
    yield


# ===========================================================================
# memory_save: topic creation from template + schema
# ===========================================================================


def test_save_creates_topic_with_full_schema():
    result = amo.memory_save(
        topic_id="infra_ssh",
        title="Infra SSH access notes",
        summary="How to reach the fake staging host over SSH.",
        tags=["infra", "ssh"],
        keywords=["ssh", "staging"],
    )
    assert "created" in result.lower()

    topic = amo._load_topic("infra_ssh")
    assert topic is not None
    # every schema field from topic_template.json must be present
    for field in (
        "topic_id", "title", "status", "created", "updated", "tags", "keywords",
        "summary", "data_sources", "method", "key_facts", "hypotheses",
        "decisions", "artifacts", "open_questions", "next_actions", "related_topics",
    ):
        assert field in topic, f"missing schema field: {field}"
    assert topic["topic_id"] == "infra_ssh"
    assert topic["title"] == "Infra SSH access notes"
    assert topic["status"] == "active"
    assert topic["tags"] == ["infra", "ssh"]
    assert topic["keywords"] == ["ssh", "staging"]
    assert topic["created"] == topic["updated"]


def test_save_second_call_updates_not_recreate():
    amo.memory_save(topic_id="infra_ssh", title="Infra SSH access notes")
    result = amo.memory_save(topic_id="infra_ssh", summary="Updated summary text.")
    assert "updated" in result.lower()
    topic = amo._load_topic("infra_ssh")
    assert topic["summary"] == "Updated summary text."
    assert topic["created"] != "" and topic["created"] is not None


# ===========================================================================
# memory_save: appends to the right arrays
# ===========================================================================


def test_save_appends_key_fact():
    amo.memory_save(
        topic_id="infra_ssh",
        key_fact="the fake staging host is host.example",
        confidence="high",
    )
    topic = amo._load_topic("infra_ssh")
    assert topic["key_facts"] == [{"fact": "the fake staging host is host.example", "confidence": "high"}]


def test_save_appends_data_source():
    amo.memory_save(
        topic_id="infra_ssh",
        data_source="ssh://host.example:22",
        source_note="fake staging box, key-based auth only",
    )
    topic = amo._load_topic("infra_ssh")
    assert topic["data_sources"] == [
        {"path": "ssh://host.example:22", "note": "fake staging box, key-based auth only"}
    ]


def test_save_appends_method_step():
    amo.memory_save(topic_id="infra_ssh", method_step="ssh -i fake_key.pem user@host.example")
    topic = amo._load_topic("infra_ssh")
    assert topic["method"] == ["ssh -i fake_key.pem user@host.example"]


def test_save_appends_decision():
    amo.memory_save(
        topic_id="infra_ssh",
        decision="always use the fake_key.pem identity file",
        rationale="password auth is disabled on the fake staging host",
    )
    topic = amo._load_topic("infra_ssh")
    assert len(topic["decisions"]) == 1
    d = topic["decisions"][0]
    assert d["decision"] == "always use the fake_key.pem identity file"
    assert d["rationale"] == "password auth is disabled on the fake staging host"
    assert "when" in d and d["when"]


def test_save_appends_artifact():
    amo.memory_save(
        topic_id="infra_ssh",
        artifact="reports/fake_weekly_status_format.docx",
        artifact_type="report",
        artifact_note="standard weekly status report format template",
    )
    topic = amo._load_topic("infra_ssh")
    assert topic["artifacts"] == [
        {
            "path": "reports/fake_weekly_status_format.docx",
            "type": "report",
            "note": "standard weekly status report format template",
        }
    ]


def test_save_appends_next_action_and_open_question():
    amo.memory_save(
        topic_id="infra_ssh",
        next_action="rotate the fake_key.pem before it expires",
        open_question="does the fake staging host support ed25519 keys?",
    )
    topic = amo._load_topic("infra_ssh")
    assert topic["next_actions"] == ["rotate the fake_key.pem before it expires"]
    assert topic["open_questions"] == ["does the fake staging host support ed25519 keys?"]


def test_save_accepts_comma_separated_tags_string():
    amo.memory_save(topic_id="infra_ssh", tags="alpha, beta", keywords="gamma,delta")
    topic = amo._load_topic("infra_ssh")
    assert topic["tags"] == ["alpha", "beta"]
    assert topic["keywords"] == ["gamma", "delta"]


def test_save_merges_tags_without_duplicating():
    amo.memory_save(topic_id="infra_ssh", tags=["alpha"])
    amo.memory_save(topic_id="infra_ssh", tags=["alpha", "beta"])
    topic = amo._load_topic("infra_ssh")
    assert topic["tags"] == ["alpha", "beta"]


# ===========================================================================
# memory_save: de-duplication
# ===========================================================================


def test_save_dedupes_repeated_key_fact():
    amo.memory_save(topic_id="infra_ssh", key_fact="dup fact", confidence="medium")
    result = amo.memory_save(topic_id="infra_ssh", key_fact="dup fact", confidence="medium")
    topic = amo._load_topic("infra_ssh")
    assert len(topic["key_facts"]) == 1
    assert "already present" in result.lower()


def test_save_dedupes_repeated_data_source():
    amo.memory_save(topic_id="infra_ssh", data_source="ssh://host.example:22", source_note="n1")
    amo.memory_save(topic_id="infra_ssh", data_source="ssh://host.example:22", source_note="n1")
    topic = amo._load_topic("infra_ssh")
    assert len(topic["data_sources"]) == 1


def test_save_dedupes_repeated_method_step():
    amo.memory_save(topic_id="infra_ssh", method_step="step one")
    amo.memory_save(topic_id="infra_ssh", method_step="step one")
    topic = amo._load_topic("infra_ssh")
    assert topic["method"] == ["step one"]


def test_save_dedupes_repeated_decision():
    amo.memory_save(topic_id="infra_ssh", decision="use fake_key.pem", rationale="r1")
    amo.memory_save(topic_id="infra_ssh", decision="use fake_key.pem", rationale="r2")
    topic = amo._load_topic("infra_ssh")
    # decision text is the identity -- second call with a different rationale
    # is still a repeat of the same decision, not a second entry
    assert len(topic["decisions"]) == 1


def test_save_dedupes_repeated_artifact():
    amo.memory_save(topic_id="infra_ssh", artifact="reports/fake_format.docx", artifact_type="report")
    amo.memory_save(topic_id="infra_ssh", artifact="reports/fake_format.docx", artifact_type="report")
    topic = amo._load_topic("infra_ssh")
    assert len(topic["artifacts"]) == 1


def test_save_dedupes_repeated_next_action_and_open_question():
    amo.memory_save(topic_id="infra_ssh", next_action="do the thing")
    amo.memory_save(topic_id="infra_ssh", next_action="do the thing")
    amo.memory_save(topic_id="infra_ssh", open_question="why though?")
    amo.memory_save(topic_id="infra_ssh", open_question="why though?")
    topic = amo._load_topic("infra_ssh")
    assert topic["next_actions"] == ["do the thing"]
    assert topic["open_questions"] == ["why though?"]


# ===========================================================================
# memory_save: index.json upsert
# ===========================================================================


def test_save_upserts_index_entry():
    amo.memory_save(
        topic_id="infra_ssh",
        title="Infra SSH access notes",
        summary="Fake summary for search.",
        tags=["infra"],
    )
    idx = amo._load_index()
    entries = [t for t in idx["topics"] if t["id"] == "infra_ssh"]
    assert len(entries) == 1
    e = entries[0]
    assert e["title"] == "Infra SSH access notes"
    assert e["status"] == "active"
    assert e["tags"] == ["infra"]
    assert e["file"] == "topics/infra_ssh.json"
    assert "Fake summary" in e["one_liner"]
    assert idx["last_updated"]


def test_save_upsert_does_not_duplicate_index_row():
    amo.memory_save(topic_id="infra_ssh", title="v1")
    amo.memory_save(topic_id="infra_ssh", title="v2")
    idx = amo._load_index()
    entries = [t for t in idx["topics"] if t["id"] == "infra_ssh"]
    assert len(entries) == 1
    assert entries[0]["title"] == "v2"


def test_save_bumps_index_last_updated(monkeypatch):
    # _now_iso() is called multiple times per memory_save (created/updated,
    # topic updated, index last_updated) -- use an ever-incrementing fake
    # clock rather than a short fixed sequence, so it never runs dry.
    counter = [0]

    def _fake_now():
        counter[0] += 1
        return f"2026-01-01T00:00:{counter[0]:02d}Z"

    monkeypatch.setattr(amo, "_now_iso", _fake_now)
    amo.memory_save(topic_id="infra_ssh", title="v1")
    idx1 = amo._load_index()
    amo.memory_save(topic_id="infra_ssh", title="v2")
    idx2 = amo._load_index()
    assert idx2["last_updated"] != idx1["last_updated"]


def test_save_never_invents_owner_in_fresh_index():
    amo.memory_save(topic_id="infra_ssh", title="v1")
    idx = amo._load_index()
    assert idx["owner"] == ""


def test_save_preserves_existing_owner_field():
    amo._ensure_dirs()
    amo._atomic_write_json(
        amo._index_path(),
        {"version": "1.0", "last_updated": "", "owner": "placeholder@example.test",
         "description": "d", "facts": [], "topics": [], "boot_protocol": [], "shutdown_protocol": []},
    )
    amo.memory_save(topic_id="infra_ssh", title="v1")
    idx = amo._load_index()
    assert idx["owner"] == "placeholder@example.test"


# ===========================================================================
# atomicity
# ===========================================================================


def test_save_leaves_no_leftover_tmp_files():
    amo.memory_save(topic_id="infra_ssh", title="v1", key_fact="f1")
    amo.memory_save(topic_id="infra_ssh", summary="s1")
    leftover = list(amo._topics_dir().glob(".tmp_*"))
    assert leftover == []
    leftover_idx = list(amo.MEM_DIR.glob(".tmp_*"))
    assert leftover_idx == []


def test_topic_file_is_valid_json_after_save():
    amo.memory_save(topic_id="infra_ssh", title="v1", key_fact="f1")
    raw = amo._topic_path("infra_ssh").read_text(encoding="utf-8")
    json.loads(raw)  # must not raise
    assert not raw.startswith("﻿")  # no BOM


# ===========================================================================
# memory_search
# ===========================================================================


def test_search_finds_by_title_keyword():
    amo.memory_save(topic_id="infra_ssh", title="Infra SSH access notes")
    result = amo.memory_search("infra")
    assert "infra_ssh" in result
    assert "no matches" not in result.lower()


def test_search_finds_by_tag():
    amo.memory_save(topic_id="infra_ssh", title="t", tags=["fakenetworking"])
    result = amo.memory_search("fakenetworking")
    assert "infra_ssh" in result


def test_search_finds_by_key_fact_text():
    amo.memory_save(topic_id="infra_ssh", title="t", key_fact="the widget count is forty-two")
    result = amo.memory_search("widget forty-two")
    assert "infra_ssh" in result


def test_search_finds_by_data_source_text():
    amo.memory_save(topic_id="infra_ssh", title="t", data_source="db://fake_reporting_view")
    result = amo.memory_search("fake_reporting_view")
    assert "infra_ssh" in result


def test_search_ranks_more_token_matches_higher():
    amo.memory_save(topic_id="topic_a", title="alpha only", tags=["alpha"])
    amo.memory_save(topic_id="topic_b", title="alpha beta", tags=["alpha", "beta"])
    result = amo.memory_search("alpha beta")
    assert result.index("topic_b") < result.index("topic_a")


def test_search_empty_query_is_clean():
    assert "no matches" in amo.memory_search("").lower()


def test_search_on_empty_store_is_clean():
    assert amo.memory_search("anything") == "(no matches)"


def test_search_finds_facts_files():
    amo._ensure_dirs()
    amo._atomic_write_json(
        amo._facts_dir() / "fake_profile.json",
        {"name": "Fake Person", "role": "fake_role", "note": "prefers concise reports"},
    )
    result = amo.memory_search("concise reports")
    assert "fact:fake_profile" in result


# ===========================================================================
# memory_read
# ===========================================================================


def test_read_returns_full_topic_json():
    amo.memory_save(topic_id="infra_ssh", title="Infra SSH access notes", key_fact="f1")
    result = amo.memory_read("infra_ssh")
    parsed = json.loads(result)
    assert parsed["topic_id"] == "infra_ssh"
    assert parsed["title"] == "Infra SSH access notes"
    assert parsed["key_facts"][0]["fact"] == "f1"


def test_read_missing_topic_is_clean_message():
    result = amo.memory_read("no_such_topic_at_all")
    assert "no topic found" in result.lower()


def test_read_rejects_empty_topic_id_without_raising():
    result = amo.memory_read("")
    assert "error" in result.lower()


# ===========================================================================
# memory_list
# ===========================================================================


def test_list_shows_saved_topics():
    amo.memory_save(topic_id="infra_ssh", title="Infra SSH access notes", summary="fake summary one")
    amo.memory_save(topic_id="report_format", title="Weekly report format", summary="fake summary two")
    result = amo.memory_list()
    assert "infra_ssh" in result
    assert "report_format" in result
    assert "2 topic(s)" in result


def test_list_on_empty_store_is_clean():
    assert amo.memory_list() == "(no topics in memory yet)"


def test_list_falls_back_to_scanning_topics_dir_when_index_missing():
    amo._ensure_dirs()
    amo._atomic_write_json(amo._topic_path("orphan_topic"), amo._new_topic("orphan_topic", title="Orphan"))
    # index.json was never written by memory_save in this test -- list must
    # still surface the topic by scanning topics/ directly.
    result = amo.memory_list()
    assert "orphan_topic" in result


# ===========================================================================
# corrupt-file tolerance
# ===========================================================================


def test_malformed_topic_file_is_tolerated_by_read():
    amo._ensure_dirs()
    (amo._topics_dir() / "broken.json").write_text("{ not valid json", encoding="utf-8")
    result = amo.memory_read("broken")
    assert "no topic found" in result.lower()


def test_malformed_topic_file_is_tolerated_by_search():
    amo._ensure_dirs()
    (amo._topics_dir() / "broken.json").write_text("{ not valid json", encoding="utf-8")
    result = amo.memory_search("anything")
    assert result == "(no matches)"


def test_malformed_index_file_is_tolerated():
    amo._ensure_dirs()
    amo._index_path().write_text("not json at all {{{", encoding="utf-8")
    result = amo.memory_list()
    assert "error" not in result.lower()


def test_wrong_shape_index_file_is_tolerated():
    amo._ensure_dirs()
    amo._index_path().write_text('["not", "a", "dict"]', encoding="utf-8")
    idx = amo._load_index()
    assert idx["topics"] == []


def test_save_recovers_cleanly_after_malformed_index():
    amo._ensure_dirs()
    amo._index_path().write_text("{{{ broken", encoding="utf-8")
    result = amo.memory_save(topic_id="infra_ssh", title="v1")
    assert "created" in result.lower() or "updated" in result.lower()
    idx = amo._load_index()
    assert any(t["id"] == "infra_ssh" for t in idx["topics"])


def test_missing_topic_dir_is_tolerated_by_list_and_search():
    # MEM_DIR itself does not exist yet (fixture only set the path, nothing created)
    assert amo.memory_list() == "(no topics in memory yet)"
    assert amo.memory_search("anything") == "(no matches)"


# ===========================================================================
# validation / never raises
# ===========================================================================


def test_save_rejects_empty_topic_id_without_raising():
    result = amo.memory_save(topic_id="")
    assert "error" in result.lower()


def test_save_invalid_confidence_falls_back_to_medium():
    amo.memory_save(topic_id="infra_ssh", key_fact="f1", confidence="not-a-real-level")
    topic = amo._load_topic("infra_ssh")
    assert topic["key_facts"][0]["confidence"] == "medium"


def test_save_invalid_artifact_type_falls_back_to_other():
    amo.memory_save(topic_id="infra_ssh", artifact="a/path", artifact_type="not-a-real-type")
    topic = amo._load_topic("infra_ssh")
    assert topic["artifacts"][0]["type"] == "other"


def test_topic_id_is_sanitized_against_path_traversal():
    amo.memory_save(topic_id="../../evil", title="t")
    # must land inside topics_dir, never escape it
    for p in amo._topics_dir().glob("*.json"):
        assert ".." not in p.name
    assert not (amo.MEM_DIR.parent / "evil.json").exists()


# ===========================================================================
# gating: agent_memory_save requires unlock; search/read/list do not
# ===========================================================================


def test_agent_memory_save_is_gated_by_require_unlocked(monkeypatch):
    monkeypatch.setattr(amo, "require_unlocked", lambda: "[locked] no.")
    result = amo.agent_memory_save(topic_id="infra_ssh", title="t")
    assert result == "[locked] no."
    # nothing was written
    assert amo._load_topic("infra_ssh") is None


def test_memory_save_is_gated_by_require_unlocked(monkeypatch):
    monkeypatch.setattr(amo, "require_unlocked", lambda: "[locked] no.")
    result = amo.memory_save(topic_id="infra_ssh", title="t")
    assert result == "[locked] no."


def test_agent_memory_search_not_gated(monkeypatch):
    monkeypatch.setattr(amo, "require_unlocked", lambda: "[locked] no.")
    result = amo.agent_memory_search("anything")
    assert result == "(no matches)"


def test_agent_memory_read_not_gated(monkeypatch):
    monkeypatch.setattr(amo, "require_unlocked", lambda: "[locked] no.")
    result = amo.agent_memory_read("no_such_topic")
    assert "no topic found" in result.lower()


def test_agent_memory_list_not_gated(monkeypatch):
    monkeypatch.setattr(amo, "require_unlocked", lambda: "[locked] no.")
    result = amo.agent_memory_list()
    assert result == "(no topics in memory yet)"


# ===========================================================================
# wrapper functions delegate correctly (unique-name collision avoidance)
# ===========================================================================


def test_wrapper_names_are_distinct_from_memory_ops_module():
    from tools import memory_ops

    assert amo.memory_save.__name__ == "memory_save"
    assert memory_ops.memory_save.__name__ == "memory_save"
    # but the MCP-registered wrapper names must differ from memory_ops' exports
    assert amo.agent_memory_save.__name__ == "agent_memory_save"
    assert amo.agent_memory_search.__name__ == "agent_memory_search"
    assert amo.agent_memory_read.__name__ == "agent_memory_read"
    assert amo.agent_memory_list.__name__ == "agent_memory_list"
    assert not hasattr(memory_ops, "agent_memory_save")


def test_agent_memory_save_wrapper_delegates_to_memory_save():
    result = amo.agent_memory_save(topic_id="infra_ssh", title="Infra SSH access notes")
    assert "created" in result.lower()
    topic = amo._load_topic("infra_ssh")
    assert topic["title"] == "Infra SSH access notes"


def test_agent_memory_search_wrapper_delegates_to_memory_search():
    amo.memory_save(topic_id="infra_ssh", title="Infra SSH access notes")
    assert amo.agent_memory_search("infra") == amo.memory_search("infra")


def test_agent_memory_read_wrapper_delegates_to_memory_read():
    amo.memory_save(topic_id="infra_ssh", title="Infra SSH access notes")
    assert amo.agent_memory_read("infra_ssh") == amo.memory_read("infra_ssh")


def test_agent_memory_list_wrapper_delegates_to_memory_list():
    amo.memory_save(topic_id="infra_ssh", title="Infra SSH access notes")
    assert amo.agent_memory_list() == amo.memory_list()
