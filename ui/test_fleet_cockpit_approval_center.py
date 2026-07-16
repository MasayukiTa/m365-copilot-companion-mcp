from pathlib import Path


SOURCE = Path(__file__).with_name("FleetCockpit.cs").read_text(encoding="utf-8")


def test_run_mode_is_not_mislabeled_as_operation_approval():
    assert 'if (k == "run_mode") return ja ? "実行方式" : "Run mode";' in SOURCE
    assert '_approvalLbl.Text = T("run_mode")' in SOURCE
    assert '"承認センター、未処理 "' in SOURCE


def test_approval_center_reads_durable_gates_even_when_idle():
    assert 'Directory.GetFiles(dir, "gate_*.json")' in SOURCE
    assert 'List<Dictionary<string, object>> gates = PendingGates(root);' in SOURCE
    assert 'UpdateGateBanner(root);' in SOURCE
    assert 'if (idle)' in SOURCE


def test_high_impact_approval_requires_a_second_confirmation():
    assert "GateNeedsSecondConfirmation" in SOURCE
    assert "MessageBoxButton.YesNo" in SOURCE
    assert "MessageBoxResult.No" in SOURCE
    assert "Approve all" not in SOURCE


def test_approval_center_has_accessibility_and_audit_history():
    assert 'AutomationProperties.SetName(_approvalCenterBtn' in SOURCE
    assert 'AutomationProperties.SetName(approve' in SOURCE
    assert 'gd["answered_at"] = NowUnix();' in SOURCE
    assert '"最近の判断"' in SOURCE


def test_actionable_notification_prompt_can_answer_without_opening_chat():
    assert 'args[0].Equals("--approval-gate"' in SOURCE
    assert "class ApprovalPromptWindow : Window" in SOURCE
    assert '_approve.Click += delegate { Answer("approved"); }' in SOURCE
    assert '_deny.Click += delegate { Answer("denied"); }' in SOURCE
    assert "M365CompanionApprovalPrompt" in SOURCE


def test_confirmation_auto_and_bypass_are_visible_and_persistent():
    assert '"default")' in SOURCE
    assert '"auto")' in SOURCE
    assert '"bypass")' in SOURCE
    assert "job_approval_mode=" in SOURCE
    assert "ApprovalPromptWindow.SavePolicy(next)" in SOURCE


def test_approval_prompt_uses_the_shared_theme_and_live_preferences():
    assert "Theme.Bg(_dark)" in SOURCE
    assert "Theme.Surface(_dark)" in SOURCE
    assert "Theme.Accent(_dark)" in SOURCE
    assert "Theme.UiFont" in SOURCE
    assert "UiPreferencesChanged()" in SOURCE


def test_unfinished_run_banner_has_persistent_close_button():
    assert 'IconButton("close", 14' in SOURCE
    assert "DismissResumeState(capturedSignature)" in SOURCE
    assert "cockpit_resume_dismissed.json" in SOURCE
    assert "ResumeStateSignature()" in SOURCE
    assert "ResumeStateDismissed(resumeSig)" in SOURCE
    assert 'rd["signature"] = resumeSig' in SOURCE


def test_health_repairs_have_immediate_busy_feedback():
    assert "HealthState.Checking" in SOURCE
    assert "BuildSpinner(10)" in SOURCE
    assert 'BuildFixPillContent(_fixRunning)' in SOURCE
    assert 'T(busy ? "hs_fixing_button" : "hs_fix")' in SOURCE
    assert "_fixTargetMask" in SOURCE
    assert "_healthWake.Set()" in SOURCE
    assert 'kind == "checking" || kind == "starting"' in SOURCE
    assert 'Path.GetDirectoryName(Path.GetFullPath(_statusPath))' in SOURCE
