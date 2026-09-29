# -*- coding: utf-8 -*-
from pathlib import Path

SUP = Path('scripts/supervisor.ps1').read_text(encoding='utf-8-sig', errors='replace')
UI = Path('ui/FleetCockpit.cs').read_text(encoding='utf-8-sig', errors='replace')


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
    assert 'SERVER_TRANSITION_MAX_AGE_S' in UI
    poll = UI[UI.index('void PollHealthOnce()'):UI.index('// 2) Edge:')]
    assert 'plannedRestart' in poll
    assert 'HealthState.Yellow' in poll
    assert 'HealthState.Red' in poll
    assert 'plannedRestart != null' in poll


def test_transition_reader_is_bounded_and_fail_closed():
    i = UI.index('PlannedServerTransition ReadPlannedServerTransition()')
    block = UI[i:i+4200]
    assert 'server_transition.json' in block
    assert 'SERVER_TRANSITION_MAX_AGE_S' in block
    assert 'return null' in block
    assert 'planned_restart' in block
