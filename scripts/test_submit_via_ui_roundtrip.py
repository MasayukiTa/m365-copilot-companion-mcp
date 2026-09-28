from pathlib import Path

SRC = Path(__file__).with_name("win").joinpath("submit_via_ui.ps1").read_text(
    encoding="utf-8-sig", errors="replace"
)

def test_submit_rereads_exact_composer_value_before_invoke():
    i = SRC.index("function Submit([string]$text)")
    block = SRC[i:SRC.index("# IS A RUN ALREADY GOING?", i)]
    set_i = block.index("Set-Text $target $text")
    verify_i = block.index("[String]::Equals(")
    invoke_i = block.index("$ip.Invoke()")
    assert set_i < verify_i < invoke_i
    assert "[StringComparison]::Ordinal" in block
    assert "composer text changed before submit" in block

def test_mismatch_fails_closed_without_invoking_or_clearing_user_text():
    i = SRC.index("function Submit([string]$text)")
    block = SRC[i:SRC.index("# IS A RUN ALREADY GOING?", i)]
    start = block.index("if (-not [String]::Equals(")
    end = block.index("[Console]::Error.WriteLine", start)
    mismatch = block[start:end]
    assert "throw" in mismatch
    assert "$ip.Invoke()" not in mismatch
    assert "SetValue(\\\"\\\")" not in mismatch
