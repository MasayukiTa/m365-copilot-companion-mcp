# run_sandbox.ps1 -- HOST side of the fresh-PC install-path check (see README.md here).
#
# Builds the sandbox's source from `git archive HEAD` ONLY (never the working tree: no .env,
# .setup, .fleet, .venv or other local state can reach the sandbox), writes a .wsb that maps
# it read-only plus a writable results folder, starts Windows Sandbox, waits for the driver's
# DONE.json, then closes the sandbox.
#
# ASCII ONLY (Windows PowerShell 5.1 reads BOM-less scripts in the ANSI code page).
param(
    [Parameter(Mandatory = $true)][ValidateSet("AC", "B", "C")][string]$Scenario,
    [Parameter(Mandatory = $true)][string]$Work,
    [string]$Repo = "",
    [int]$TimeoutMin = 60,
    [int]$MemoryMB = 4096
)
$ErrorActionPreference = "Stop"
if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
$SandboxProcs = @("WindowsSandbox", "WindowsSandboxClient", "WindowsSandboxRemoteSession", "WindowsSandboxServer")

$exe = Join-Path $env:SystemRoot "System32\WindowsSandbox.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "Windows Sandbox is not available: $exe is missing." }
if (Get-Process -Name $SandboxProcs -ErrorAction SilentlyContinue) {
    throw "A Windows Sandbox is already running; only one can run at a time. Close it first."
}

# A sandbox that was just closed keeps its VM (vmmemWindowsSandbox) -- and its hold on the
# previous mapped folder -- for a while; a new one started meanwhile is refused. Wait it out.
$vmWait = Get-Date
while (Get-Process -Name "vmmemWindowsSandbox" -ErrorAction SilentlyContinue) {
    if (((Get-Date) - $vmWait).TotalMinutes -gt 5) { throw "the previous sandbox VM is still shutting down after 5 min" }
    Start-Sleep -Seconds 5
}

New-Item -ItemType Directory -Force -Path $Work | Out-Null
$Work = (Get-Item -LiteralPath $Work).FullName
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
# One mapped tree per run: never reused, so nothing a previous sandbox still holds is in the way.
$map = Join-Path $Work "map-$Scenario-$stamp"
New-Item -ItemType Directory -Force -Path (Join-Path $map "src") | Out-Null

$zip = Join-Path $Work "src-$stamp.zip"
& git -C $Repo archive --format=zip -o $zip HEAD
if ($LASTEXITCODE -ne 0) { throw "git archive failed ($LASTEXITCODE)" }
$head = (& git -C $Repo rev-parse HEAD).Trim()
Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $map "src") -Force
foreach ($bad in ".env", ".setup", ".fleet", ".venv") {
    if (Test-Path -LiteralPath (Join-Path $map "src\$bad")) { throw "git archive unexpectedly carries $bad; refusing." }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "sandbox_driver.ps1") -Destination $map -Force

$results =Join-Path $Work "results-$Scenario-$stamp"
New-Item -ItemType Directory -Force -Path $results | Out-Null
Set-Content -LiteralPath (Join-Path $results "host.txt") -Encoding ASCII -Value @("head=$head", "scenario=$Scenario", "started=$(Get-Date -Format o)")

$cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\harness\sandbox_driver.ps1 -Scenario $Scenario -Harness C:\harness -Results C:\results"
$wsbXml = @"
<Configuration>
  <Networking>Enable</Networking>
  <vGPU>Disable</vGPU>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <PrinterRedirection>Disable</PrinterRedirection>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <MemoryInMB>$MemoryMB</MemoryInMB>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$([System.Security.SecurityElement]::Escape($map))</HostFolder>
      <SandboxFolder>C:\harness</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$([System.Security.SecurityElement]::Escape($results))</HostFolder>
      <SandboxFolder>C:\results</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>$([System.Security.SecurityElement]::Escape($cmd))</Command>
  </LogonCommand>
</Configuration>
"@
$wsb = Join-Path $Work "install-path-$Scenario.wsb"
Set-Content -LiteralPath $wsb -Value $wsbXml -Encoding ASCII

Write-Host "results: $results"
$t0 = Get-Date
Start-Process -FilePath $exe -ArgumentList ('"' + $wsb + '"') | Out-Null
$done = Join-Path $results "DONE.json"
$progress = Join-Path $results "progress.log"
$lastLen = 0
try {
    while (-not (Test-Path -LiteralPath $done)) {
        if (((Get-Date) - $t0).TotalMinutes -gt $TimeoutMin) { Write-Host "TIMEOUT after $TimeoutMin min"; break }
        # A sandbox that closes or crashes before DONE.json is a result too: say when.
        if (((Get-Date) - $t0).TotalMinutes -gt 2 -and -not (Get-Process -Name $SandboxProcs -ErrorAction SilentlyContinue)) {
            $msg = "SANDBOX GONE before DONE.json at $(Get-Date -Format o)"
            Write-Host $msg
            Add-Content -LiteralPath (Join-Path $results "host.txt") -Value $msg
            break
        }
        if (Test-Path -LiteralPath $progress) {
            $lines = @(Get-Content -LiteralPath $progress -ErrorAction SilentlyContinue)
            if ($lines.Count -gt $lastLen) { $lines[$lastLen..($lines.Count - 1)] | ForEach-Object { Write-Host $_ }; $lastLen = $lines.Count }
        }
        Start-Sleep -Seconds 10
    }
    if (Test-Path -LiteralPath $done) { Write-Host ("DONE: " + (Get-Content -Raw -LiteralPath $done)) }
} finally {
    Add-Content -LiteralPath (Join-Path $results "host.txt") -Value ("host_wait_min={0:N1}" -f ((Get-Date) - $t0).TotalMinutes)
    # Only the sandbox's own processes; nothing else on this PC is touched.
    Get-Process -Name $SandboxProcs -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host "sandbox closed"
}
