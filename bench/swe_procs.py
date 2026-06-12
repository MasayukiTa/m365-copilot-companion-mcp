"""List python processes with their command lines (via PowerShell Win32_Process) and flag any
that are running fleet_runner / relay_fleet, to detect a duplicate fleet run."""
import subprocess

ps = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
     "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
    capture_output=True, text=True, errors="replace")
lines = [l for l in (ps.stdout or "").splitlines() if l.strip()]
fleet = [l for l in lines if "fleet_runner" in l or "relay_fleet" in l]
print("total python procs:", len(lines))
print("fleet_runner/relay_fleet procs:", len(fleet))
for l in fleet:
    print("  ", l[:160])
