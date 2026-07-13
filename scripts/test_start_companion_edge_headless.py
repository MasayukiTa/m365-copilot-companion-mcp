from pathlib import Path


SOURCE = (Path(__file__).parent / "start_companion_edge.ps1").read_text(encoding="utf-8-sig")


def test_headless_is_the_unconditional_default_and_recovery_baseline():
    assert "$useHeadless = -not $Foreground" in SOURCE
    assert 'Set-Content -Path $modeFile -Value "headless"' in SOURCE
    assert 'Set-Content -Path $modeFile -Value "headed"' not in SOURCE


def test_foreground_is_explicit_and_cannot_conflict_with_headless():
    assert "if ($Headless -and $Foreground)" in SOURCE
    assert "mutually exclusive" in SOURCE
