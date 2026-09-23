# convenience_marker.ps1 -- the ONE place that knows where the two convenience launchers live
# and how the person's answer about them is recorded.
#
# THE RECORD IS .setup\convenience_provisioned, written by quickstart.bat as
#   shortcut=yes|no
#   autostart=yes|no
# and read by start_all.ps1 (Ensure-ConvenienceProvisioning). Until now only quickstart WROTE
# it, so an answer given any other way did not stick: unregister-supervisor.ps1 removed the
# Startup shortcut and the logon task and left "autostart=yes" behind, and the next start_all
# put both back (new-PC analysis D11). Every script that changes one of the two things now
# records the change here too, so the file always says what the person last asked for.
#
# THE .LNK NAMES WERE WRITTEN OUT IN TWO SCRIPTS, and start_all now has to test for the files
# as well. Three copies of one path is how one of them ends up checking a file nobody creates;
# they are defined once, below.
#
# Dot-source to use:  . scripts\win\convenience_marker.ps1
# No top-level side effects. ASCII / ENGLISH ONLY.

function Get-DesktopLauncherPath {
    # AN OVERRIDE FOR THE TESTS, NOT A SETTING. [Environment]::GetFolderPath reads the shell's
    # known-folder registry, which no child process can redirect; without this the only way to
    # exercise unregister-supervisor.ps1 would be against the owner's real Startup folder.
    $dir = $env:M365_COMPANION_DESKTOP_DIR
    if (-not $dir) { $dir = [Environment]::GetFolderPath("Desktop") }
    return (Join-Path $dir "M365 Companion.lnk")
}

function Get-StartupLauncherPath {
    $dir = $env:M365_COMPANION_STARTUP_DIR
    if (-not $dir) { $dir = [Environment]::GetFolderPath("Startup") }
    return (Join-Path $dir "M365 Companion.lnk")
}

function Get-ConvenienceMarkerPath([string]$RepoRoot) {
    return (Join-Path $RepoRoot ".setup\convenience_provisioned")
}

function Read-ConvenienceDecision([string]$RepoRoot) {
    # $null when there is no file (no decision was ever recorded -- NOT consent). Otherwise a
    # hashtable of the key=value lines. An old marker holding only the word "provisioned"
    # comes back as an empty hashtable, which start_all reads as "already handled, touch
    # nothing".
    $p = Get-ConvenienceMarkerPath $RepoRoot
    if (-not (Test-Path -LiteralPath $p)) { return $null }
    $decision = @{}
    foreach ($line in @(Get-Content -LiteralPath $p -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*([A-Za-z_]+)\s*=\s*(\S+)\s*$') { $decision[$matches[1]] = $matches[2] }
    }
    return $decision
}

function Set-ConvenienceDecision([string]$RepoRoot, [string]$Key, [string]$Value) {
    # Replace (or add) one key=value line and keep every other line as it was -- the other
    # answer, and a legacy "provisioned" word, belong to somebody else's decision. Written to a
    # temp file and moved over the old one, so a crash mid-write cannot leave half a record
    # (an empty record reads as "touch nothing", which would silently drop the answer).
    # Returns $true when the record now says Key=Value.
    try {
        $p = Get-ConvenienceMarkerPath $RepoRoot
        $dir = Split-Path -Parent $p
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $lines = @()
        if (Test-Path -LiteralPath $p) { $lines = @(Get-Content -LiteralPath $p -ErrorAction Stop) }
        $out = @()
        $done = $false
        foreach ($line in $lines) {
            if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) {
                if (-not $done) { $out += ($Key + "=" + $Value); $done = $true }
            } else {
                $out += $line
            }
        }
        if (-not $done) { $out += ($Key + "=" + $Value) }
        $tmp = $p + ".tmp"
        [System.IO.File]::WriteAllLines($tmp, [string[]]$out, (New-Object System.Text.ASCIIEncoding))
        Move-Item -LiteralPath $tmp -Destination $p -Force
        return $true
    } catch {
        return $false
    }
}

function Get-ConvenienceProvisioningPlan {
    # PURE. What start_all should create now, given the recorded answers and what exists.
    #   * nothing is created without a "yes" on record;
    #   * a "yes" re-creates the thing only when it is MISSING -- re-running both scripts on
    #     every start (the old behaviour) rewrote the shortcut and re-registered the logon task
    #     every single day, whatever state the person had left them in.
    # Returns @{ Shortcut = <bool>; Autostart = <bool> }.
    param($Decision, [bool]$ShortcutPresent, [bool]$AutostartPresent)
    $plan = @{ Shortcut = $false; Autostart = $false }
    if ($null -eq $Decision) { return $plan }
    $plan.Shortcut  = (($Decision['shortcut'] -eq 'yes') -and -not $ShortcutPresent)
    $plan.Autostart = (($Decision['autostart'] -eq 'yes') -and -not $AutostartPresent)
    return $plan
}
