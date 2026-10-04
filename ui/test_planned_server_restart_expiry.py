from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUP = (ROOT / 'scripts' / 'supervisor.ps1').read_text(encoding='utf-8-sig', errors='replace')
UI = (ROOT / 'ui' / 'FleetCockpit.cs').read_text(encoding='utf-8-sig', errors='replace')


def test_supervisor_transition_expiry_matches_its_own_restart_budget():
    block = SUP[SUP.index('function Write-ServerTransition'):SUP.index('function Clear-ServerTransition')]
    assert '$transitionBudgetSeconds = $StartupGraceSeconds + ($FailuresBeforeAction * $IntervalSeconds)' in block
    assert 'expires = $now + $transitionBudgetSeconds' in block
    assert 'started = $now' in block


def test_cockpit_prefers_marker_expiry_but_keeps_legacy_compatibility():
    i = UI.index('PlannedServerTransition ReadPlannedServerTransition()')
    block = UI[i:i+5200]
    assert 'expires' in block
    assert 'LEGACY_SERVER_TRANSITION_MAX_AGE_S' in block
    assert 'SERVER_TRANSITION_HARD_MAX_AGE_S' in block
    assert 'effectiveExpiry' in block
    assert 'expires - started' in block


def test_hard_cap_is_safety_only_not_the_operational_restart_budget():
    assert 'const double LEGACY_SERVER_TRANSITION_MAX_AGE_S = 60.0;' in UI
    assert 'const double SERVER_TRANSITION_HARD_MAX_AGE_S = 600.0;' in UI
    assert 'SERVER_TRANSITION_MAX_AGE_S = 240.0' not in UI
