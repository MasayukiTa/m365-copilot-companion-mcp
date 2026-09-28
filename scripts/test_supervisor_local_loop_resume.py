from pathlib import Path

SRC = Path(__file__).with_name('supervisor.ps1').read_text(encoding='utf-8-sig', errors='replace')


def test_supervisor_scans_generic_local_loop_markers_every_cycle():
    assert '$LocalLoopMarkerDir = Join-Path $Root ".fleet\\local_loop_active"' in SRC
    assert 'function Invoke-LocalLoopAutoResume' in SRC
    loop = SRC[SRC.index('while ($true) {'):]
    assert 'Invoke-LocalLoopAutoResume | Out-Null' in loop


def test_generic_local_loop_resume_uses_controller_and_existing_runner_monitor():
    i = SRC.index('function Invoke-LocalLoopAutoResume')
    block = SRC[i:i+9000]
    assert 'relay.local_loop_controller' in block
    assert 'resume_argv' in block
    assert 'Start-Process' in block and '-PassThru' in block
    assert 'Register-AutoResumeRunner' in block
    assert 'retry_after' in block
    assert 'restart_count' in block


def test_local_loop_autoresume_is_default_on_but_has_opt_out():
    assert 'MCP_LOCAL_LOOP_AUTORESUME' in SRC
    assert 'function Test-LocalLoopAutoResumeEnabled' in SRC


def test_local_loop_marker_binds_pid_to_process_birth_in_both_directions():
    i = SRC.index('function Test-LocalLoopMarkerProcessAlive')
    block = SRC[i:i+2500]
    assert '[Math]::Abs($processStarted - $markerStarted) -gt 60' in block
    assert '$process.ProcessName -notlike "python*"' in block


def test_local_loop_restart_backoff_grows_and_is_capped():
    assert 'function Get-LocalLoopRetryDelaySeconds' in SRC
    i = SRC.index('function Get-LocalLoopRetryDelaySeconds')
    block = SRC[i:i+1800]
    assert '[Math]::Pow(2' in block
    assert '[Math]::Min(900' in block
    resume = SRC[SRC.index('function Invoke-LocalLoopAutoResume'):SRC.index('# -- Queue delivery:')]
    assert '$retryDelay = Get-LocalLoopRetryDelaySeconds -RestartCount $restart' in resume
    assert '$nowEpoch + $retryDelay' in resume
    assert '$nowEpoch + 30' not in resume


def test_child_marker_backoff_is_not_cleared_by_supervisor_contract():
    resume = SRC[SRC.index('function Invoke-LocalLoopAutoResume'):SRC.index('# -- Queue delivery:')]
    assert 'restart_count' in resume and 'retry_after' in resume
    assert 'healthy child preserves this deadline' in resume
