@echo off
REM Compile-only check for the WPF binaries (no manifest, no GUI launch, separate output).
REM
REM TWO DEFECTS THIS FILE HAD, BOTH OF THE SAME SHAPE -- it looked like a check and could not be
REM one:
REM   1. It carried its own copy of the cockpit's source list, which went stale the first time a
REM      shared source was added (ui\FleetCommands.cs, 2026-09-22). The check compiled something
REM      nobody ships, and could not have caught the break it exists to catch.
REM   2. It printed BUILDCHECK_EXIT=<n> and then returned 0 regardless, so no caller could tell
REM      a clean build from a broken one without reading the text.
REM
REM bench\ui_build_check.py parses the source list out of rebuild_ui.ps1 -- the script that
REM actually produces the binaries -- and exits non-zero when a target fails. One list, one
REM verdict, and this stays as the entry point people already know.
setlocal
python "%~dp0..\bench\ui_build_check.py"
exit /b %ERRORLEVEL%
