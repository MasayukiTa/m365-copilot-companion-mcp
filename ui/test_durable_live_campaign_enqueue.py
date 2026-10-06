from pathlib import Path

SOURCE = Path(__file__).with_name('FleetCockpit.cs').read_text(encoding='utf-8-sig', errors='replace')


def test_local_loop_live_add_uses_durable_campaign_enqueue_not_fleet_commands():
    start = SOURCE.index('void TryAddGoalsToActiveRun()')
    block = SOURCE[start:SOURCE.index('void TryAddGoalsToLiveFleet()', start)]
    assert 'if (ActiveRunIsLocalLoop())' in block
    assert 'TryAddGoalsToDurableRun();' in block
    assert 'TryAddGoalsToLiveFleet();' in block
    assert block.index('TryAddGoalsToDurableRun();') < block.index('TryAddGoalsToLiveFleet();')


def test_durable_live_add_writes_only_plain_goal_array_and_invokes_python_intake():
    start = SOURCE.index('void TryAddGoalsToDurableRun()')
    block = SOURCE[start:SOURCE.index('void TryAddGoalsToLiveFleet()', start)]
    assert '_js.Serialize(goals)' in block
    assert '--enqueue-goals-file' in block
    assert 'relay.local_loop_controller' in block
    assert 'psi.CreateNoWindow = true;' in block
    assert 'FleetCommands.Write' not in block
    assert 'Cmd1("add_goal"' not in block


def test_durable_live_add_keeps_input_until_enqueue_process_reports_success():
    start = SOURCE.index('void TryAddGoalsToDurableRun()')
    block = SOURCE[start:SOURCE.index('void TryAddGoalsToLiveFleet()', start)]
    assert 'DispatcherTimer' in block
    assert 'if (!proc.HasExited) return;' in block
    assert 'if (proc.ExitCode == 0)' in block
    clear = block.index('_goalInput.Text = "";')
    success = block.index('if (proc.ExitCode == 0)')
    assert success < clear
    assert 'if (_goalInput.Text == submittedText)' in block
    assert 'NoteSubmitted(goals, submitBaseline);' in block


def test_phase2_placeholder_is_gone():
    assert 'Phase 2 will wire this branch to the durable campaign queue' not in SOURCE
    assert 'Live additions to the durable runtime are not queued yet' not in SOURCE
