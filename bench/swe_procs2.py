"""Show every python process whose command line mentions the SWE orchestrator or fleet_runner,
so we can tell if the self-driving run is alive, hung, or dead."""
import subprocess

ps = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
     "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
    capture_output=True, text=True, errors="replace")
lines = [l for l in (ps.stdout or "").splitlines() if l.strip()]
orch = [l for l in lines if "swe_run_until_done" in l]
fleet = [l for l in lines if "relay.fleet_runner" in l]
print("orchestrator procs:", len(orch))
for l in orch:
    print("  ", l[:140])
print("fleet_runner procs:", len(fleet))
for l in fleet:
    print("  ", l[:140])
