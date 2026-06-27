@echo off
REM Build the native WPF chat with the C# compiler that ships with Windows.
REM No Visual Studio, no .NET SDK, no Node. Then launch it.
setlocal
set "CSC=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
set "WPF=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF"
set "XAML=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Xaml.dll"
if not exist "%CSC%" ( echo ERROR: csc.exe not found - .NET Framework 4.x required & exit /b 1 )
set "WEBEXT=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Web.Extensions.dll"
"%CSC%" /nologo /target:winexe /out:"%~dp0CopilotChat.exe" /r:"%WPF%\PresentationFramework.dll" /r:"%WPF%\PresentationCore.dll" /r:"%WPF%\WindowsBase.dll" /r:"%XAML%" /r:"%WEBEXT%" "%~dp0CopilotChat.cs" "%~dp0Markdown.cs" "%~dp0Theme.cs"
if errorlevel 1 ( echo BUILD FAILED & exit /b 1 )
echo BUILD OK: %~dp0CopilotChat.exe
start "" "%~dp0CopilotChat.exe"
