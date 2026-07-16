from bridge import copilot_bridge


def test_p2c_commands_hidden_by_default_and_shown_for_levels_one_and_two(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_bridge, "REPO", tmp_path)
    env = tmp_path / ".env"
    env.write_text("MCP_REVIEW_P2C=0\n", encoding="utf-8")
    assert copilot_bridge._p2c_review_level() == 0
    assert not copilot_bridge._p2c_review_enabled()
    assert "/deep-review" not in copilot_bridge._current_help_text()

    env.write_text("MCP_REVIEW_P2C=1\n", encoding="utf-8")
    assert copilot_bridge._p2c_review_level() == 1
    assert copilot_bridge._p2c_review_enabled()
    assert "/deep-review" in copilot_bridge._current_help_text()
    assert "/deep-security-review" in copilot_bridge._current_help_text()
    assert "/review-2" not in copilot_bridge._current_help_text()

    env.write_text("MCP_REVIEW_P2C=2\n", encoding="utf-8")
    assert copilot_bridge._p2c_review_level() == 2
    assert copilot_bridge._p2c_review_enabled()
    assert "/deep-security-review" in copilot_bridge._current_help_text()


def test_p2c_invalid_value_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_bridge, "REPO", tmp_path)
    (tmp_path / ".env").write_text("MCP_REVIEW_P2C=full\n", encoding="utf-8")
    assert copilot_bridge._p2c_review_level() == 0
    assert not copilot_bridge._p2c_review_enabled()
