# register_selfimprove_nightly.ps1 -- the schedule that runs the self-improvement loop.
#
# WHY A FILE AND NOT A CLICK. The loop had every part except the part that starts it, and the
# reason is written in relay/selfimprove/l2_cron.py: "This module does NOT register any schedule
# -- that is a separate, deliberate operator step." The step was never taken, and nothing in the
# repository recorded that it had not been. Measured 2026-09-14: one scheduled task on the
# machine, unrelated; the newest file under .fleet/selfimprove was 20 days old.
#
# So the registration lives here, in the tree, where it can be read, re-run and reviewed -- not
# in a dialog box on one machine.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\win\register_selfimprove_nightly.ps1
#   ... -Remove        remove the schedule
#   ... -Show          print what is registered, change nothing
#   ... -At 04:30      a different time
#
# IT DRIVES THE FLEET. One firing runs a real sweep with real turns. What it may NOT do is
# install its own winner: scripts/run_nightly_real.py passes activate=False, and a scheduled run
# that could change the running harness is a system that changes while nobody is watching.

param(
    [switch]$Remove,
    [switch]$Show,
    [string]$At = "03:30",
    [string]$TaskName = "SelfImproveNightly"
)

$ErrorActionPreference = "Stop"
$repo   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $repo ".venv\Scripts\python.exe"
$driver = Join-Path $repo "scripts\selfimprove_driver.py"

function Show-Task {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Output "not registered: $TaskName"; return }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Output ("task    : {0}  state={1}" -f $t.TaskName, $t.State)
    foreach ($a in $t.Actions) { Write-Output ("action  : {0} {1}" -f $a.Execute, $a.Arguments) }
    foreach ($g in $t.Triggers) { Write-Output ("trigger : {0}" -f $g.StartBoundary) }
    Write-Output ("last run: {0}  result={1}" -f $info.LastRunTime, $info.LastTaskResult)
    Write-Output ("next run: {0}" -f $info.NextRunTime)
}

if ($Show) { Show-Task; exit 0 }

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "removed: $TaskName"
    exit 0
}

if (-not (Test-Path $python)) { throw "no interpreter at $python" }
if (-not (Test-Path $driver)) { throw "no driver at $driver" }

# -WorkingDirectory is the repo, because the driver and everything under it resolve .fleet and
# the venv relative to the tree rather than to wherever Task Scheduler happens to start.
$action  = New-ScheduledTaskAction -Execute $python -Argument "`"$driver`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# StartWhenAvailable so a machine that was asleep at 03:30 still runs the pass when it wakes --
# without it a laptop simply skips the night and the log shows nothing, which is the exact
# failure mode this whole exercise is about. No wake-to-run: it must not power a machine up.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 5) `
    -MultipleInstances IgnoreNew `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "One gated self-improvement pass (activate=False)." `
    -Force | Out-Null

Write-Output "registered: $TaskName"
Show-Task
