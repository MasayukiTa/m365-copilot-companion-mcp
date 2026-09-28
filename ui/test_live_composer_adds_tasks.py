from pathlib import Path
import re

SOURCE = Path(__file__).with_name('FleetCockpit.cs').read_text(encoding='utf-8', errors='replace')


def _composer_handler_blocks():
    # The same choice exists in Ctrl+Enter and the primary button.
    return re.findall(r'if \(HandleSlashSetting\(\)\) return;\s*if \(_composerRunActive\) ([A-Za-z0-9_]+)\(\);\s*else StartFleet\(\);', SOURCE)


def test_live_composer_adds_a_new_task_instead_of_steering_an_existing_worker():
    calls = _composer_handler_blocks()
    assert len(calls) >= 2, calls
    assert all(name == 'TryAddGoalsToLiveFleet' for name in calls), calls


def test_live_task_add_uses_the_same_lossless_command_channel_and_immediate_ui_row():
    assert 'void TryAddGoalsToLiveFleet()' in SOURCE
    block = SOURCE[SOURCE.index('void TryAddGoalsToLiveFleet()'):]
    block = block[:block.index('\n    void ', 10)]
    assert 'Cmd1("add_goal", adds)' in block
    assert 'SendTrackedCommand(patch, out commandPath)' in block
    assert 'patch["ack"] = ackPath' in block
    assert 'WatchLiveAddHandoff(commandPath, ackPath)' in block
    assert 'NoteSubmitted(goals)' in block
    assert '_goalInput.Text = ""' in block


def test_steer_remains_a_per_worker_action():
    assert 'bool TrySteerSend(string name, string text, out string failReason)' in SOURCE
    assert 'RequestSteer(name, t)' in SOURCE
