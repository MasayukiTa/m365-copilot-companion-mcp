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
# THE BOTTOM COMPOSER ADDS TASKS WHILE A RUN IS ACTIVE. Start/Send and Ctrl+Enter share the
# same current cockpit path: idle starts a run, live enqueues add_goal work. Steering is a
# per-worker card action and this helper intentionally does not emulate it.
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
                    # STDERR, NOT THE OUTPUT STREAM. A PowerShell function returns everything
                    # written to output, so `Write-Output` here made the caller's $win a
                    # String and the next line died with
                    #     [System.String] does not contain a method named 'FindAll'
                    # -- a diagnostic message becoming the return value. The caller captures
                    # stderr with 2>&1, so the message still reaches the log.
                    if ($i -gt 0) { [Console]::Error.WriteLine(("cockpit: composer was in window {0} of {1}" -f ($i + 1), $wins.Count)) }
                    return $w
                }
            }
            # NO WINDOW HAS THE COMPOSER. A minimized WPF window drops its content out of the
            # automation tree, so restore once before concluding anything, rather than
            # retrying a search that cannot succeed.
            if (-not $restored -and $proc.MainWindowHandle -ne [IntPtr]::Zero) {
                $restored = $true
                [Console]::Error.WriteLine("cockpit: no window exposes goalInput; restoring the main window")
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
    # THE LEADING COMMA IS LOAD-BEARING. PowerShell unwraps a collection of exactly one
    # element as it leaves a function, so with a single Edit on screen this returned the
    # AutomationElement ITSELF rather than a collection of one, and the caller's
    # `$edits.Item($i)` died with
    #     [System.Windows.Automation.AutomationElement] does not contain a method named 'Item'
    #
    # WHAT MADE IT HARD TO SEE: `$edits.Count` still says 1 afterwards, because PowerShell
    # gives every object a synthetic Count. So the line above it prints "editable fields: 1",
    # which looks like the collection is intact, and the failure lands one line later on a
    # method call. Two facts that disagree, with the reassuring one printed first.
    #
    # WHEN IT FIRES: a cockpit that has just been rebuilt has no history box and no worker
    # cards, so goalInput is the ONLY Edit in the tree -- exactly one. A freshly started
    # cockpit therefore hit this every time, while one that had been used did not.
    #
    # The same shape is safe at the two other FindAll sites in this file (`$wins`, `$btns`)
    # because those assign the result to a variable inside the same scope; the unwrap happens
    # on the way OUT of a function, and only this one returns.
    return ,$window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
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
#
# AND THEN KEYSTROKES WERE DROPPED ENTIRELY (2026-08-30). Submit now invokes the Start/Send
# button through InvokePattern, which needs no focus and no foreground window at all -- see
# the comment inside Submit. The Send-CtrlEnter that used to live here was left behind,
# called from nowhere, for a fortnight: a function that still looks like the way this script
# works, that a reader would reasonably call, and that would silently reintroduce the
# focus-dependence the button-invoke exists to avoid. Deleted rather than kept "in case",
# because the history above is the part worth keeping and it is right here.

function Get-GoalAcceptanceSnapshot([string[]]$goals) {
    $statusPath = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) ".fleet/status.json"
    $started = ""
    $running = $false
    $matchCount = 0
    try {
        if (Test-Path $statusPath) {
            $st = Get-Content $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($null -ne $st.started) { $started = [string]$st.started }
            $running = [bool]$st.running
            $wanted = @{}
            foreach ($g in @($goals)) {
                if (-not $wanted.ContainsKey($g)) { $wanted[$g] = 0 }
                $wanted[$g]++
            }
            $seen = @{}
            foreach ($w in @($st.workers)) {
                $wg = [string]$w.goal
                if (-not $wanted.ContainsKey($wg)) { continue }
                if (-not $seen.ContainsKey($wg)) { $seen[$wg] = 0 }
                $seen[$wg]++
            }
            foreach ($g in $wanted.Keys) {
                $matchCount += [Math]::Min([int]$wanted[$g], [int]($seen[$g]))
            }
        }
    } catch { }
    return [PSCustomObject]@{ Started = $started; Running = $running; MatchCount = $matchCount }
}

function Test-GoalAccepted($before, $after, [int]$expectedCount) {
    if ($expectedCount -le 0 -or $null -eq $before -or $null -eq $after) { return $false }
    $fresh = $after.Running -and -not [String]::IsNullOrEmpty([string]$after.Started) -and
             ($after.Started -ne $before.Started) -and ($after.MatchCount -ge $expectedCount)
    $liveGrowth = ($after.Started -eq $before.Started) -and
                  ($after.MatchCount -ge ($before.MatchCount + $expectedCount))
    return [bool]($fresh -or $liveGrowth)
}

function Submit([string]$text, [switch]$ExpectFleetGoal) {
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
    # INVOKE THE BUTTON, DO NOT SEND A KEYSTROKE.
    #
    # keybd_event goes wherever keyboard focus is, so it needs a foreground window -- and
    # measured 2026-08-30 there was none: GetForegroundWindow() returned 0, the cockpit
    # exposed two top-level windows of which the FIRST was a leftover WPF Popup (class
    # 'Popup') that had taken over MainWindowHandle, and Set-Text's value sat in the composer
    # while the script reported that nothing was submitted. Foregrounding the right window
    # does not fix it either: SetForegroundWindow is refused to a process that is not already
    # in front, which is the normal state for an unattended run.
    #
    # The button does the same thing Ctrl+Enter does -- FleetCockpit.cs gives _startBtn and
    # the composer's Return handler the same body -- and InvokePattern needs no focus, no
    # foreground window and no keyboard at all.
    $startBtn = $null
    $bc = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)
    $btns = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $bc)
    # BY NAME, in both languages the cockpit ships. The name comes from T("start"), so these
    # two strings are the whole set -- matching on width instead would pick a different
    # button the moment the layout changes.
    $wanted = @("並列実行を開始", "Start parallel run", "送信", "Send", "追加", "Add")
    for ($i = 0; $i -lt $btns.Count -and -not $startBtn; $i++) {
        $b = $btns.Item($i)
        if ($wanted -contains $b.Current.Name -and $b.Current.IsEnabled) { $startBtn = $b }
    }
    if (-not $startBtn) {
        throw ("no start button found in the cockpit (looked for: " + ($wanted -join ", ") +
               "). Ctrl+Enter cannot be used instead: an unattended run has no foreground window.")
    }
    $ip = $null
    if (-not $startBtn.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) {
        throw "the start button does not support Invoke"
    }

    # FAIL CLOSED ON COMPOSER CORRUPTION. ValuePattern.SetValue() above is supposed to replace
    # the whole text atomically, but an unattended machine can still receive an external edit
    # between that write and this button invoke. Measured 2026-09-28: a 645-char READ-ONLY goal
    # reached goals_input.txt as 646 chars with a leading "3". Re-read the SAME textbox at the
    # last possible moment and require an ordinal exact match before creating any durable work.
    $vpVerify = $null
    if (-not $target.TryGetCurrentPattern(
            [System.Windows.Automation.ValuePattern]::Pattern, [ref]$vpVerify)) {
        throw "cannot re-read the composer immediately before submit"
    }
    $observed = [string]$vpVerify.Current.Value
    if (-not [String]::Equals($observed, $text, [StringComparison]::Ordinal)) {
        $common = [Math]::Min($observed.Length, $text.Length)
        $at = 0
        while ($at -lt $common -and $observed[$at] -eq $text[$at]) { $at++ }
        $expectedCode = if ($at -lt $text.Length) { "U+{0:X4}" -f [int][char]$text[$at] } else { "<end>" }
        $observedCode = if ($at -lt $observed.Length) { "U+{0:X4}" -f [int][char]$observed[$at] } else { "<end>" }
        throw ("composer text changed before submit at index {0}: expected {1}, observed {2}; lengths {3}->{4}. " +
               "Nothing was submitted and the current composer text was left untouched." -f
               $at, $expectedCode, $observedCode, $text.Length, $observed.Length)
    }

    $expectedGoals = @()
    $baseline = $null
    if ($ExpectFleetGoal) {
        $expectedGoals = @($text -split "`r?`n" | ForEach-Object { $_.Trim() } |
                           Where-Object { $_.Length -gt 0 -and -not $_.StartsWith('#') })
        if ($expectedGoals.Count -eq 0) { throw "fleet goal submission contains no usable goal lines" }
        $baseline = Get-GoalAcceptanceSnapshot $expectedGoals
    }

    [Console]::Error.WriteLine(("submit: invoking button '{0}'" -f $startBtn.Current.Name))
    $ip.Invoke()

    Start-Sleep -Milliseconds 900
    # VERIFY THE HANDOFF, not one historical UI side-effect. Older cockpit builds cleared the
    # composer immediately. Current fresh-Start deliberately keeps operator text until the new
    # runner actually owns the state-dir and its goal appears in status.json. A still-filled box
    # can therefore mean "durably waiting for the closing coordinator", not "nothing happened".
    $after = ""
    $vp2 = $null
    if ($target.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp2)) {
        $after = [string]$vp2.Current.Value
    }
    if ($after.Trim().Length -eq 0) {
        Write-Output ("submitted: {0}" -f ($text.Substring(0, [Math]::Min(70, $text.Length))))
        return
    }
    if (-not $ExpectFleetGoal) {
        throw ("the composer still holds text after button invoke; command was not accepted: " +
               $after.Substring(0, [Math]::Min(60, $after.Length)))
    }

    $acceptDeadline = (Get-Date).AddSeconds([Math]::Max([int]$TimeoutSeconds, 65))
    while ((Get-Date) -lt $acceptDeadline) {
        $now = Get-GoalAcceptanceSnapshot $expectedGoals
        $accepted = Test-GoalAccepted $baseline $now $expectedGoals.Count
        if ($accepted) {
            Write-Output ("submitted: {0}" -f ($text.Substring(0, [Math]::Min(70, $text.Length))))
            return
        }
        Start-Sleep -Milliseconds 200
    }
    throw ("submission was not accepted before the timeout; composer text was preserved: " +
           $after.Substring(0, [Math]::Min(60, $after.Length)))
}

# IS A RUN ALREADY GOING? Keep this diagnostic visible because it is useful when a GUI submit
# misbehaves. It no longer changes Goal semantics: the bottom composer adds tasks in a live run.
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
    if ($Steer) {
        # The bottom composer is task intake in BOTH idle and live states now. Invoking its
        # button with -Steer would therefore ADD A TASK, not steer, which is worse than refusing.
        # Steering remains a per-worker card action until this UIA helper grows a card-targeted
        # path with an explicit worker identity.
        throw "-Steer is not supported by the current cockpit bottom composer; steer from the target worker card instead"
    }
    foreach ($g in $Goal) {
        if ($g -match "`n") { throw "a goal may not contain a newline; the cockpit splits on them" }
    }
    # The same visible button is Start when idle and Add while a run is live. Multiple goals are
    # placed in the composer together, one per line; Cockpit splits them into independent add_goal
    # items and its durable handoff/ack path owns the run-ending race.
    Submit ($Goal -join "`n") -ExpectFleetGoal
}
