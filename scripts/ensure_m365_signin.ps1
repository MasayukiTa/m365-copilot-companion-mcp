# Wrapper so quickstart.bat can run the M365 sign-in step the same way it runs every other
# step: `powershell -File scripts\<name>.ps1`. quickstart invokes nothing but PowerShell, and
# adding a bare python call there would mean quickstart having to resolve an interpreter --
# which is exactly the dead code that was removed from it earlier.
#
# The real work is in ensure_m365_signin.py: it probes in the BACKGROUND and only brings the
# browser forward if a sign-in wall is actually showing.
# -CheckOnly asks the question without taking the window, so a background start can
# report a needed sign-in instead of surfacing a browser at nobody.
param([int]$Port = 9222, [double]$TimeoutSeconds = 600, [switch]$CheckOnly)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    # Same fallback order the supervisor uses. A missing venv is a bootstrap problem and is
    # reported by STEP 1, so this does not try to diagnose it -- it just does not crash here.
    $py = "python"
}

$script = Join-Path $PSScriptRoot "ensure_m365_signin.py"
if (-not (Test-Path $script)) {
    Write-Host "  (sign-in helper not found; skipping)"
    exit 0
}

$pyArgs = @($script, "--port", $Port, "--timeout", $TimeoutSeconds)
if ($CheckOnly) { $pyArgs += "--check-only" }
& $py @pyArgs
$code = $LASTEXITCODE

# NEVER FAIL THE WHOLE SETUP OVER THIS. If sign-in did not finish, everything else that was
# installed is still installed, and re-running quickstart.bat picks it up. The health check
# below reports it either way, so a non-zero here would only turn a resumable state into an
# alarming one.
if ($code -ne 0) {
    Write-Host "  (sign-in not completed yet -- the health check below will show it)"
}

# AND SAY WHICH BROWSERS ARE COVERED. Everything above signs in ONE profile: the companion on
# :9222. The bridge (:9223) and the evaluation browser (:9224) each have their own
# --user-data-dir -- Edge locks a profile to a single process, so concurrent browsers require
# distinct profiles -- which means their own cookies and their own sign-in.
#
# The owner asked on 2026-09-23 whether a sign-in could have gone into one of the two and not
# the other. It can, and nothing said so: quickstart's step reported the companion and stopped.
# A person who has just signed in should see, on the same screen, which browsers that covered.
#
# REPORTED, NOT DRIVEN. Surfacing a second browser to sign it in during setup is a window
# nobody asked for; doctor carries the per-profile rows with the command to fix each one.
$report = (& $py $script --port $Port --check-only --all 2>&1 | Out-String)
$rows = @()
foreach ($line in ($report -split "`r?`n")) {
    if ($line -match '^\s*PROFILE:\s+(\d+)\s+(\S+)\s+(\w+)') {
        $rows += ("    :{0}  {1,-24} {2}" -f $Matches[1], $Matches[2], $Matches[3])
    }
}
if ($rows.Count -gt 0) {
    Write-Host "  Sign-in state per browser profile (each has its own cookies):"
    $rows | ForEach-Object { Write-Host $_ }
}
exit 0
