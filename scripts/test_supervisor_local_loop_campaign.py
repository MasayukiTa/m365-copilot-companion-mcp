from pathlib import Path

SRC = Path(__file__).with_name('supervisor.ps1').read_text(encoding='utf-8-sig', errors='replace')


def test_supervisor_has_local_loop_campaign_drain():
    assert '$LocalLoopCampaignPath = Join-Path $FleetDir "local_loop_campaign.json"' in SRC
    assert 'function Invoke-LocalLoopCampaignDrain' in SRC
    i = SRC.index('function Invoke-LocalLoopCampaignDrain')
    block = SRC[i:i+5000]
    assert '--drain-campaign' in block
    assert 'relay.local_loop_controller' in block
    assert 'Push-Location $Root' in block
    assert '$LASTEXITCODE' in block


def test_campaign_drain_runs_only_when_manifest_is_present_and_leaves_feature_flag_to_python():
    i = SRC.index('function Invoke-LocalLoopCampaignDrain')
    block = SRC[i:i+5000]
    assert 'Test-Path $LocalLoopCampaignPath' in block
    assert 'Test-ExecutionProfilesEnabled' not in block
    assert 'MCP_EXECUTION_PROFILES' not in block


def test_campaign_drain_runs_after_autoresume_at_startup_and_each_tick():
    startup = SRC[SRC.index('# Checked once, here'):SRC.index('$serverMiss = 0')]
    assert startup.index('Invoke-LocalLoopAutoResume') < startup.index('Invoke-LocalLoopCampaignDrain')
    loop = SRC[SRC.index('while ($true) {'):]
    assert loop.index('Invoke-LocalLoopAutoResume') < loop.index('Invoke-LocalLoopCampaignDrain')


def test_campaign_drain_logs_real_launches_but_not_empty_passes():
    i = SRC.index('function Invoke-LocalLoopCampaignDrain')
    block = SRC[i:i+5000]
    assert 'ConvertFrom-Json' in block
    assert '.launched' in block
    assert 'LOCAL_LOOP campaign launched' in block
