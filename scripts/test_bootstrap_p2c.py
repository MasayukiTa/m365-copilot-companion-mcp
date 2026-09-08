import os

import pytest

from scripts import bootstrap

# WINDOWS-ONLY, AND RUN ON THE WINDOWS JOB INSTEAD. Every test here calls step_gen_env, which
# protects the generated secrets with tools.secret_store.protect_secret -- DPAPI, which raises
# "DPAPI protection is only available on Windows" on the ubuntu runner. These were red there
# for that reason alone, not for anything they assert. A bare skip would have retired the
# coverage silently, so ci.yml's windows-install-smoke job now runs this file.
pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="step_gen_env protects secrets with DPAPI, which is Windows-only")


def test_existing_env_gets_default_off_once(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MCP_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "step_header", lambda _text: None)
    monkeypatch.setattr(bootstrap, "log", lambda _text: None)
    bootstrap.step_gen_env()
    bootstrap.step_gen_env()
    text = env.read_text(encoding="utf-8")
    assert text.count("TASK_JOB_APPROVAL_MODE=default") == 1
    assert text.count("MCP_REVIEW_P2C=0") == 1
    assert text.count("MCP_EXECUTION_PROFILES=0") == 1
    assert text.count("MCP_DEEP_REVIEW_TRANSPORT=auto") == 1
    assert text.count("MCP_LOCAL_REVIEW_MAX_CONCURRENT=2") == 1
    assert text.count("MCP_LOCAL_ROTATE_AFTER_TURNS=3") == 1
    assert text.count("MCP_LOCAL_EDGE_MB_LIMIT=1400") == 1
    assert "MCP_API_KEY=secret" in text


def test_existing_explicit_on_is_never_overwritten(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    original = (
        "MCP_REVIEW_P2C=1\nMCP_EXECUTION_PROFILES=1\n"
        "MCP_DEEP_REVIEW_TRANSPORT=fleet\nMCP_LOCAL_REVIEW_MAX_CONCURRENT=4\n"
        "MCP_LOCAL_ROTATE_AFTER_TURNS=2\nMCP_LOCAL_EDGE_MB_LIMIT=999\n"
    )
    env.write_text(original, encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "step_header", lambda _text: None)
    monkeypatch.setattr(bootstrap, "log", lambda _text: None)
    bootstrap.step_gen_env()
    text = env.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert text.count("TASK_JOB_APPROVAL_MODE=default") == 1
    assert text.count("MCP_REVIEW_P2C=1") == 1


def test_existing_explicit_full_validation_is_never_overwritten(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    original = "MCP_REVIEW_P2C=2\nMCP_EXECUTION_PROFILES=1\n"
    env.write_text(original, encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "step_header", lambda _text: None)
    monkeypatch.setattr(bootstrap, "log", lambda _text: None)
    bootstrap.step_gen_env()
    text = env.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert text.count("MCP_REVIEW_P2C=2") == 1
