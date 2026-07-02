"""Diagnose the two failed pilot workers: show each worktree's current diff (files + sizes),
the predictions file, and the latest swebench eval report from WSL (resolved/unresolved + the
test log tail). Read-only."""
import subprocess, os, json

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISTRO = "MiasmaLab"

def wsl(script, timeout=120):
    return subprocess.run(["wsl.exe", "-d", DISTRO, "sh", "-c", script],
                          capture_output=True, text=True, timeout=timeout)

for inst in ["astropy__astropy-14182", "astropy__astropy-14365"]:
    wt = os.path.join(REPO, ".fleet", "swe", "work", "wt_" + inst)
    print("=" * 72)
    print(inst)
    g = subprocess.run(["git", "-C", wt, "diff"], capture_output=True, text=True)
    diff = g.stdout
    print("  diff_chars =", len(diff))
    for ln in diff.splitlines():
        if ln.startswith("diff --git"):
            print("   ", ln)
    pf = os.path.join(REPO, ".fleet", "swe", "preds", inst + ".json")
    print("  pred file exists:", os.path.exists(pf))
    run_id = "agent_" + inst.replace("__", "_")
    r = wsl("cat /root/swe/companion." + run_id + ".json 2>/dev/null")
    out = (r.stdout or "").strip()
    if out:
        try:
            d = json.loads(out)
            print("  resolved_ids:", d.get("resolved_ids"))
            print("  unresolved_ids:", d.get("unresolved_ids"))
            print("  error_ids:", d.get("error_ids"))
        except Exception as e:
            print("  report parse error:", e, out[:200])
    else:
        print("  no eval report found for run_id", run_id)
