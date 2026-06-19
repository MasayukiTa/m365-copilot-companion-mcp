<#
.SYNOPSIS
  One-click setup for m365-copilot-companion-mcp.

.DESCRIPTION
  Prepares a freshly cloned / unzipped copy for use:
    1. Verifies Python 3.10+ is available.
    2. Creates a .venv virtual environment (idempotent).
    3. Installs the Python dependencies from requirements.txt.
    4. Generates a .env with fresh random secrets if one does not exist yet.
    5. Optionally installs external helper tools via winget (Dev Tunnels CLI,
       Tesseract OCR) when -WithExternalTools is passed.

  Safe to re-run. Existing .env secrets are never overwritten.

  NOTE: This file is intentionally English-only. Do not add non-ASCII text to
  any .ps1 in this repo; PowerShell mis-decodes it and breaks the script.

.PARAMETER WithExternalTools
  Also install optional external tools through winget.

.PARAMETER Force
  Recreate the virtual environment from scratch.

.EXAMPLE
  .\setup.ps1

.EXAMPLE
  .\setup.ps1 -WithExternalTools
#>
param(
    [switch]$WithExternalTools,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "    WARN: $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
Write-Step "Checking Python"
$pythonCmd = $null
foreach ($candidate in @("python", "py -3")) {
    try {
        $parts = $candidate.Split(" ")
        $ver = & $parts[0] $parts[1..($parts.Length - 1)] --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 10) { $pythonCmd = $candidate; break }
        }
    } catch { }
}
if (-not $pythonCmd) {
    Write-Warn2 "Python 3.10+ not found on PATH."
    Write-Host "    Install it with:  winget install Python.Python.3.12" -ForegroundColor Yellow
    Write-Host "    Then re-run this script." -ForegroundColor Yellow
    exit 1
}
Write-Ok "Using '$pythonCmd' ($ver)"

# ---------------------------------------------------------------------------
# 2. Virtual environment
# ---------------------------------------------------------------------------
Write-Step "Creating virtual environment (.venv)"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if ($Force -and (Test-Path ".venv")) {
    Write-Warn2 "Removing existing .venv (-Force)"
    Remove-Item -Recurse -Force ".venv"
}
if (-not (Test-Path $venvPython)) {
    $parts = $pythonCmd.Split(" ")
    & $parts[0] $parts[1..($parts.Length - 1)] -m venv .venv
    Write-Ok "Created .venv"
} else {
    Write-Ok ".venv already present"
}

# ---------------------------------------------------------------------------
# 3. Python dependencies
# ---------------------------------------------------------------------------
Write-Step "Installing Python dependencies"
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt
Write-Ok "Dependencies installed"

# ---------------------------------------------------------------------------
# 4. .env with fresh secrets
# ---------------------------------------------------------------------------
Write-Step "Preparing .env"
if (Test-Path ".env") {
    Write-Ok ".env already exists (left untouched)"
} else {
    function New-Hex([int]$bytes) {
        $buf = New-Object byte[] $bytes
        $rng = [System.Security.Cryptography.RNGCryptoServiceProvider]::new()
        $rng.GetBytes($buf)
        ($buf | ForEach-Object { $_.ToString("x2") }) -join ""
    }
    $apiKey   = New-Hex 20   # 40 hex chars
    $unlockPw = New-Hex 8    # 16 hex chars

    if (Test-Path ".env.example") {
        $lines = Get-Content ".env.example"
    } else {
        $lines = @(
            "MCP_API_KEY=replace",
            "MCP_UNLOCK_PASSWORD=replace",
            "MCP_UNLOCK_TTL_DAYS=30",
            "MCP_ALLOWED_BASE=~"
        )
    }
    $out = foreach ($line in $lines) {
        if ($line -match "^\s*MCP_API_KEY\s*=")          { "MCP_API_KEY=$apiKey" }
        elseif ($line -match "^\s*MCP_UNLOCK_PASSWORD\s*=") { "MCP_UNLOCK_PASSWORD=$unlockPw" }
        else { $line }
    }
    Set-Content -Path ".env" -Value $out -Encoding ASCII
    Write-Ok "Wrote .env with fresh random MCP_API_KEY and MCP_UNLOCK_PASSWORD"
    Write-Host "    Keep these secret. Your Bearer token is: $apiKey" -ForegroundColor Magenta
    Write-Host "    Your unlock password is:               $unlockPw" -ForegroundColor Magenta
}

# ---------------------------------------------------------------------------
# 5. Optional external tools (winget)
# ---------------------------------------------------------------------------
if ($WithExternalTools) {
    Write-Step "Installing optional external tools via winget"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Warn2 "winget not found. Skipping external tools."
    } else {
        # Tesseract (OCR) via winget -- best-effort.
        Write-Host "    Installing Tesseract OCR (ocr_* tools) [UB-Mannheim.TesseractOCR]" -ForegroundColor Cyan
        try {
            winget install --id UB-Mannheim.TesseractOCR --accept-source-agreements --accept-package-agreements --silent -e
            Write-Ok "Tesseract OCR"
        } catch { Write-Warn2 "Failed to install Tesseract: $($_.Exception.Message)" }

        # Dev Tunnels: delegated to the robust, idempotent setup_devtunnel.ps1, which (a) installs
        # the CLI via winget OR a direct download if winget fails, (b) signs in via browser OR a
        # device code if the browser does not open, and (c) CREATES the tunnel + port and prints the
        # public URL to paste into Copilot Studio. This replaces the old fragile inline winget+login
        # that left a fresh machine with no CLI, no sign-in, and no URL. See docs/STARTUP_devtunnel_login.md.
        Write-Host "    Dev Tunnels: running setup_devtunnel.ps1 (install + sign-in + tunnel + URL)..." -ForegroundColor Cyan
        try { & (Join-Path $PSScriptRoot "setup_devtunnel.ps1") }
        catch { Write-Warn2 "setup_devtunnel.ps1 failed: $($_.Exception.Message) -- run '.\setup_devtunnel.ps1' manually." }

        Write-Host "    Not installable via winget (install manually only if you need them):" -ForegroundColor Yellow
        Write-Host "      - Microsoft PowerPoint / Outlook  (for pptx_export_png / outlook_* tools)" -ForegroundColor Yellow
        Write-Host "      - ODBC Driver 18 for SQL Server   (for odbc_* tools)" -ForegroundColor Yellow
        Write-Host "      - Poppler                          (for ocr_pdf)" -ForegroundColor Yellow
    }
} else {
    Write-Step "Optional external tools"
    Write-Ok "Skipped. Re-run with -WithExternalTools to install devtunnel + Tesseract."
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next steps:" -ForegroundColor Green
Write-Host "  1. Start the MCP server:        .\start.ps1"
Write-Host "  2. (Remote clients) sign in to Dev Tunnels ONCE, then host + keep alive:"
Write-Host "       devtunnel login          # one-time interactive sign-in (persists across reboots) -- BEFORE the supervisor"
Write-Host "       .\supervisor.ps1 -TunnelName <your-tunnel-name>"
Write-Host "  3. Point your MCP client at http://localhost:8000/mcp with header"
Write-Host "       Authorization: Bearer <MCP_API_KEY from .env>"
