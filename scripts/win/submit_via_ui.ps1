# Put a goal into the cockpit the way a person does, and read back what the cockpit shows.
#
# WHY THROUGH THE UI AND NOT THROUGH THE API. A run driven straight into the fleet proves the
# fleet works. It does not prove the cockpit hands it the same thing -- and the gap between
# those two has bitten here: the back end was correct while the surface was full of errors, and
# "the tests pass" was true of a path nobody uses.
#
# ONE GOAL PER CALL, DELIBERATELY. The cockpit splits its input on newlines, so a multi-line
# paste becomes several goals. That happened: five goals appeared where one was meant, and the
# run had to be stopped and resubmitted. If several goals are wanted, call this several times.
#
#   powershell -NoProfile -File scripts/win/submit_via_ui.ps1 -Goal "..." [-Command "/fanout on"]
#   powershell -NoProfile -File scripts/win/submit_via_ui.ps1 -ReadOnly

[CmdletBinding()]
param(
    [string]$Goal = "",
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

if ($Command) { Submit $Command }
if ($Goal) {
    if ($Goal -match "`n") { throw "the cockpit splits on newlines; submit one goal per call" }
    Submit $Goal
}
