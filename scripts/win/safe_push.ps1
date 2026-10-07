# Push the current branch through relay.git_push_safety (see scripts/safe_push.py).
# Extra arguments are passed straight through, e.g.  safe_push.ps1 --force   or   --check
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py (Join-Path $repo "scripts\safe_push.py") --repo (Get-Location).Path @args
exit $LASTEXITCODE
