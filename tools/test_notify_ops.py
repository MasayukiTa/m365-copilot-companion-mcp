from tools.notify_ops import notify_approval_gate


def test_approval_notification_is_inert_under_pytest(tmp_path):
    gate = tmp_path / "gate_example.json"
    gate.write_text("{}", encoding="utf-8")
    result = notify_approval_gate("Approval", "Review this", gate)
    assert "suppressed" in result
