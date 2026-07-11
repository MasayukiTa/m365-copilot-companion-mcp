@echo off
cd /d "%~dp0"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
    "%VENV_PY%" "bench\review_run.py" --kind security %*
) else (
    echo .venv python not found; run quickstart.bat first.
)
pause
