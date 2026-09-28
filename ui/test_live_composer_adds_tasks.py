from pathlib import Path
import re

SOURCE = Path(__file__).with_name('FleetCockpit.cs').read_text(encoding='utf-8', errors='replace')


def _composer_handler_blocks():
    # The same choice exists in Ctrl+Enter and the primary button.
    return re.findall(r'if \(HandleSlashSetting\(\)\) return;\s*if \(_composerRunActive\) ([A-Za-z0-9_]+)\(\);\s*else StartFleet\(\);', SOURCE)


def test_live_composer_adds_a_new_task_instead_of_steering_an_existing_worker():
    calls = _composer_handler_blocks()
    assert len(calls) >= 2, calls
    assert all(name == 'TryAddGoalsToActiveRun' for name in calls), calls


def test_live_task_add_uses_the_same_lossless_command_channel_and_immediate_ui_row():
    assert 'void TryAddGoalsToLiveFleet()' in SOURCE
    block = SOURCE[SOURCE.index('void TryAddGoalsToLiveFleet()'):]
    block = block[:block.index('\n    void ', 10)]
    assert 'Cmd1("add_goal", adds)' in block
    assert 'SendTrackedCommand(patch, out commandPath)' in block
    assert 'patch["ack"] = ackPath' in block
    assert 'WatchLiveAddHandoff(commandPath, ackPath)' in block
    assert 'NoteSubmitted(goals, submitBaseline)' in block
    assert '_goalInput.Text = ""' in block


def test_steer_remains_a_per_worker_action():
    assert 'bool TrySteerSend(string name, string text, out string failReason)' in SOURCE
    assert 'RequestSteer(name, t)' in SOURCE


def test_durable_live_composer_never_crosses_into_fleet_command_channel():
    assert 'bool ActiveRunIsLocalLoop()' in SOURCE
    assert 'S(st, "execution_mode"), "LOCAL_LOOP"' in SOURCE
    start = SOURCE.index('void TryAddGoalsToActiveRun()')
    block = SOURCE[start:SOURCE.index('void TryAddGoalsToLiveFleet()', start)]
    assert 'if (ActiveRunIsLocalLoop())' in block
    assert 'return;' in block
    assert 'TryAddGoalsToLiveFleet();' in block
    assert block.index('return;') < block.index('TryAddGoalsToLiveFleet();')
    assert '_goalInput.Text = ""' not in block


def test_classic_live_add_keeps_the_lossless_fleet_channel():
    start = SOURCE.index('void TryAddGoalsToLiveFleet()')
    block = SOURCE[start:SOURCE.index('void WatchLiveAddHandoff(', start)]
    assert 'Cmd1("add_goal", adds)' in block
    assert 'SendTrackedCommand(patch, out commandPath)' in block
