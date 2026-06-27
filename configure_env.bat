@echo off
REM ===========================================================================
REM  Configure the M365 Copilot agent URLs in .env -- opens ONLY a dialog window.
REM  Double-click this any time to set / change the agent URLs (main, fleet,
REM  research, analyst). Routes through a windowless VBS so no console appears --
REM  just the URL-entry dialog. ASCII / ENGLISH ONLY.
REM ===========================================================================
cd /d "%~dp0"
wscript.exe "%~dp0configure_env_hidden.vbs"
