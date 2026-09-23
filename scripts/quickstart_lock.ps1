# quickstart_lock.ps1 -- one quickstart at a time per checkout (D21).
#
# WHY. Nothing stopped two quickstart windows running at once (a double double-click is enough),
# and the effects were all silent: both ran pip into the same .venv; both saw no .env and each
# wrote a fresh one with DIFFERENT secrets, so one window displayed a Bearer token and unlock
# password that were not the ones saved; both ran `devtunnel create <name>` and the loser took
# the collision branch and created a second tunnel. Only the supervisor had a mutex.
#
# HOW. A lock FILE created with CreateNew -- the one filesystem operation that is atomic
# between two processes racing for it -- holding the owner's PID and that process's start time.
# The owner is the cmd.exe running quickstart.bat (this script's parent). A lock is STALE, and
# is taken over, when that process is gone or the PID now belongs to a different process
# (start time differs: PIDs are reused). A lock held by the SAME cmd (quickstart re-run in the
# console it was interrupted in) is ours. quickstart.bat releases it on every exit it controls;
# a window closed mid-run leaves a lock whose owner is dead, which the next run takes over.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\quickstart_lock.ps1 acquire
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\quickstart_lock.ps1 release
#
# EXIT: 0 acquired / released; 10 another LIVE quickstart holds it (message printed, names the
# PID); 11 could not CONFIRM ownership after 3 attempts (message printed) -- treated the same as
# "in use": quickstart.bat fails closed on either code rather than installing unlocked, because
# an unconfirmed lock is exactly the ambiguous state two quickstarts racing into one .venv looks
# like from here. Anything else (a crash in this script, or cmd's own 9009 when PowerShell itself
# is not on PATH) also makes quickstart.bat fail closed with a generic message -- see quickstart.bat.
#
# ASCII / ENGLISH ONLY.
param(
    [Parameter(Position = 0)][ValidateSet('acquire', 'release')][string]$Action = 'acquire',
    [string]$LockPath = '',
    [int]$OwnerPid = 0          # tests only; default is this script's parent (the cmd.exe)
)
$ErrorActionPreference = 'Stop'
if (-not $LockPath) { $LockPath = Join-Path (Split-Path -Parent $PSScriptRoot) '.setup\quickstart.lock' }

function Get-ProcessStamp([int]$ProcessId) {
    # "<pid>|<creation time ticks>" or '' when the process does not exist.
    $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $ProcessId) -ErrorAction SilentlyContinue
    if (-not $p) { return '' }
    return ("{0}|{1}" -f $ProcessId, $p.CreationDate.ToUniversalTime().Ticks)
}

function Get-ParentPid {
    $me = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $PID)
    return [int]$me.ParentProcessId
}

function Read-Lock([string]$Path) {
    try { return ([System.IO.File]::ReadAllText($Path)).Trim() } catch { return $null }
}

function Test-LockAlive([string]$Content) {
    # Alive = the recorded process still exists AND is the same process (start time matches).
    if (-not $Content -or $Content -notmatch '^(\d+)\|(\d+)$') { return $false }
    return ((Get-ProcessStamp ([int]$Matches[1])) -eq $Content)
}

function Try-CreateLock([string]$Path, [string]$Content) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew,
                                     [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    } catch [System.IO.IOException] {
        return $false
    }
    try {
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($Content)
        $fs.Write($bytes, 0, $bytes.Length)
    } finally { $fs.Dispose() }
    return $true
}

$owner = if ($OwnerPid -gt 0) { $OwnerPid } else { Get-ParentPid }
$stamp = Get-ProcessStamp $owner

if ($Action -eq 'release') {
    $cur = Read-Lock $LockPath
    # Only OUR lock is removed; a lock another live quickstart took over is not ours to drop.
    if ($cur -ne $null -and ($cur -eq $stamp -or -not (Test-LockAlive $cur))) {
        Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
    }
    exit 0
}

for ($attempt = 0; $attempt -lt 3; $attempt++) {
    if (Try-CreateLock $LockPath $stamp) { exit 0 }
    $cur = Read-Lock $LockPath
    if ($cur -eq '') {
        # Created a moment ago and not yet written: give the creator time to write it before
        # judging it, or two starters would each delete the other's fresh lock.
        Start-Sleep -Milliseconds 300
        $cur = Read-Lock $LockPath
    }
    if ($cur -eq $stamp) { exit 0 }                 # same console, re-run after an interrupt
    if ($cur -ne $null -and (Test-LockAlive $cur)) {
        $otherPid = ($cur -split '\|')[0]
        Write-Host ""
        Write-Host "  ANOTHER quickstart.bat IS ALREADY RUNNING for this folder (process $otherPid)."
        Write-Host "  Two at once would install into the same .venv and could write two different"
        Write-Host "  sets of secrets, so this one stops here and changes nothing."
        Write-Host "  Finish or close the other quickstart window, then run quickstart.bat again."
        Write-Host "  If you are sure no other quickstart is open, delete this file and retry:"
        Write-Host "      $LockPath"
        exit 10
    }
    # Stale (owner gone or PID reused) or unreadable mid-write: remove and race for it again.
    Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 150
}
# FAIL CLOSED. 3 attempts each either lost the create race to a lock that stayed unreadable/
# contested or hit some other repeated interference (never resolving to "ours", "stale", or "a
# live PID") -- an ambiguous state, not a confirmed absence of another quickstart. Proceeding
# unlocked here is exactly the bug this lock exists to close: two installs could still write
# different secrets into the same .env. This is NOT "helper cannot run" (that is any other
# non-zero, e.g. cmd's own 9009) -- the helper ran fine three times and still could not confirm
# ownership, so it is reported the same as "in use", with automatic stale-lock detection (by the
# owning PID's liveness, not by asking anyone to delete a file) already having been tried above.
Write-Host ""
Write-Host "  COULD NOT CONFIRM the quickstart lock ($LockPath) after 3 attempts -- something kept"
Write-Host "  changing it underneath this check. Treated as IN USE, so nothing was installed."
Write-Host "  A lock left behind by a closed quickstart window is detected and cleared"
Write-Host "  automatically (by checking whether its owning process is still alive), so this"
Write-Host "  usually clears itself. Wait a few seconds and run quickstart.bat again."
exit 11
