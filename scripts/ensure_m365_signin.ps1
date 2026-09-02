# Wrapper so quickstart.bat can run the M365 sign-in step the same way it runs every other
# step: `powershell -File scripts\<name>.ps1`. quickstart invokes nothing but PowerShell, and
# adding a bare python call there would mean quickstart having to resolve an interpreter --
# which is exactly the dead code that was removed from it earlier.
#
# The real work is in ensure_m365_signin.py: it probes in the BACKGROUND and only brings the
# browser forward if a sign-in wall is actually showing.
param([int]$Port = 9222, [double]$TimeoutSeconds = 600)

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

& $py $script --port $Port --timeout $TimeoutSeconds
$code = $LASTEXITCODE

# NEVER FAIL THE WHOLE SETUP OVER THIS. If sign-in did not finish, everything else that was
# installed is still installed, and re-running quickstart.bat picks it up. The health check
# below reports it either way, so a non-zero here would only turn a resumable state into an
# alarming one.
if ($code -ne 0) {
    Write-Host "  (sign-in not completed yet -- the health check below will show it)"
}
exit 0
