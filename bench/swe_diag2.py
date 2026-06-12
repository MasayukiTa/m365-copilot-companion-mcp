"""Show the full agent diff for each failed worker and the tail of the swebench test log so we
can see the real assertion/traceback that the agent never received as feedback."""
import subprocess, os

REPO = r"C:\Users\USER\companion-mcp"
DISTRO = "MiasmaLab"

def wsl(script, timeout=120):
    return subprocess.run(["wsl.exe", "-d", DISTRO, "sh", "-c", script],
                          capture_output=True, text=True, timeout=timeout)

for inst in ["astropy__astropy-14182", "astropy__astropy-14365"]:
    wt = os.path.join(REPO, ".fleet", "swe", "work", "wt_" + inst)
    print("#" * 78)
    print("#", inst)
    print("#" * 78)
    g = subprocess.run(["git", "-C", wt, "diff"], capture_output=True, text=True)
    print("----- AGENT DIFF -----")
    print(g.stdout)
    run_id = "agent_" + inst.replace("__", "_")
    # find the test_output log
    find = wsl("ls /root/swe/logs/run_evaluation/" + run_id + "/companion/" + inst + "/ 2>/dev/null")
    print("----- log dir contents -----")
    print(find.stdout, find.stderr)
    tail = wsl("tail -60 /root/swe/logs/run_evaluation/" + run_id + "/companion/" + inst + "/test_output.txt 2>/dev/null")
    print("----- test_output.txt tail -----")
    print(tail.stdout)
