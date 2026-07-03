# start_brain_tunnel.ps1
# Open the SSH REVERSE tunnel that lets the Minecraft bot on kiyus reach the local
# brain proxy WITHOUT any key ever leaving this machine:
#
#   kiyus 127.0.0.1:8012  --[ssh -R]-->  <home> 127.0.0.1:8012 (brain_proxy.py)
#
# The remote bind is loopback-only on kiyus, so nothing on kiyus' network can reach
# the proxy except local processes. Idempotent: if a matching ssh tunnel process is
# already running, this script reports it and exits without starting a second one.
#
# Usage:
#   .\start_brain_tunnel.ps1            # start (or reuse) the tunnel, detached
#   .\start_brain_tunnel.ps1 -Status    # only report whether a tunnel is up
#
# NOTE: run start_brain_path.ps1 FIRST so :8012 is actually listening locally;
# otherwise the tunnel is up but every bot request dies at this end.

param(
    [string]$SshHost   = "EVAL_HOST",
    [int]$Port         = 8012,
    [switch]$Status
)

$ErrorActionPreference = "Stop"

$forwardSpec = "127.0.0.1:${Port}:127.0.0.1:${Port}"

# --- detect an existing tunnel (ssh.exe whose command line carries our -R spec) ---
function Get-ExistingTunnel {
    Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match [regex]::Escape($forwardSpec) -and
            $_.CommandLine -match [regex]::Escape($SshHost)
        }
}

$existing = @(Get-ExistingTunnel)
if ($existing.Count -gt 0) {
    Write-Host ("[brain-tunnel] already running: ssh PID {0} (-R {1} {2})" -f `
        $existing[0].ProcessId, $forwardSpec, $SshHost)
    exit 0
}
if ($Status) {
    Write-Host "[brain-tunnel] no tunnel process found"
    exit 1
}

# --- warn if the local proxy end is not listening yet (tunnel would be useless) ---
$local = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $local) {
    Write-Warning ("[brain-tunnel] nothing is listening on 127.0.0.1:{0} locally. " -f $Port)
    Write-Warning "Run scripts\start_brain_path.ps1 first, or bot requests will fail at this end."
}

# --- start the tunnel, detached and windowless ---
# ExitOnForwardFailure: die loudly if kiyus' :8012 loopback is already taken,
# instead of holding a half-broken session. ServerAlive keeps NAT/idle paths open.
$sshArgs = @(
    "-N",
    "-R", $forwardSpec,
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=4",
    $SshHost
)
$proc = Start-Process ssh -ArgumentList $sshArgs -WindowStyle Hidden -PassThru

# Give ssh a moment to fail fast (auth error / forward collision), then confirm.
Start-Sleep -Seconds 5
if ($proc.HasExited) {
    Write-Error ("[brain-tunnel] ssh exited immediately (code {0}). Check ssh connectivity to {1} and whether kiyus 127.0.0.1:{2} is already bound." -f $proc.ExitCode, $SshHost, $Port)
    exit 1
}
Write-Host ("[brain-tunnel] up: ssh PID {0}  (-R {1} -> local brain_proxy)" -f $proc.Id, $forwardSpec)
exit 0
