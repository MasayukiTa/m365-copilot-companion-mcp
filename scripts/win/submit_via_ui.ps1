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

if (-not ('Win32.Wnd' -as [type])) {
    Add-Type -Namespace Win32 -Name Wnd -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SetForegroundWindow(System.IntPtr hWnd);
'@ -PassThru | Out-Null
}
if (-not ('Win32.KeyInput' -as [type])) {
    Add-Type -Namespace Win32 -Name KeyInput -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, System.UIntPtr dwExtraInfo);
'@ -PassThru | Out-Null
}
function Get-Cockpit {
    # PICK THE WINDOW THAT HAS THE COMPOSER, NOT A WINDOW THAT BELONGS TO THE PROCESS.
    #
    # This used to match on ClassName "HwndWrapper[FleetCockpit.exe;;" -- but the real class
    # name ends with a per-process guid, and PropertyCondition compares for equality, not by
    # prefix. So the first condition never matched anything and every call fell through to
    # the fallback, which returns the first TOP-LEVEL WINDOW OF THE PROCESS. In WPF a popup,
    # a tooltip and a ComboBox dropdown are each a top-level window, so that fallback can
    # hand back a window with no controls in it at all.
    #
    # Measured 2026-08-29/30: from 23:03 to 23:58 every submit died with "no writable text
    # field found in the cockpit" while the cockpit was running, responding, on screen, and
    # holding an enabled non-readonly goalInput 864 pixels wide. Twenty-four attempts across
    # eight driver launches failed against a window that was never the one being looked for,
    # and the run sat unfinished for six and three quarter hours.
    #
    # The composer is what the caller needs, so the composer is the criterion.
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $idCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'goalInput')
    $restored = $false
    while ((Get-Date) -lt $deadline) {
        $proc = Get-Process -Name FleetCockpit -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($proc) {
            $root = [System.Windows.Automation.AutomationElement]::RootElement
            $byPid = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)
            $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $byPid)
            for ($i = 0; $i -lt $wins.Count; $i++) {
                $w = $wins.Item($i)
                if ($w.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $idCond)) {
                    if ($i -gt 0) { Write-Output ("cockpit: composer was in window {0} of {1}" -f ($i + 1), $wins.Count) }
                    return $w
                }
            }
            # NO WINDOW HAS THE COMPOSER. A minimized WPF window drops its content out of the
            # automation tree, so restore once before concluding anything, rather than
            # retrying a search that cannot succeed.
            if (-not $restored -and $proc.MainWindowHandle -ne [IntPtr]::Zero) {
                $restored = $true
                Write-Output "cockpit: no window exposes goalInput; restoring the main window"
                [Win32.Wnd]::ShowWindow($proc.MainWindowHandle, 9) | Out-Null      # SW_RESTORE
                [Win32.Wnd]::SetForegroundWindow($proc.MainWindowHandle) | Out-Null
                Start-Sleep -Milliseconds 900
                continue
            }
        }
        Start-Sleep -Milliseconds 400
    }
    throw "no cockpit window exposes the goal composer; is FleetCockpit running and not minimized?"
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

# WHICH BOX IS THE GOAL BOX. By AutomationId, which the cockpit now sets. Before it did,
# the only distinguishing property was WIDTH -- 1008 pixels against the history search
# box's 160 -- and that holds while a run is idle and breaks the moment a run adds steer
# boxes to the worker cards. The width guard refused rather than guessing, which is right,
# and also meant no steer could be sent during a run at all. An id is the fix the guard's
# own message asked for.
$idCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'goalInput')
$target = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $idCond)
if ($target) {
    Write-Output 'goal box: found by AutomationId'
} else {
    # FALLBACK for a cockpit built before the id existed. Same guard as before: refuse
    # rather than guess when the widths do not separate cleanly.
    $best = $null; $bestW = 0; $secondW = 0
    for ($i = 0; $i -lt $edits.Count; $i++) {
        $e = $edits.Item($i)
        if (-not $e.Current.IsEnabled) { continue }
        $vp = $null
        if (-not $e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) { continue }
        if ($vp.Current.IsReadOnly) { continue }
        $w = $e.Current.BoundingRectangle.Width
        if ($w -gt $bestW) { $secondW = $bestW; $best = $e; $bestW = $w }
        elseif ($w -gt $secondW) { $secondW = $w }
    }
    if (-not $best) { throw 'no writable text field found in the cockpit' }
    if ($secondW -gt 0 -and $bestW -lt ($secondW * 2)) {
        throw ('cannot tell the goal box from the other field: widths {0:N0} and {1:N0}. ' +
               'Rebuild the cockpit so the box carries its AutomationId.' -f $bestW, $secondW)
    }
    $target = $best
    Write-Output ('goal box: by width {0:N0} (next widest {1:N0}) -- no AutomationId' -f $bestW, $secondW)
}

# CTRL+ENTER WITHOUT SendKeys.
#
# SendKeys.SendWait drives a journal hook by default, and a journal hook needs the system to
# service it within a timeout. Under load it does not: on 2026-08-29, with eight workers and
# seventeen orphaned test processes on the box, three consecutive batches died here with
#   "1" の引数を指定して "SendWait" を呼び出し中に例外が発生
# and none of them was ever submitted. The driver above logged it and waited an hour for each
# of the runs that had therefore never started -- three hours, and a report claiming forty
# predictions for a slice where twenty-two instances had been sent nowhere.
#
# keybd_event goes through SendInput, which has no hook and no timeout, so a busy machine
# delays the keystroke instead of failing it. Same keys, same window, no journal.
function Send-CtrlEnter {
    $VK_CONTROL = 0x11; $VK_RETURN = 0x0D; $KEYEVENTF_KEYUP = 0x0002
    [Win32.KeyInput]::keybd_event($VK_CONTROL, 0, 0, [System.UIntPtr]::Zero)
    Start-Sleep -Milliseconds 40
    [Win32.KeyInput]::keybd_event($VK_RETURN, 0, 0, [System.UIntPtr]::Zero)
    Start-Sleep -Milliseconds 40
    [Win32.KeyInput]::keybd_event($VK_RETURN, 0, $KEYEVENTF_KEYUP, [System.UIntPtr]::Zero)
    [Win32.KeyInput]::keybd_event($VK_CONTROL, 0, $KEYEVENTF_KEYUP, [System.UIntPtr]::Zero)
}

function Submit([string]$text) {
    # CTRL+ENTER, NOT ENTER. The composer sets AcceptsReturn, so a plain Enter inserts a
    # newline and nothing is submitted -- which is exactly what happened the first time
    # this ran: the goal went into the box, the box grew a line, and no run started while
    # the script reported "submitted". FleetCockpit.cs:3485 is the authority.
    if (-not (Set-Text $target $text)) { throw "the field refused a value" }
    Start-Sleep -Milliseconds 250

    # THE KEYSTROKE GOES WHEREVER KEYBOARD FOCUS IS, so the window has to be in front
    # before the composer can hold focus at all. UIA's SetFocus throws outright on an
    # element in a background or minimized window -- measured 2026-08-30 06:52, where the
    # composer was found, filled, and then
    #     "0" の引数を指定して "SetFocus" を呼び出し中に例外が発生
    # ended the batch. Bring the window forward first, and treat a still-failing SetFocus
    # as non-fatal: a foreground window with one text box already routes the keys.
    $cp = Get-Process -Name FleetCockpit -EA SilentlyContinue | Select-Object -First 1
    if ($cp -and $cp.MainWindowHandle -ne [IntPtr]::Zero) {
        [Win32.Wnd]::ShowWindow($cp.MainWindowHandle, 9) | Out-Null       # SW_RESTORE
        [Win32.Wnd]::SetForegroundWindow($cp.MainWindowHandle) | Out-Null
        Start-Sleep -Milliseconds 500
    }
    $focused = $false
    for ($f = 1; $f -le 3 -and -not $focused; $f++) {
        try { $target.SetFocus(); $focused = $true }
        catch { Start-Sleep -Milliseconds 400 }
    }
    if (-not $focused) { Write-Output "note: SetFocus refused; relying on the foreground window" }
    Start-Sleep -Milliseconds 200
    Send-CtrlEnter
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
