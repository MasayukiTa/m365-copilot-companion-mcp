@echo off
REM Compile-only check for FleetCockpit (no manifest, no GUI launch, separate output).
setlocal
set "FW=C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
if not exist "%~dp0_buildcheck" mkdir "%~dp0_buildcheck"
"%FW%\csc.exe" /nologo /target:winexe /out:"%~dp0_buildcheck\FleetCockpit_test.exe" /r:"%FW%\WPF\PresentationFramework.dll" /r:"%FW%\WPF\PresentationCore.dll" /r:"%FW%\WPF\WindowsBase.dll" /r:"%FW%\System.Xaml.dll" /r:"%FW%\System.Web.Extensions.dll" /r:"%FW%\System.Windows.Forms.dll" "%~dp0FleetCockpit.cs" "%~dp0SelfImproveDashboard.cs" "%~dp0Theme.cs"
echo BUILDCHECK_EXIT=%errorlevel%
