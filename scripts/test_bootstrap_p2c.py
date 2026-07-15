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
    assert "MCP_API_KEY=secret" in text


def test_existing_explicit_on_is_never_overwritten(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MCP_REVIEW_P2C=1\nMCP_EXECUTION_PROFILES=1\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "step_header", lambda _text: None)
    monkeypatch.setattr(bootstrap, "log", lambda _text: None)
    bootstrap.step_gen_env()
    assert env.read_text(encoding="utf-8") == (
        "MCP_REVIEW_P2C=1\nMCP_EXECUTION_PROFILES=1\n"
    )
