@echo off
REM Build and launch the WPF Copilot chat.
REM
REM THIS FILE NO LONGER CARRIES A SOURCE LIST -- see the note in build_cockpit.bat. Both .bats
REM restated which .cs each binary compiles, both went stale the first time a shared source file
REM was added (ui\FleetCommands.cs, 2026-09-22), and neither said so: they just stopped
REM compiling, and only CI's C# build noticed.
REM
REM rebuild_ui.ps1 owns the list and builds both binaries, which is also what keeps the cockpit
REM from relaunching a stale chat.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0rebuild_ui.ps1"
exit /b %ERRORLEVEL%
