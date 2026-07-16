from scripts import bootstrap


def test_existing_env_gets_default_off_once(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MCP_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "step_header", lambda _text: None)
    monkeypatch.setattr(bootstrap, "log", lambda _text: None)
    bootstrap.step_gen_env()
    bootstrap.step_gen_env()
    text = env.read_text(encoding="utf-8")
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
    assert env.read_text(encoding="utf-8") == original


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
