from pathlib import Path


SOURCE = (Path(__file__).parent / "CopilotChat.cs").read_text(encoding="utf-8-sig")


def test_p2c_commands_are_flag_gated_in_native_slash_palette():
    palette = SOURCE[SOURCE.index("string[][] _commands") : SOURCE.index("void BuildCmdPopup")]

    assert "P2cReviewEnabled()" in palette
    assert "if (!P2cReviewEnabled()) return baseCommands;" in palette
    assert "_p2cCommandJaReview" in palette
    assert "_p2cCommandJaSecurity" in palette
    assert 'new[]{"/review-2"' in SOURCE
    assert 'new[]{"/security-review-2"' in SOURCE


def test_p2c_commands_are_flag_gated_in_native_help():
    help_method = SOURCE[SOURCE.index("string CommandHelpText()") : SOURCE.index("// Repo root:")]

    assert "if (P2cReviewEnabled())" in help_method
    assert "/review-2 [diff|<path>]" in help_method
    assert "/security-review-2 [diff|<path>]" in help_method
