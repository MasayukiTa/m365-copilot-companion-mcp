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
