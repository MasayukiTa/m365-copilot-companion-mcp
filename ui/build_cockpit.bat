@echo off
REM Build the native WPF fleet cockpit with the C# compiler that ships with Windows.
REM No Visual Studio, no .NET SDK, no Node. Then launch it.
setlocal
set "FW=C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
set "CSC=%FW%\csc.exe"
set "WPF=%FW%\WPF"
if not exist "%CSC%" ( echo ERROR: csc.exe not found - .NET Framework 4.x required & exit /b 1 )
"%CSC%" /nologo /target:winexe /out:"%~dp0FleetCockpit.exe" /r:"%WPF%\PresentationFramework.dll" /r:"%WPF%\PresentationCore.dll" /r:"%WPF%\WindowsBase.dll" /r:"%FW%\System.Xaml.dll" /r:"%FW%\System.Web.Extensions.dll" "%~dp0FleetCockpit.cs"
if errorlevel 1 ( echo BUILD FAILED & exit /b 1 )
echo BUILD OK: %~dp0FleetCockpit.exe
start "" "%~dp0FleetCockpit.exe"
