from pathlib import Path

SRC = Path(__file__).with_name("win").joinpath("submit_via_ui.ps1").read_text(
    encoding="utf-8-sig", errors="replace"
)

def test_submit_rereads_exact_composer_value_before_invoke():
    i = SRC.index("function Submit([string]$text")
    block = SRC[i:SRC.index("# IS A RUN ALREADY GOING?", i)]
    set_i = block.index("Set-Text $target $text")
    verify_i = block.index("[String]::Equals(")
    invoke_i = block.index("$ip.Invoke()")
    assert set_i < verify_i < invoke_i
    assert "[StringComparison]::Ordinal" in block
    assert "composer text changed before submit" in block

def test_mismatch_fails_closed_without_invoking_or_clearing_user_text():
    i = SRC.index("function Submit([string]$text")
    block = SRC[i:SRC.index("# IS A RUN ALREADY GOING?", i)]
    start = block.index("if (-not [String]::Equals(")
    end = block.index("[Console]::Error.WriteLine", start)
    mismatch = block[start:end]
    assert "throw" in mismatch
    assert "$ip.Invoke()" not in mismatch
    assert "SetValue(\\\"\\\")" not in mismatch


def test_post_invoke_accepts_delayed_fresh_start_or_live_add_by_status_identity():
    i = SRC.index("function Submit([string]$text")
    block = SRC[i:SRC.index("# IS A RUN ALREADY GOING?", i)]
    assert "Get-GoalAcceptanceSnapshot" in SRC
    assert "$baseline = Get-GoalAcceptanceSnapshot $expectedGoals" in block
    assert "$accepted = Test-GoalAccepted $baseline $now $expectedGoals.Count" in block
    assert "$ip.Invoke()" in block
    assert block.index("$ip.Invoke()") < block.index("Test-GoalAccepted")
    assert "the composer still holds text after Ctrl+Enter; nothing was submitted" not in block
    assert "submission was not accepted before the timeout" in block


def test_goal_acceptance_supports_both_fresh_started_change_and_live_count_growth():
    assert "function Test-GoalAccepted" in SRC
    i = SRC.index("function Test-GoalAccepted")
    block = SRC[i:SRC.index("function Submit", i)]
    assert "$fresh =" in block and "$liveGrowth =" in block
    assert "$after.MatchCount -ge ($before.MatchCount + $expectedCount)" in block
    assert "$after.Started -ne $before.Started" in block
    assert "$after.MatchCount -ge $expectedCount" in block


def test_command_submit_keeps_the_old_immediate_clear_contract():
    bottom = SRC[SRC.index("if ($Command)"):]
    assert "Submit $Command" in bottom
    assert 'Submit ($Goal -join "`n") -ExpectFleetGoal' in bottom


def test_header_no_longer_claims_active_bottom_composer_is_steer():
    head = "\n".join(SRC.splitlines()[:25])
    assert "CTRL+ENTER STEERS WHILE A RUN IS ACTIVE" not in head
    assert "bottom composer adds tasks" in head.lower()



def test_automated_gui_submissions_are_serialized_across_processes():
    assert ". (Join-Path $PSScriptRoot 'gui_submit_lock.ps1')" in SRC
    lock_i = SRC.index("$submitLock = Enter-GuiSubmitLock")
    cockpit_i = SRC.index("$win = Get-Cockpit")
    finally_i = SRC.index("} finally {", cockpit_i)
    release_i = SRC.index("Exit-GuiSubmitLock $submitLock", finally_i)
    assert lock_i < cockpit_i < finally_i < release_i
    assert "$submitLockTimeout = [Math]::Max(90, $TimeoutSeconds + 15)" in SRC


def test_readonly_probe_returns_to_inprocess_caller_instead_of_exiting_parent_shell():
    assert "if ($ReadOnly) { return }" in SRC
    assert "if ($ReadOnly) { exit 0 }" not in SRC
