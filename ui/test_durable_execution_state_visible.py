# -*- coding: utf-8 -*-
"""The cockpit must surface durable task progress, not only conversation turns."""
from pathlib import Path

SOURCE = Path(__file__).with_name("FleetCockpit.cs").read_text(encoding="utf-8")


def test_collapsed_card_reads_the_durable_execution_contract():
    for token in (
        'Obj(w, "execution")',
        '"current_step"',
        '"current_step_index"',
        '"completed_count"',
        '"last_progress_at"',
        '"artifacts"',
    ):
        assert token in SOURCE
    assert 'Text = stepPrefix + OneLine(currentStep)' in SOURCE
    assert 'stepLine.Children.Add(MakeIcon("play_arrow", 13, Fg));' in SOURCE
    assert 'meta.Append(" · ").Append(_lang == 0 ? "完了 " : "done ").Append(completed);' in SOURCE


def test_expanded_overview_shows_steps_waiting_and_artifacts():
    assert 'UIElement ExecutionOverview(Dictionary<string, object> w)' in SOURCE
    for token in ('"completed_steps"', '"waiting_reason"', '"next_step"', '"Artifacts"'):
        assert token in SOURCE
    assert 'UIElement execView = ExecutionOverview(w);' in SOURCE


def test_execution_progress_participates_in_row_diffing():
    assert 'sb.Append("|exec:")' in SOURCE
    assert 'StableShortHash(S(ex, "last_progress"))' in SOURCE
    assert 'S(ex, "last_progress_at")' in SOURCE


def test_durable_runtime_is_an_explicit_opt_in_launch_path():
    assert 'string _runtimeMode = "fleet"' in SOURCE
    assert 'SaveKey("runtime", _runtimeMode)' in SOURCE
    assert 'if (_runtimeMode == "durable")' in SOURCE
    assert 'bool SpawnDurableTask(string goal, string submittedText)' in SOURCE
    assert '-m relay.local_loop_controller --goal-file' in SOURCE
    assert 'psi.CreateNoWindow = true;' in SOURCE
    assert 'MCP_EXECUTION_PROFILES=1' in SOURCE


def test_runtime_slash_command_is_discoverable_and_persistent():
    assert 'g0.StartsWith("/runtime "' in SOURCE
    assert 'new[]{"/runtime"' in SOURCE
    assert 'ln.StartsWith("runtime=")' in SOURCE
    assert 'cmdName == "/runtime"' in SOURCE


def test_initial_durable_start_keeps_input_until_status_accepts_the_job():
    start = SOURCE[SOURCE.index("void StartFleet()"):SOURCE.index("bool ActiveRunIsLocalLoop()") ]
    durable = start[start.index('if (_runtimeMode == "durable")'):]
    assert '_durableStartPending' in SOURCE
    assert 'SpawnDurableTask(goals[0], durableSubmittedText)' in durable
    assert '_goalInput.Text = "";' not in durable
    assert 'Durable task started.' not in durable

    spawn = SOURCE[SOURCE.index("bool SpawnDurableTask(string goal, string submittedText)"):SOURCE.index("bool SpawnFleet(List<string> goals") ]
    assert 'proc = System.Diagnostics.Process.Start(psi);' in spawn
    assert 'if (proc == null)' in spawn
    assert 'WatchDurableStart(proc, submitBaseline, goalFile, submittedText ?? "", goal);' in spawn
    assert '_durableStartPending = true;' in spawn

    watch = SOURCE[SOURCE.index("void WatchDurableStart("):SOURCE.index("bool SpawnFleet(List<string> goals") ]
    assert 'ActiveRunIsLocalLoop(root)' in watch
    assert 'FreshRunContainsGoals(root, new List<string> { goal })' in watch
    assert 'StartedOf(root)' in watch and 'baseline.Started' in watch
    assert 'if (_goalInput.Text == submittedText) _goalInput.Text = "";' in watch
    assert 'proc.HasExited' in watch
    assert 'input was kept' in watch.lower()


def test_durable_start_pending_blocks_duplicate_launch_clicks():
    start = SOURCE[SOURCE.index("void StartFleet()"):SOURCE.index("bool ActiveRunIsLocalLoop()") ]
    assert 'if (_durableStartPending)' in start


def test_waiting_runtime_has_a_dedicated_same_job_resume_action():
    assert 'bool isLocalRuntimeWait' in SOURCE
    assert 'runtime_resume_allowed' in SOURCE
    assert 'ResumeLocalLoopRuntime(' in SOURCE
    assert 'ResumeLocalLoopRuntime(runtimeWaitWorker)' in SOURCE
    method = SOURCE[SOURCE.index('bool ResumeLocalLoopRuntime('):]
    method = method[:method.index('\n    void ', 20) if '\n    void ' in method[20:] else len(method)]
    assert '-m relay.local_loop_controller' in method
    assert '--job-id' in method
    assert '--resume-runtime' in method
    assert '--state-dir' in method
    assert '--db' in method
    assert 'S(w, "local_job_db")' in method
    assert 'psi.CreateNoWindow = true;' in method
    assert 'RetryGoal' not in method


def test_waiting_runtime_status_has_a_human_label():
    theme = Path(__file__).with_name('Theme.cs').read_text(encoding='utf-8-sig')
    assert '{ "waiting_runtime", "warning" }' in theme
    assert 'case "waiting_runtime"' in theme
    assert 'Runtime paused' in theme


def _method(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


def test_local_loop_control_artifacts_never_use_generic_fleet_retry():
    helper = _method(SOURCE, "static bool IsLocalLoopControlGoal(", "static bool IsOperatorAttention(")
    for prefix in ("execute local_loop job ", "run local_loop job ", "local_loop run ",
                   "local_loop bootstrap ", "local_loop protocol "):
        assert prefix in helper.lower()

    retry = _method(SOURCE, "void RetryGoal(Dictionary<string, object> w)", "int RetryAllShown(")
    assert retry.index('IsLocalLoopControlGoal(goal)') < retry.index('RunIsLive()')

    auto = _method(SOURCE, "void AutoRetryScan(Dictionary<string, object> root)", "Dictionary<string, object> ReadStatus()")
    assert 'IsRetryableWorker(w)' in auto

    bulk = _method(SOURCE, "int RetryAllShown(", "static readonly string[] _retryableOutcomes")
    assert 'IsRetryableWorker(w)' in bulk

    spawn = _method(SOURCE, "bool SpawnFleet(List<string> goals", "static bool FreshRunContainsGoals(")
    assert 'IsLocalLoopControlGoal(SubmittedTasks.GoalTextOf(' in spawn


def test_internal_control_stuck_rows_are_not_operator_attention():
    helper = _method(SOURCE, "static bool IsOperatorAttention(", "static bool IsRetryableWorker(")
    assert '!IsLocalLoopControlGoal(S(w, "goal"))' in helper
    assert '!IsInfraStuck(w)' in helper

    header = SOURCE[SOURCE.index('int cntAttn = 0;'):SOURCE.index('string triple;', SOURCE.index('int cntAttn = 0;'))]
    assert 'IsOperatorAttention(ww2)' in header

    card = _method(SOURCE, "Border Card(Dictionary<string, object> w)", "UIElement BuildCardTabs(")
    assert 'bool isAttention = !closed && IsOperatorAttention(w);' in card
    assert 'if (!IsLocalLoopControlGoal(goal))' in card

    hist = _method(SOURCE, "Border HistoryRow(Dictionary<string, object> e)", "static bool IsAttentionStatus(")
    assert 'bool internalControl = IsLocalLoopControlGoal(S(e, "goal"));' in hist
    assert 'Internal control' in hist
    assert 'if (!internalControl)' in hist


def test_collapsed_card_does_not_repeat_the_full_goal_as_current_step():
    card = _method(SOURCE, "Border Card(Dictionary<string, object> w)", "UIElement BuildCardTabs(")
    assert 'bool currentStepRepeatsGoal = string.Equals(' in card
    assert '!currentStepRepeatsGoal' in card
    # Expanded execution details keep the authoritative raw current_step visible.
    detail = _method(SOURCE, "UIElement ExecutionOverview(Dictionary<string, object> w)", "UIElement TabOverview(")
    assert 'string current = S(execution, "current_step");' in detail
    assert 'head += " · " + current' in detail or 'head += " �E " + current' in detail
