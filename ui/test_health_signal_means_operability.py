# -*- coding: utf-8 -*-
"""Traffic-light colors report current operability, not maintenance metadata.

Details may say that a process is stale or an auxiliary bridge probe is degraded, but if the
path the fleet is actually using is demonstrably working, the signal must be green. A warning
color is reserved for impaired/uncertain operation that changes what the operator should do.
"""
from pathlib import Path
import re

SRC = Path(__file__).with_name('FleetCockpit.cs').read_text(encoding='utf-8-sig')


def _between(start, end):
    i = SRC.index(start)
    j = SRC.index(end, i)
    return SRC[i:j]


def test_recent_stale_is_green_but_stale_past_the_self_heal_window_can_warn():
    block = SRC[SRC.index('string codeState = HealthField(srvBody, "server_code")'):]
    block = block[:block.index('// 1) Tunnel:')]

    # Staleness itself is deploy freshness, not service failure. The actionable case is that it
    # stayed stale longer than the supervisor should need to cycle the server.
    prolonged = 'codeState == "stale" && StaleLongEnoughToMatter(srvBody)'
    assert prolonged in block
    i = block.index(prolonged)
    j = block.index('else if (codeState == "stale")', i)
    assert 'SetDot(0, HealthState.Yellow' in block[i:j]

    # A normal just-changed server remains green while the automatic cycle has time to run.
    k = block.index('else', j + 1)
    recent = block[j:k]
    assert 'SetDot(0, HealthState.Green' in recent
    assert 'SetDot(0, HealthState.Yellow' not in recent
    assert 'hs_srv_detail_stale_recent' in recent


def test_real_fleet_tool_success_dominates_bridge_probe_color():
    block = _between('void PollToolProbeOnce(DateTime now)', 'string AgeMinutesText(')
    fleet = block.index('if (fleetWorking)')
    next_branch = block.index('else if (ok && fleetFailing)', fleet)
    # The stale limit follows the configured probe interval (never under 20 min); it used to be a
    # literal 20.0.
    stale = block.index('ageMin >= ToolProbeStaleAfterMin()', next_branch)
    assert fleet < next_branch < stale, 'real fleet calls must be considered before stale bridge-probe metadata'
    branch = block[fleet:next_branch]
    assert 'HealthState.Green' in branch
    assert 'hs_tool_detail_bridge_only_down' in branch, 'degraded bridge remains visible in detail'
    assert 'HealthState.Yellow' not in branch


def test_fleet_tool_failure_can_still_warn_even_if_bridge_probe_is_green():
    block = _between('void PollToolProbeOnce(DateTime now)', 'string AgeMinutesText(')
    assert 'ok && fleetFailing' in block
    i = block.index('ok && fleetFailing')
    assert 'HealthState.Yellow' in block[i:i+300]
