"""Show the full agent diff for each failed worker and the tail of the swebench test log so we
can see the real assertion/traceback that the agent never received as feedback."""
import subprocess, os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISTRO = "MiasmaLab"

def wsl(script, timeout=120):
    from tools.childproc import run as _run_child
    return _run_child(["wsl.exe", "-d", DISTRO, "sh", "-c", script], timeout=timeout)

for inst in ["astropy__astropy-14182", "astropy__astropy-14365"]:
    wt = os.path.join(REPO, ".fleet", "swe", "work", "wt_" + inst)
    print("#" * 78)
    print("#", inst)
    print("#" * 78)
    from tools.childproc import run as _run_child
    g = _run_child(["git", "-C", wt, "diff"])
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
