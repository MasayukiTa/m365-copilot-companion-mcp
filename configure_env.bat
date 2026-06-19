@echo off
REM ===========================================================================
REM  Configure the M365 Copilot agent URLs in .env -- opens a dialog window.
REM  Double-click this any time to set / change the agent URLs (main, fleet,
REM  research, analyst). No hand-editing of .env. ASCII / ENGLISH ONLY.
REM ===========================================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure_env.ps1"
