from bridge import copilot_bridge


def test_p2c_commands_hidden_by_default_and_shown_only_for_one(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_bridge, "REPO", tmp_path)
    env = tmp_path / ".env"
    env.write_text("MCP_REVIEW_P2C=0\n", encoding="utf-8")
    assert not copilot_bridge._p2c_review_enabled()
    assert "/deep-review" not in copilot_bridge._current_help_text()

    env.write_text("MCP_REVIEW_P2C=1\n", encoding="utf-8")
    assert copilot_bridge._p2c_review_enabled()
    assert "/deep-review" in copilot_bridge._current_help_text()
    assert "/deep-security-review" in copilot_bridge._current_help_text()
    assert "/review-2" not in copilot_bridge._current_help_text()
