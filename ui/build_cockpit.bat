@echo off
REM Build and launch the WPF fleet cockpit.
REM
REM THIS FILE NO LONGER CARRIES A SOURCE LIST. It used to spell out which .cs go into
REM FleetCockpit.exe, which made it the third of four copies of one fact -- and on 2026-09-22 the
REM copies drifted: ui\FleetCommands.cs was added to rebuild_ui.ps1 and to nothing else, so CI's
REM C# build broke with "CS0103: The name 'FleetCommands' does not exist in the current context"
REM and THIS FILE was broken too, silently, for anybody who ran it. A list nobody compiles is a
REM list nobody notices going stale.
REM
REM rebuild_ui.ps1 is what actually produces the shipped binaries, so it owns the list. It also
REM removes the race this pair of .bats created: each of them ended with `start <exe>`, and the
REM cockpit relaunches CopilotChat, so building them one after the other could leave an OLD chat
REM running. Building both is the point, not an extra cost.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0rebuild_ui.ps1"
exit /b %ERRORLEVEL%
