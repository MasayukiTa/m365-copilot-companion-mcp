# Does interrupting a running job change the cockpit window's state, and which path does it?
#
# THE REPORT WAS "I TYPED AN INTERRUPT INTO A RUNNING JOB, PRESSED ENTER, AND THE FLEET
# MINIMISED." Reading that as "Enter is broken" throws away most of the ways to reproduce it.
# The event has two halves -- a keystroke, and an interrupt being delivered -- and only one of
# them needs a keyboard. So this sends the same interrupt down three different paths and
# records the window's state around each. If the window changes state on the path that uses no
# keyboard at all, the keystroke was never the cause.
#
#   uia-ctrl-enter    UI Automation puts text in goalInput and sends Ctrl+Enter.
#                     This is what a person does; the cockpit turns it into a steer while a
#                     run is live (see the -Steer path of submit_via_ui.ps1).
#   uia-plain-enter   The same, with plain Enter. The composer sets AcceptsReturn, so this
#                     should insert a newline and do nothing else -- if the window moves here
#                     and not on Ctrl+Enter, the key handling is the suspect.
#   api               A steer written straight into the command file the cockpit itself
#                     writes. NO KEYBOARD, NO FOCUS CHANGE, NO UI AUTOMATION. If the window
#                     state changes on this one, nothing about Enter is involved.
#
# IT WILL NOT GUESS AND IT WILL NOT SEND BY ACCIDENT. A steer reaches a real worker and
# becomes part of somebody's work, so nothing is sent without -Confirm, and without it the
# script prints exactly what it would send and stops.
#
#   powershell -NoProfile -File scripts\win\repro_steer_window_state.ps1
#   powershell -NoProfile -File scripts\win\repro_steer_window_state.ps1 -Confirm -Paths api
#   powershell -NoProfile -File scripts\win\repro_steer_window_state.ps1 -Confirm

[CmdletBinding()]
param(
    [ValidateSet("api", "uia-ctrl-enter", "uia-plain-enter", "all")]
    [string[]]$Paths = @("all"),
    [switch]$Confirm,
    [int]$SettleMs = 1500,
    [string]$Text = "[repro] window-state probe; no action required, please continue."
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

if (-not ('Win32.WndState' -as [type])) {
    Add-Type -Namespace Win32 -Name WndState -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool IsIconic(System.IntPtr hWnd);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool IsZoomed(System.IntPtr hWnd);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool IsWindowVisible(System.IntPtr hWnd);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern System.IntPtr GetForegroundWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool GetWindowRect(System.IntPtr hWnd, out System.Drawing.Rectangle r);
'@ -ReferencedAssemblies System.Drawing -PassThru | Out-Null
}
if (-not ('Win32.KeyIn' -as [type])) {
    Add-Type -Namespace Win32 -Name KeyIn -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, System.UIntPtr dwExtraInfo);
'@ -PassThru | Out-Null
}

function Get-CockpitWindow {
    # The window that HAS THE COMPOSER, for the reason submit_via_ui.ps1 records at length:
    # in WPF a popup, a tooltip and a dropdown are each a top-level window of the process, so
    # "the first window of the process" can be one with no controls in it.
    $proc = Get-Process -Name FleetCockpit -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $proc) { throw "FleetCockpit is not running." }
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $byPid = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)
    $idCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'goalInput')
    $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $byPid)
    for ($i = 0; $i -lt $wins.Count; $i++) {
        $w = $wins.Item($i)
        if ($w.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $idCond)) { return $w }
    }
    throw "no cockpit window exposes goalInput; is it minimized?"
}

function Get-State($hwnd) {
    $r = New-Object System.Drawing.Rectangle
    [void][Win32.WndState]::GetWindowRect($hwnd, [ref]$r)
    return [pscustomobject]@{
        Iconic     = [Win32.WndState]::IsIconic($hwnd)
        Zoomed     = [Win32.WndState]::IsZoomed($hwnd)
        Visible    = [Win32.WndState]::IsWindowVisible($hwnd)
        Foreground = ([Win32.WndState]::GetForegroundWindow() -eq $hwnd)
        Rect       = ("{0},{1} {2}x{3}" -f $r.X, $r.Y, ($r.Width - $r.X), ($r.Height - $r.Y))
    }
}

function Format-State($s) {
    "iconic={0} zoomed={1} visible={2} foreground={3} rect={4}" -f `
        $s.Iconic, $s.Zoomed, $s.Visible, $s.Foreground, $s.Rect
}

function Compare-State($before, $after) {
    $changed = @()
    foreach ($k in @("Iconic", "Zoomed", "Visible", "Foreground", "Rect")) {
        if ($before.$k -ne $after.$k) { $changed += ("{0}: {1} -> {2}" -f $k, $before.$k, $after.$k) }
    }
    return $changed
}

function Get-RunState {
    $p = Join-Path $Root ".fleet\status.json"
    if (-not (Test-Path $p)) { return $null }
    try { return (Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

function Send-ApiSteer([string]$worker, [string]$text) {
    # The same channel the cockpit writes, one file per command so nothing merges and nothing
    # can clobber -- see relay/fleet_runner.py::read_commands for why it stopped being a single
    # commands.json. Written to a .tmp first and moved, because the reader skips .tmp and would
    # otherwise be able to read a half-written file.
    $dir = Join-Path $Root ".fleet\commands.d"
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    $payload = @{ steer = @(@{ worker = $worker; text = $text }) } | ConvertTo-Json -Depth 5 -Compress
    $name = "repro-{0}-{1}" -f (Get-Date -Format "yyyyMMddHHmmssfff"), (Get-Random -Maximum 99999)
    $tmp = Join-Path $dir ($name + ".json.tmp")
    $fin = Join-Path $dir ($name + ".json")
    [System.IO.File]::WriteAllText($tmp, $payload, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $fin
    return $fin
}

function Send-UiaSteer($win, [string]$text, [bool]$withCtrl) {
    $idCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'goalInput')
    $box = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $idCond)
    if (-not $box) { throw "goalInput vanished between steps" }
    $vp = $null
    if (-not $box.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
        throw "goalInput does not offer ValuePattern"
    }
    $vp.SetValue($text)
    try { $box.SetFocus() } catch { }
    Start-Sleep -Milliseconds 250
    $VK_CONTROL = 0x11; $VK_RETURN = 0x0D; $KEYUP = 0x0002
    if ($withCtrl) { [Win32.KeyIn]::keybd_event($VK_CONTROL, 0, 0, [UIntPtr]::Zero) }
    [Win32.KeyIn]::keybd_event($VK_RETURN, 0, 0, [UIntPtr]::Zero)
    [Win32.KeyIn]::keybd_event($VK_RETURN, 0, $KEYUP, [UIntPtr]::Zero)
    if ($withCtrl) { [Win32.KeyIn]::keybd_event($VK_CONTROL, 0, $KEYUP, [UIntPtr]::Zero) }
}

# ---------------------------------------------------------------------------------------

$win = Get-CockpitWindow
$hwnd = [IntPtr]$win.Current.NativeWindowHandle
Write-Output ("cockpit: {0}  hwnd={1}" -f $win.Current.Name, $hwnd)
Write-Output ("baseline: " + (Format-State (Get-State $hwnd)))

$st = Get-RunState
$running = $false
$worker = ""
if ($st) {
    $running = [bool]$st.running
    foreach ($w in @($st.workers)) {
        if ($w -and $w.status -and $w.status -notin @("done", "stuck", "cancelled", "failed")) {
            $worker = [string]$w.name; break
        }
    }
}
Write-Output ("run in flight: {0}   first live worker: '{1}'" -f $running, $worker)

# A STEER NEEDS A RUN, AND SAYING SO IS THE WHOLE VALUE OF STOPPING HERE. With no run the
# cockpit refuses the steer (its `steer_dead` message) and the command file is consumed by
# nobody, so every path would report "no change" and the run would look like evidence that
# nothing is wrong. That is worse than no measurement.
if (-not $running -or -not $worker) {
    Write-Output ""
    Write-Output "NOT MEASURING: a steer needs a live run with a live worker."
    Write-Output "Start one first -- a throwaway goal is enough -- then run this again:"
    Write-Output "  powershell -NoProfile -File scripts\win\submit_via_ui.ps1 -Goal ""1+1 を計算して答えだけ返して"""
    exit 2
}

$want = if ($Paths -contains "all") { @("api", "uia-plain-enter", "uia-ctrl-enter") } else { $Paths }

Write-Output ""
Write-Output ("about to exercise {0} path(s) against worker '{1}':" -f $want.Count, $worker)
foreach ($p in $want) { Write-Output ("  {0}  text: {1}" -f $p.PadRight(16), $Text) }

if (-not $Confirm) {
    Write-Output ""
    Write-Output "DRY RUN -- nothing sent. Each of these delivers a real steer to a real worker."
    Write-Output "Re-run with -Confirm once you are content for that worker to receive it."
    exit 0
}

$results = @()
foreach ($p in $want) {
    $before = Get-State $hwnd
    $note = ""
    try {
        switch ($p) {
            "api"             { $note = "wrote " + (Split-Path -Leaf (Send-ApiSteer $worker $Text)) }
            "uia-plain-enter" { Send-UiaSteer $win $Text $false; $note = "plain Enter" }
            "uia-ctrl-enter"  { Send-UiaSteer $win $Text $true;  $note = "Ctrl+Enter" }
        }
    } catch {
        $note = "FAILED: " + $_.Exception.Message
    }
    Start-Sleep -Milliseconds $SettleMs
    $after = Get-State $hwnd
    $changed = Compare-State $before $after
    $results += [pscustomobject]@{ Path = $p; Note = $note; Changed = $changed }
    Write-Output ""
    Write-Output ("--- {0} ({1})" -f $p, $note)
    Write-Output ("    before: " + (Format-State $before))
    Write-Output ("    after : " + (Format-State $after))
    if ($changed.Count -eq 0) { Write-Output "    window state: UNCHANGED" }
    else { foreach ($c in $changed) { Write-Output ("    window state CHANGED -- " + $c) } }
}

Write-Output ""
Write-Output "=== summary ==="
foreach ($r in $results) {
    $verdict = if ($r.Changed.Count -eq 0) { "no change" } else { ($r.Changed -join "; ") }
    Write-Output ("  {0}  {1}" -f $r.Path.PadRight(16), $verdict)
}
Write-Output ""
Write-Output "Read it this way: if 'api' changed the window state, no keystroke was involved and"
Write-Output "the interrupt DELIVERY is the cause. If only the two uia- paths did, the key"
Write-Output "handling is. If nothing changed, this configuration does not reproduce it -- say so"
Write-Output "rather than concluding it is fixed."
