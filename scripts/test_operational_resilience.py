from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bridge_keepalive_uses_a_nonblocking_single_supervisor_mutex():
    source = (ROOT / "scripts" / "start_bridge.ps1").read_text(encoding="utf-8")

    acquire = source.index("$keepaliveMutex.WaitOne(0)")
    first_edge_start = source.index("Ensure-Edge -Hard:$HardReset")
    assert "System.Threading.Mutex" in source
    assert "System.Threading.AbandonedMutexException" in source
    assert acquire < first_edge_start


def test_tunnel_ownership_timeout_is_indeterminate_and_never_auto_repaired():
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    repair = (ROOT / "scripts" / "repair.ps1").read_text(encoding="utf-8")

    assert "Check-TriState \"tunnel_owned\"" in doctor
    assert "$attempt -le 3" in doctor
    assert "Invoke-DevTunnelBounded @('list') 10" in doctor
    assert "return $null" in doctor
    assert "indeterminate = $indeterminate" in doctor
    assert repair.count("-not $_.indeterminate") >= 3
