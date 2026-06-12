"""One-shot status snapshot: fleet_runner processes, status.json workers, orchestrator log tail,
and WSL docker activity (running containers + any swebench eval process)."""
import json, os, subprocess

REPO = r"C:\Users\USER\companion-mcp"
DISTRO = "MiasmaLab"

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")

def wsl(s):
    return run(["wsl.exe", "-d", DISTRO, "sh", "-c", s])

# 1. fleet_runner procs
ps = run(["powershell", "-NoProfile", "-Command",
          "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
          "Where-Object { $_.CommandLine -like '*relay.fleet_runner*' } | "
          "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"])
fl = [l for l in (ps.stdout or "").splitlines() if l.strip()]
print("=== fleet_runner procs: %d ===" % len(fl))
for l in fl:
    print("  ", l[:130])

# 2. status.json
try:
    d = json.load(open(os.path.join(REPO, ".fleet", "status.json"), encoding="utf-8"))
    print("=== status.json: running=%s done=%s/%s ===" % (d.get("running"), d.get("done_count"), d.get("total")))
    for w in d.get("workers", []):
        inst = w.get("cwd", "").split("wt_")[-1]
        print("   %s[%s] status=%s outcome=%s turn=%s verify=%s %s" % (
            w.get("name"), inst, w.get("status"), w.get("outcome"),
            w.get("turn"), w.get("verify_attempts"), (w.get("reason") or "")[:45]))
except Exception as e:
    print("=== status.json unreadable:", e)

# 3. orchestrator log tail
logp = os.path.join(REPO, ".fleet", "swe", "run_until_done.log")
if os.path.exists(logp):
    tail = open(logp, encoding="utf-8").read().splitlines()[-8:]
    print("=== run_until_done.log tail ===")
    for l in tail:
        print("  ", l)
else:
    print("=== no run_until_done.log ===")

# 4. WSL docker activity
print("=== WSL docker ===")
c = wsl("docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | head -8")
print("running containers:\n", (c.stdout or "(none)").rstrip())
e = wsl("pgrep -fa run_evaluation 2>/dev/null | head -4")
print("swebench eval procs:\n", (e.stdout or "(none)").rstrip())
