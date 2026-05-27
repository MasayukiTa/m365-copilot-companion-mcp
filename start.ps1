$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not $env:PYTHONIOENCODING) { $env:PYTHONIOENCODING = "utf-8" }

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Test-Python($pythonCommand) {
    try {
        & $pythonCommand -c "import sys; print(sys.version)" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if ((Test-Path $venvPython) -and (Test-Python $venvPython)) {
    & $venvPython main.py
    exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    python main.py
    exit $LASTEXITCODE
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 main.py
    exit $LASTEXITCODE
}

throw "Python was not found. Recreate .venv or install Python 3.10+ and run pip install -r requirements.txt."
