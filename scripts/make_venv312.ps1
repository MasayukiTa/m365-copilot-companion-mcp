#Requires -Version 5.1
<#
.SYNOPSIS
  Recreate the Python 3.12 virtual environment (.venv312) for this repo.

.DESCRIPTION
  main.py hits a native crash under Python 3.10.0 (observed two unattended
  exits). Python 3.12.14 runs it cleanly (8001 concurrent start, /health 200
  confirmed). The .venv312 that fixes this was originally built by hand and the
  recipe lived only in one person's head. This script is that recipe, made
  reproducible so anyone can rebuild .venv312 from scratch.

  What it does (idempotent):
    1. Locate (or install) uv -- the Astral Python/venv manager.
    2. Ensure uv has a managed CPython 3.12 (uv python install 3.12).
    3. Build .venv312 at the repo root with that interpreter, into a temporary
       dir first; only swap it into place after every step below succeeds.
    4. Install the pinned dependencies from requirements.txt.
    5. Verify the interpreter is 3.12 and that 'import main' succeeds.
  If any step fails, the partially built environment is removed and the
  existing .venv312 (if any) is left untouched. Re-running replaces .venv312.

.USAGE
  From the repo root:
    powershell -ExecutionPolicy Bypass -File scripts\make_venv312.ps1
  Optional:
    -Python 3.12.14   pin an exact managed CPython patch (default: 3.12)
    -Force            rebuild even if a healthy .venv312 already exists

.ROLLBACK / HOW TO UNDO
  .venv312 is NOT tracked in git (see .gitignore) and nothing else depends on
  it being present. To go back to the Python 3.10 .venv, just delete .venv312:
      Remove-Item -Recurse -Force .\.venv312
  supervisor.ps1 prefers .venv312 when it exists and falls back to .venv when
  it does not -- so removing the directory is the whole rollback, no other edit
  needed. (That supervisor preference is a separate change, job 7f461ec01a26;
  this script only builds the environment supervisor looks for.)

.NOTES
  ASCII only on purpose. No process is killed by name; nothing here touches a
  running server. Safe to run while the stack is up -- supervisor keeps using
  the current interpreter until the next respawn.
#>
[CmdletBinding()]
param(
    [string]$Python = '3.12',
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$m) { Write-Host "[make_venv312] $m" }
function Fail([string]$m) { Write-Error "[make_venv312] $m"; exit 1 }

# --- Resolve repo root (parent of this scripts\ dir) -------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
$Req       = Join-Path $RepoRoot 'requirements.txt'
$Target    = Join-Path $RepoRoot '.venv312'
$Staging   = Join-Path $RepoRoot ('.venv312.build-' + [System.Guid]::NewGuid().ToString('N').Substring(0,8))

if (-not (Test-Path $Req)) { Fail "requirements.txt not found at $Req" }

# --- Idempotent shortcut: healthy 3.12 env already present ------------------
if ((Test-Path $Target) -and (-not $Force)) {
    $existingPy = Join-Path $Target 'Scripts\python.exe'
    if (Test-Path $existingPy) {
        $v = (& $existingPy -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $v -eq '3.12') {
            Write-Step ".venv312 already present and is Python $v. Use -Force to rebuild. Nothing to do."
            exit 0
        }
    }
    Write-Step ".venv312 exists but is not a healthy 3.12 env; it will be rebuilt."
}

# --- Locate or install uv ---------------------------------------------------
function Resolve-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cands = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'uv\uv.exe'),
        (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe')
    )
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    return $null
}

$Uv = Resolve-Uv
if (-not $Uv) {
    Write-Step 'uv not found; installing it (Astral official installer, per-user, no admin).'
    try {
        $prev = $env:UV_NO_MODIFY_PATH
        powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
        if ($LASTEXITCODE -ne 0) { Fail 'uv installer exited non-zero.' }
    } catch {
        Fail "Could not install uv automatically: $($_.Exception.Message). Install uv manually (https://docs.astral.sh/uv/) and re-run."
    }
    $Uv = Resolve-Uv
    if (-not $Uv) { Fail 'uv still not found on PATH after install. Open a new shell or add it to PATH, then re-run.' }
}
Write-Step "Using uv at: $Uv"

# --- Ensure a managed CPython 3.12 -----------------------------------------
Write-Step "Ensuring uv-managed CPython $Python ..."
& $Uv python install $Python
if ($LASTEXITCODE -ne 0) { Fail "uv python install $Python failed." }

# --- Clean any stale staging dir -------------------------------------------
if (Test-Path $Staging) { Remove-Item -Recurse -Force $Staging }

$built = $false
try {
    # 1. Create venv with the managed 3.12 interpreter, into staging.
    Write-Step "Creating venv (Python $Python) at staging dir ..."
    & $Uv venv --python $Python $Staging
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed." }

    $PyExe = Join-Path $Staging 'Scripts\python.exe'
    if (-not (Test-Path $PyExe)) { throw "python.exe not found in staging venv." }

    # 2. Install pinned dependencies.
    Write-Step 'Installing requirements.txt ...'
    & $Uv pip install --python $PyExe -r $Req
    if ($LASTEXITCODE -ne 0) { throw "dependency install failed." }

    # 3. Verify interpreter version is 3.12.
    $ver = (& $PyExe -c "import sys;print('%d.%d'%sys.version_info[:2])")
    if ($LASTEXITCODE -ne 0) { throw "could not query interpreter version." }
    if ($ver -ne '3.12') { throw "expected Python 3.12 but got $ver." }
    Write-Step "Interpreter OK: Python $ver"

    # 4. Verify 'import main' succeeds from the repo root.
    Write-Step "Verifying 'import main' ..."
    & $PyExe -c "import main" 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) { throw "'import main' failed under the new env." }
    Write-Step "'import main' OK."

    # 5. Swap staging into place atomically-ish: remove old, rename staging.
    if (Test-Path $Target) {
        Write-Step 'Removing previous .venv312 ...'
        Remove-Item -Recurse -Force $Target
    }
    Rename-Item -Path $Staging -NewName (Split-Path -Leaf $Target)
    $built = $true
    Write-Step "Done. .venv312 is ready at: $Target"
}
finally {
    if (-not $built -and (Test-Path $Staging)) {
        Write-Step 'A step failed; removing the half-built environment (existing .venv312 left untouched).'
        Remove-Item -Recurse -Force $Staging -ErrorAction SilentlyContinue
    }
}

exit 0
