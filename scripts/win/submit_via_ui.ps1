# Put a goal into the cockpit the way a person does, and read back what the cockpit shows.
#
# WHY THROUGH THE UI AND NOT THROUGH THE API. A run driven straight into the fleet proves the
# fleet works. It does not prove the cockpit hands it the same thing -- and the gap between
# those two has bitten here: the back end was correct while the surface was full of errors, and
# "the tests pass" was true of a path nobody uses.
#
# ONE LINE PER GOAL, WHICH IS THE COCKPIT'S OWN RULE. It splits its input on newlines, and its
# footer says so. That once turned one intended goal into five, because the goal had newlines
# in it; the same rule is how several goals are started together, which is the only way to
# start several -- see below.
#
# CTRL+ENTER STEERS WHILE A RUN IS ACTIVE (FleetCockpit.cs:3488), it does not add a goal. So a
# second submission during a run does not do what it looks like it does, and this refuses
# rather than quietly steering something the caller did not mean to touch.
#
#   powershell -NoProfile -File scripts/win/submit_via_ui.ps1 -Goal "..." [-Command "/fanout on"]
#   powershell -NoProfile -File scripts/win/submit_via_ui.ps1 -ReadOnly

[CmdletBinding()]
param(
    [string[]]$Goal = @(),
    [string]$GoalFile = "",
    [switch]$Steer,
    [string]$Command = "",
    [switch]$ReadOnly,
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms

function Get-Cockpit {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        $cond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ClassNameProperty, "HwndWrapper[FleetCockpit.exe;;")
        $win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
        if ($win) { return $win }
        # The class name carries a per-process guid, so fall back to matching by process id.
        $proc = Get-Process -Name FleetCockpit -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($proc) {
            $byPid = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)
            $win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $byPid)
            if ($win) { return $win }
        }
        Start-Sleep -Milliseconds 400
    }
    throw "the cockpit window was not found; is FleetCockpit running?"
}

function Get-Edits($window) {
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit)
    return $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
}

function Set-Text($element, [string]$text) {
    # ValuePattern where the control offers it: it replaces the whole value at once, so a
    # half-typed goal can never be submitted by a stray Enter.
    $vp = $null
    if ($element.TryGetCurrentPattern(
            [System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
        $vp.SetValue($text)
        return $true
    }
    return $false
}

$win = Get-Cockpit
$name = $win.Current.Name
Write-Output ("cockpit: {0}" -f $name)

$edits = Get-Edits $win
Write-Output ("editable fields: {0}" -f $edits.Count)
for ($i = 0; $i -lt $edits.Count; $i++) {
    $e = $edits.Item($i)
    $val = ""
    $vp = $null
    if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
        $val = $vp.Current.Value
    }
    Write-Output ("  [{0}] name='{1}' id='{2}' enabled={3} value='{4}'" -f `
        $i, $e.Current.Name, $e.Current.AutomationId, $e.Current.IsEnabled,
        ($val -replace "`r?`n", " ").Substring(0, [Math]::Min(60, $val.Length)))
}

# ONE GOAL PER LINE OF A FILE, WHICH IS THE ONLY RELIABLE WAY TO PASS SEVERAL.
#
# `powershell -File script.ps1 -Goal "a","b"` does NOT build an array: -File passes
# arguments as literal strings without evaluating them, so that arrives as the single
# string "a,b". It did: four questions went in as one goal of 289 characters joined by
# commas, the script reported success, the cockpit accepted it and the fleet fanned the
# nonsense out into six subtasks. Nothing detected it, because everything worked.
if ($GoalFile) {
    if (-not (Test-Path $GoalFile)) { throw "no such goal file: $GoalFile" }
    $Goal = @(Get-Content -LiteralPath $GoalFile -Encoding UTF8 |
              Where-Object { $_.Trim().Length -gt 0 -and -not $_.TrimStart().StartsWith("#") })
}

# SAY WHAT IS ABOUT TO GO IN, PER GOAL. A count and a prefix each is enough to see a
# mangled argument before it becomes a run: one goal where four were meant is obvious on
# this line and invisible everywhere else.
if ($Goal.Count -gt 0) {
    Write-Output ("about to submit {0} goal(s):" -f $Goal.Count)
    for ($i = 0; $i -lt $Goal.Count; $i++) {
        $g = $Goal[$i]
        Write-Output ("  [{0}] {1} chars: {2}" -f $i, $g.Length,
                      $g.Substring(0, [Math]::Min(56, $g.Length)))
    }
}

# READONLY IS A DRY RUN, not just a field dump: it prints exactly what would go in.
if ($ReadOnly) { exit 0 }

# WHICH BOX IS THE GOAL BOX. The cockpit gives neither field a name or an automation id, so
# there is nothing to match on but shape -- and the two shapes are not close: the goal
# composer spans the window, the history search box is 160 pixels wide. Picking the widest
# is therefore reliable HERE, and the check below makes it fail loudly rather than quietly
# if that ever stops being true. A silently-wrong pick would type a goal into the search box
# and report success.
$best = $null; $bestW = 0; $secondW = 0
for ($i = 0; $i -lt $edits.Count; $i++) {
    $e = $edits.Item($i)
    if (-not $e.Current.IsEnabled) { continue }
    $vp = $null
    if (-not $e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) { continue }
    if ($vp.Current.IsReadOnly) { continue }
    $w = $e.Current.BoundingRectangle.Width
    Write-Output ('  candidate [{0}] width={1:N0} height={2:N0}' -f $i, $w, $e.Current.BoundingRectangle.Height)
    if ($w -gt $bestW) { $secondW = $bestW; $best = $e; $bestW = $w }
    elseif ($w -gt $secondW) { $secondW = $w }
}
if (-not $best) { throw 'no writable text field found in the cockpit' }
if ($secondW -gt 0 -and $bestW -lt ($secondW * 2)) {
    throw ('cannot tell the goal box from the other field: widths {0:N0} and {1:N0}. ' +
           'Give the boxes AutomationIds rather than letting this guess.' -f $bestW, $secondW)
}
$target = $best
Write-Output ('goal box: width {0:N0} (next widest {1:N0})' -f $bestW, $secondW)

function Submit([string]$text) {
    # CTRL+ENTER, NOT ENTER. The composer sets AcceptsReturn, so a plain Enter inserts a
    # newline and nothing is submitted -- which is exactly what happened the first time
    # this ran: the goal went into the box, the box grew a line, and no run started while
    # the script reported "submitted". FleetCockpit.cs:3485 is the authority.
    if (-not (Set-Text $target $text)) { throw "the field refused a value" }
    Start-Sleep -Milliseconds 250
    $target.SetFocus()
    Start-Sleep -Milliseconds 200
    [System.Windows.Forms.SendKeys]::SendWait("^{ENTER}")
    Start-Sleep -Milliseconds 900
    # AND VERIFY, because a submit that silently did nothing is the failure mode this
    # whole script exists to catch. An emptied box is the cockpit acknowledging it.
    $after = ""
    $vp2 = $null
    if ($target.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp2)) {
        $after = $vp2.Current.Value
    }
    if ($after.Trim().Length -gt 0) {
        throw ("the composer still holds text after Ctrl+Enter; nothing was submitted: " +
               $after.Substring(0, [Math]::Min(60, $after.Length)))
    }
    Write-Output ("submitted: {0}" -f ($text.Substring(0, [Math]::Min(70, $text.Length))))
}

# IS A RUN ALREADY GOING? The cockpit does not say so through automation, so ask the record
# the fleet keeps. Getting this wrong steers a running goal with the text of a new one.
$running = $false
try {
    $statusPath = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) ".fleet/status.json"
    if (Test-Path $statusPath) {
        $st = Get-Content $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $running = [bool]$st.running
    }
} catch { }
Write-Output ("run in flight: {0}" -f $running)

if ($Command) { Submit $Command }

if ($Goal.Count -gt 0) {
    if ($running -and -not $Steer) {
        throw ("a run is in flight, and Ctrl+Enter steers rather than starts while one is. " +
               "Wait for it, or pass -Steer if steering is what was meant.")
    }
    if ($Steer -and $Goal.Count -gt 1) { throw "steer one message at a time" }
    foreach ($g in $Goal) {
        if ($g -match "`n") { throw "a goal may not contain a newline; the cockpit splits on them" }
    }
    # SEVERAL GOALS GO IN TOGETHER, one per line, and start as one fleet. Submitting them one
    # at a time cannot work: the first starts a run, and every later one steers it.
    Submit ($Goal -join "`n")
}
