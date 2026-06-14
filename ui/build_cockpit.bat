@echo off
REM Build the native WPF fleet cockpit with the C# compiler that ships with Windows.
REM No Visual Studio, no .NET SDK, no Node. Then launch it.
setlocal
set "FW=C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
set "CSC=%FW%\csc.exe"
set "WPF=%FW%\WPF"
if not exist "%CSC%" ( echo ERROR: csc.exe not found - .NET Framework 4.x required & exit /b 1 )
REM /win32manifest embeds app.manifest (Per-Monitor V2 DPI) so the OS loader marks the process
REM DPI-aware at creation -> crisp on high-DPI/secondary monitors instead of bitmap-stretched.
"%CSC%" /nologo /target:winexe /win32manifest:"%~dp0app.manifest" /out:"%~dp0FleetCockpit.exe" /r:"%WPF%\PresentationFramework.dll" /r:"%WPF%\PresentationCore.dll" /r:"%WPF%\WindowsBase.dll" /r:"%FW%\System.Xaml.dll" /r:"%FW%\System.Web.Extensions.dll" /r:"%FW%\System.Windows.Forms.dll" "%~dp0FleetCockpit.cs"
if errorlevel 1 ( echo BUILD FAILED & exit /b 1 )
echo BUILD OK: %~dp0FleetCockpit.exe
start "" "%~dp0FleetCockpit.exe"
