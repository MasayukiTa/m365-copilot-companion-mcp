"""Deploy the rebuilt WPF UI: stop the running CopilotChat.exe / FleetCockpit.exe (so the locked
files can be overwritten), rebuild both to their real paths via the existing build .bat files
(which also relaunch them), and confirm both came back up."""
import os, subprocess, time

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")

for img in ["CopilotChat.exe", "FleetCockpit.exe"]:
    r = subprocess.run(["taskkill", "/F", "/IM", img], capture_output=True, text=True, errors="replace")
    print("kill", img, "->", (r.stdout or r.stderr).strip()[:70])
time.sleep(2)

for bat in ["build_and_run.bat", "build_cockpit.bat"]:
    r = subprocess.run(["cmd", "/c", os.path.join(UI, bat)], capture_output=True, text=True,
                       errors="replace", cwd=UI)
    print("===", bat, "rc=", r.returncode)
    out = (r.stdout or "") + (r.stderr or "")
    for l in out.splitlines():
        if "BUILD" in l or "error" in l.lower() or "ERROR" in l:
            print("   ", l.strip())
time.sleep(3)

for img in ["CopilotChat.exe", "FleetCockpit.exe"]:
    t = subprocess.run(["tasklist", "/fi", "imagename eq " + img], capture_output=True,
                       text=True, errors="replace")
    print(img, "RUNNING" if img in (t.stdout or "") else "NOT running")
