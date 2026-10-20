# Change one cockpit setting the way a person does: open the settings popup, open the combo
# box, pick the entry. The cockpit writes the key itself (SaveKey), so settings.txt is never
# edited from here. -ReadOnly lists what is on screen and changes nothing.
#
#   powershell -NoProfile -File scripts/win/set_setting_via_ui.ps1 -ReadOnly
#   powershell -NoProfile -File scripts/win/set_setting_via_ui.ps1 -Setting fanout_max_depth -Value 2
#   powershell -NoProfile -File scripts/win/set_setting_via_ui.ps1 -Setting fanout_hierarchical_merge -Value on
#   powershell -NoProfile -File scripts/win/set_setting_via_ui.ps1 -Setting fanout_write_scope -Value shadow
#
# Needs a cockpit built with the AutomationIds below (ui/FleetCockpit.cs). ASCII only on purpose.

[CmdletBinding()]
param(
    [string]$Setting = "",
    [string]$Value = "",
    [switch]$ReadOnly,
    [int]$TimeoutSeconds = 20
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

# setting key -> AutomationId of its combo box in the settings popup
$Boxes = @{
    'fanout_max_depth'          = 'fanoutDepthBox'
    'fanout_hierarchical_merge' = 'hierarchicalMergeBox'
    'fanout_write_scope'        = 'writeScopeBox'
    'fanout_load_routing'       = 'loadRoutingBox'
    'agent_board_mode'          = 'agentBoardBox'
    'org_version_record'        = 'orgVersionBox'
    'team_mode'                 = 'teamModeBox'
    'org_proposals'             = 'orgProposalBox'
    'org_shadow_eval'           = 'orgShadowBox'
    'effort_policy'             = 'effortPolicyBox'
    'fanout'                    = 'fanoutBox'
    'tool_transport'            = 'toolRouteBox'
    'consent_auto_approve'      = 'consentApproveBox'
    'resident_tools'            = 'residentToolsBox'
    'merge_conversation'        = 'mergeConversationBox'
    'fleet_auto_resume'         = 'autoResumeBox'
    'supervisor_self_restart'   = 'supervisorCodeBox'
    'tool_probe_idle_min'       = 'toolProbeBox'
}

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]

function Find-ById($root, [string]$id) {
    $c = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
    return $root.FindFirst($TS::Descendants, $c)
}

# Every top-level window of the cockpit process (the popup is a window of its own in WPF).
function Get-ProcessWindows {
    $proc = Get-Process -Name FleetCockpit -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $proc) { throw "FleetCockpit is not running" }
    $byPid = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $proc.Id)
    return ,$AE::RootElement.FindAll($TS::Children, $byPid)
}

function Find-Anywhere([string]$id) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $wins = Get-ProcessWindows
        for ($i = 0; $i -lt $wins.Count; $i++) {
            $e = Find-ById $wins.Item($i) $id
            if ($e) { return $e }
        }
        Start-Sleep -Milliseconds 300
    }
    return $null
}

function Open-Settings {
    $existing = Find-Anywhere 'fanoutDepthBox'
    if ($null -ne $existing) { return }
    $gear = Find-Anywhere 'settingsButton'
    if ($null -eq $gear) { throw "settingsButton not found; rebuild the cockpit (ui\rebuild_ui.ps1)" }
    $gear.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    Start-Sleep -Milliseconds 600
}

function Read-Combo($combo) {
    if ($null -eq $combo) { throw "internal: Read-Combo got no combo box" }
    $sel = $combo.GetCurrentPattern([System.Windows.Automation.SelectionPattern]::Pattern)
    $cur = $sel.Current.GetSelection()
    if ($cur.Count -gt 0) { return $cur[0].Current.Name }
    return ""
}

# Find a combo box; if the popup closed (the cockpit repaints after a save), open it again.
function Get-Combo([string]$id) {
    $c = Find-Anywhere $id
    if ($null -eq $c) {
        Open-Settings
        $c = Find-Anywhere $id
    }
    return $c
}

# What settings.txt says now (read only). $null when the file or the key is not there.
function Read-SettingsFile([string]$key) {
    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $f = Join-Path $root ".config\settings.txt"
    if (-not (Test-Path -LiteralPath $f)) { return $null }
    $hit = $null
    foreach ($ln in [System.IO.File]::ReadAllLines($f)) {
        if ($ln.StartsWith($key + "=")) { $hit = $ln.Substring($key.Length + 1) }
    }
    return $hit
}

function Invoke-Main {
    Open-Settings

    if ($ReadOnly -or -not $Setting) {
        foreach ($k in $Boxes.Keys) {
            $c = Find-Anywhere $Boxes[$k]
            if ($null -eq $c) { Write-Output ("{0}: combo {1} not found" -f $k, $Boxes[$k]); continue }
            Write-Output ("{0}: current={1}" -f $k, (Read-Combo $c))
        }
        return
    }

    if (-not $Boxes.ContainsKey($Setting)) { throw ("unknown setting '{0}'; known: {1}" -f $Setting, ($Boxes.Keys -join ', ')) }
    $combo = Get-Combo $Boxes[$Setting]
    if ($null -eq $combo) { throw ("combo {0} not found in the settings popup" -f $Boxes[$Setting]) }
    if ((Read-Combo $combo) -eq $Value) { Write-Output ("{0} already {1}" -f $Setting, $Value); return }

    $ec = $combo.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
    $ec.Expand()
    Start-Sleep -Milliseconds 400
    $item = $null
    $nameCond = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, $Value)
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline -and $null -eq $item) {
        $item = $combo.FindFirst($TS::Descendants, $nameCond)
        if ($null -eq $item) { Start-Sleep -Milliseconds 200 }
    }
    if ($null -eq $item) { try { $ec.Collapse() } catch {}; throw ("no entry '{0}' in {1}" -f $Value, $Setting) }
    $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
    Start-Sleep -Milliseconds 500
    try { $ec.Collapse() } catch {}

    # Read back. The cockpit's SelectionChanged handler is what saves the key; it may also
    # repaint or close the popup, so the combo can be gone here (that was the null call).
    $fileVal = Read-SettingsFile $Setting
    $after = $null
    $c2 = Get-Combo $Boxes[$Setting]
    if ($null -ne $c2) { $after = Read-Combo $c2 }
    if ($null -ne $fileVal -and $fileVal -ne $Value) {
        throw ("{0}: wanted {1} but settings.txt has {2}" -f $Setting, $Value, $fileVal)
    }
    if ($null -eq $after -and $fileVal -ne $Value) {
        throw ("{0}: could not read the value back (popup gone and no settings.txt line)" -f $Setting)
    }
    if ($null -ne $after -and $after -ne $Value) {
        throw ("{0}: wanted {1} but the combo shows {2}" -f $Setting, $Value, $after)
    }
    Write-Output ("{0} set to {1} through the cockpit (read back)" -f $Setting, $Value)
}

try {
    Invoke-Main
    exit 0
} catch {
    Write-Output ("FAILED: {0}" -f $_.Exception.Message)
    exit 1
}
