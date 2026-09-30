# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUP = (ROOT / 'scripts' / 'supervisor.ps1').read_text(encoding='utf-8-sig', errors='replace')
UI = (ROOT / 'ui' / 'FleetCockpit.cs').read_text(encoding='utf-8-sig', errors='replace')


def test_supervisor_publishes_and_clears_planned_server_transition():
    assert 'server_transition.json' in SUP
    assert 'Write-ServerTransition' in SUP
    assert 'Clear-ServerTransition' in SUP
    stale = SUP[SUP.index('function Invoke-StaleServerCycle'):SUP.index('function Test-PortListening')]
    assert 'Write-ServerTransition $script:ServerPlannedEndReason' in stale
    loop = SUP[SUP.index('if (Test-ServerUp) {'):SUP.index('# A HOST THIS SUPERVISOR LAUNCHED HAS EXITED')]
    assert 'Clear-ServerTransition' in loop


def test_cockpit_treats_fresh_planned_restart_as_yellow_not_red():
    assert 'ReadPlannedServerTransition' in UI
    assert 'SERVER_TRANSITION_HARD_MAX_AGE_S' in UI
    poll = UI[UI.index('void PollHealthOnce()'):UI.index('// 2) Edge:')]
    assert 'plannedRestart' in poll
    assert 'HealthState.Yellow' in poll
    assert 'HealthState.Red' in poll
    assert 'plannedRestart != null' in poll


def test_transition_reader_is_bounded_and_fail_closed():
    i = UI.index('PlannedServerTransition ReadPlannedServerTransition()')
    block = UI[i:i+4200]
    assert 'server_transition.json' in block
    assert 'SERVER_TRANSITION_HARD_MAX_AGE_S' in block
    assert 'return null' in block
    assert 'planned_restart' in block


def test_transition_path_is_initialized_from_root_before_any_restart_can_publish():
    root_i = SUP.index('$Root = Split-Path -Parent $PSScriptRoot')
    path_i = SUP.index('$ServerTransitionPath =')
    write_i = SUP.index('function Write-ServerTransition')
    late_fleet_i = SUP.index('$FleetDir = Join-Path $Root ".fleet"')
    assert root_i < path_i < write_i
    assert path_i < late_fleet_i, "transition path must not depend on FleetDir initialized ~1200 lines later"
    line = SUP[path_i:SUP.index('\n', path_i)]
    assert '$Root' in line and '".fleet"' in line
    assert '$FleetDir' not in line
