from pathlib import Path

SOURCE = Path(__file__).with_name("FleetCockpit.cs").read_text(encoding="utf-8-sig")


def _method(start, end):
    i = SOURCE.index(start)
    return SOURCE[i:SOURCE.index(end, i)]


def test_local_loop_steer_routes_before_classic_fleet_liveness_gate():
    method = _method(
        "bool TrySteerSend(string name, string text, out string failReason)",
        "// FIX A:",
    )
    assert "Dictionary<string, object> worker = WorkerByName(name);" in method
    assert "if (IsLocalLoopWorker(worker))" in method
    assert "return QueueLocalLoopSteer(worker, t, out failReason);" in method
    assert "if (!RunIsLive())" in method
    assert "RequestSteer(name, t);" in method
    assert method.index("QueueLocalLoopSteer") < method.index("RunIsLive()")
    assert method.index("RunIsLive()") < method.index("RequestSteer(name, t)")


def test_durable_steer_uses_worker_sqlite_authority_and_hidden_one_shot_controller():
    method = _method(
        "bool QueueLocalLoopSteer(Dictionary<string, object> w, string text, out string failReason)",
        "void WatchLocalLoopSteer(",
    )
    for token in (
        'S(w, "local_job_db")',
        "durable_steer_",
        "--operator-steer-job-id",
        "--operator-steer-file",
        "--state-dir",
        "--db",
        "relay.local_loop_controller",
        "psi.UseShellExecute = false;",
        "psi.CreateNoWindow = true;",
        'psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";',
    ):
        assert token in method
    assert "FleetCommands.Write" not in method
    assert "RequestSteer" not in method


def test_failed_durable_steer_restores_the_users_draft():
    watcher = _method("void WatchLocalLoopSteer(", "void RequestSteer(")
    assert "_steerDraft[jobId] = text;" in watcher
    assert "_steerBoxRef.TryGetValue(jobId, out box)" in watcher
    assert "box.Text = text;" in watcher
    assert "input restored" in watcher.lower()
    assert "proc.ExitCode" in watcher


def test_successful_durable_steer_is_confirmed_only_after_child_exit_zero():
    watcher = _method("void WatchLocalLoopSteer(", "void RequestSteer(")
    assert "proc.HasExited" in watcher
    assert "if (code == 0)" in watcher
    assert "_steerDraft.Remove(jobId);" in watcher
    assert "Operator update saved to the durable task" in watcher


def test_expanded_and_collapsed_steer_rows_share_the_same_draft_and_send_path():
    collapsed = _method("UIElement CollapsedSteerRow(string name)", "// ② steering:")
    expanded = _method("UIElement SteerRow(string name)", '// "続ける" (Continue)')
    for block in (collapsed, expanded):
        assert "TrySteerSend(" in block
        assert "_steerDraft" in block
        assert "_steerBoxRef" in block


def test_durable_steer_does_not_require_a_live_browser_controller():
    method = _method(
        "bool TrySteerSend(string name, string text, out string failReason)",
        "// FIX A:",
    )
    # LOCAL_LOOP is routed before the Fleet-only RunIsLive refusal so WAITING_RUNTIME /
    # browser-rotated durable jobs can still accept an operator correction into SQLite.
    assert method.index("IsLocalLoopWorker(worker)") < method.index("if (!RunIsLive())")
