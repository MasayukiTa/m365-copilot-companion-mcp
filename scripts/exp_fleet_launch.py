# -*- coding: utf-8 -*-
"""Launches ONE isolated fleet run of the sibling-duplication / fan-out experiment. One goal, one arm,
one state directory, one process; the operator starts the next run only when this one has ended.

ISOLATION (all enforced here, none by convention)
  * the tools run on a PRIVATE copy of the local MCP server (scripts/probes/preflight_ab_server.py) on
    port 8100 with a random key made for this experiment; relay.tool_bridge.LOCAL_MCP_URL is pointed
    at it in THIS process only. That server's tool ledger is this checkout's .fleet/tool_events.jsonl,
    so the production ledger never receives a row and the production server never receives a call;
  * the browser is the hidden Edge on its own CDP port (default 9340); ports 8000, 8765 and 9222-9224
    are refused by name, for the server and for the browser;
  * the state directory is new for every run and lives under the experiment root; the checkout is a
    worktree whose settings (.config/settings.txt) are written by `prepare`, not shared;
  * the watchdog and the pre-run Edge recycle are OFF (--stall-s 0 --no-recycle): a reset aimed at the
    wrong profile stops the production Edge, which was observed in the earlier isolated comparison.
    The run's own timeout replaces the watchdog; on expiry the process ends itself;
  * a run is refused when C: has under 3 GB free, and the generated data is hashed before and after
    the run (workers can write inside the experiment root; a changed file spoils the answers).

Nothing here prints the agent address or a token.

  python scripts/exp_fleet_launch.py prepare --root X --donor-url-file F --url-file F
  python scripts/exp_fleet_launch.py server  --root X            (start the private server, leave it running)
  python scripts/exp_fleet_launch.py run     --root X --label L --url-file F [--timeout-min 90]
  python scripts/exp_fleet_launch.py stop    --root X            (stop only the private server we started)
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORBIDDEN_PORTS = (8000, 8765, 9222, 9223, 9224)
SERVER_PORT = 8100
CDP_PORT = 9340
MIN_FREE_GB = 3.0
MAX_CONCURRENT = 3
#: Server settings the production server carries (non-secret). Without MCP_TOOL_MAP the `call_tool`
#: gateway is not registered at all. Copied from scripts/probes/preflight_ab.py, which proved them on 8100.
SERVER_SETTINGS = {"MCP_TOOL_MAP": "1", "MCP_TOOL_MAP_MAX": "13", "MCP_TOOL_MAP_INCLUDE": "list_directory",
                   "MCP_EXECUTION_PROFILES": "1", "MCP_REQUIRE_UNLOCK_TOKEN": "1", "MCP_EXT_HELPER": "1",
                   "MCP_UNLOCK_REQUIRED": "0"}
#: Settings this checkout's .config/settings.txt must carry. Depth 1 keeps one fan-out campaign per run
#: (a nested split would make the pairs incomparable and the cost unbounded).
REQUIRED_SETTINGS = {"tool_transport": "connector-less", "fanout_max_depth": "1", "maxtabs": str(MAX_CONCURRENT)}


# --------------------------------------------------------------------------- pure helpers

def check_ports(server_port, cdp_port):
    """Raises ValueError naming the first forbidden or duplicated port."""
    for name, p in (("server", server_port), ("browser", cdp_port)):
        if p in FORBIDDEN_PORTS:
            raise ValueError("refused: %s port %d belongs to production" % (name, p))
    if server_port == cdp_port:
        raise ValueError("refused: server and browser cannot share port %d" % server_port)


def disk_ok(path, floor_gb=MIN_FREE_GB):
    return shutil.disk_usage(path).free / 1e9 >= floor_gb


def fleet_args(arm, state_dir, goals_file, max_concurrent=MAX_CONCURRENT, effort="min"):
    """The fleet_runner command line. The fan-out flag is passed EXPLICITLY in both arms, so the
    only difference between the two arms is that one flag (the CLI wins over settings.txt)."""
    if arm not in ("on", "off"):
        raise ValueError("arm must be on or off")
    return ["--state-dir", state_dir, "--goals-file", goals_file, "--max-concurrent", str(max_concurrent),
            "--effort", effort, "--stall-s", "0", "--no-recycle",
            "--fanout" if arm == "on" else "--no-fanout"]


def settings_problems(text):
    """Required keys that are missing or have another value in a settings.txt body."""
    have = {}
    for ln in (text or "").splitlines():
        if "=" in ln and not ln.strip().startswith("#"):
            k, v = ln.split("=", 1)
            have[k.strip()] = v.strip()
    return ["%s=%s (found %s)" % (k, v, have.get(k)) for k, v in REQUIRED_SETTINGS.items() if have.get(k) != v]


def settings_body():
    return "".join("%s=%s\n" % kv for kv in REQUIRED_SETTINGS.items())


def port_open(port, timeout=2.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _append(path, row):
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_url(path):
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read().strip()


def _lower_priority():
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


# --------------------------------------------------------------------------- commands

def cmd_prepare(a):
    """Write this checkout's settings and seed the request template for the experiment's agent."""
    path = os.path.join(REPO, ".config", "settings.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cur = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            cur = fh.read()
    if settings_problems(cur):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(settings_body() + "".join(ln + "\n" for ln in cur.splitlines()
                                               if "=" in ln and ln.split("=", 1)[0].strip() not in REQUIRED_SETTINGS))
    print("settings:", "ok" if not settings_problems(open(path, encoding="utf-8-sig").read()) else "NOT ok")
    from relay import profile_token as PT
    from relay import template_seed as TS
    donor = _read_url(a.donor_url_file)
    os.makedirs(PT.TEMPLATE_DIR, exist_ok=True)
    src = PT.template_path(donor, a.donor_template_dir) if a.donor_template_dir else ""
    if src and os.path.exists(src) and not os.path.exists(PT.template_path(donor)):
        shutil.copyfile(src, PT.template_path(donor))
    out = TS.seed(donor, _read_url(a.url_file), PT.TEMPLATE_DIR)
    print("template written:", os.path.basename(out))
    return 0


def cmd_server(a):
    check_ports(SERVER_PORT, CDP_PORT)
    root = os.path.abspath(a.root)
    pidf = os.path.join(root, "server.pid")
    if os.path.exists(pidf) and port_open(SERVER_PORT):
        print("private server already answers on %d" % SERVER_PORT)
        return 0
    if port_open(SERVER_PORT):
        print("refused: something else already listens on %d" % SERVER_PORT)
        return 3
    key = secrets.token_hex(24)
    with open(os.path.join(root, "server.key"), "w", encoding="ascii") as fh:
        fh.write(key)
    env = dict(os.environ, MCP_API_KEY=key, PYTHONUTF8="1", MCP_ALLOWED_BASE=root)
    env.update(SERVER_SETTINGS)
    env.pop("MCP_API_KEY_PROTECTED", None)
    from tools.childproc import headless_creationflags
    logf = open(os.path.join(root, "server.log"), "ab")
    proc = subprocess.Popen([sys.executable, "-I", "-X", "utf8",
                             os.path.join(REPO, "scripts", "probes", "preflight_ab_server.py"), str(SERVER_PORT)],
                            cwd=REPO, env=env, stdout=logf, stderr=logf, creationflags=headless_creationflags())
    with open(pidf, "w", encoding="ascii") as fh:
        fh.write(str(proc.pid))
    t0 = time.time()
    while time.time() - t0 < 180:
        if proc.poll() is not None:
            print("private server exited early (see server.log)")
            return 4
        if port_open(SERVER_PORT):
            print("private server up on %d, pid %d" % (SERVER_PORT, proc.pid))
            return 0
        time.sleep(3)
    print("private server did not answer within 180 s")
    return 5


def cmd_stop(a):
    root = os.path.abspath(a.root)
    pidf = os.path.join(root, "server.pid")
    try:
        pid = int(open(pidf, encoding="ascii").read().strip())
    except (OSError, ValueError):
        print("no server.pid; nothing of ours to stop")
        return 0
    from tools import childproc
    r = childproc.run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=30,
                      creationflags=childproc.headless_creationflags())
    os.remove(pidf)
    print("stopped pid %d (exit %s)" % (pid, r.returncode))
    return 0


def _record(root, row):
    _append(os.path.join(root, "runs.jsonl"), row)


def cmd_run(a):
    check_ports(SERVER_PORT, CDP_PORT)
    root = os.path.abspath(a.root)
    with open(os.path.join(root, "plan.json"), encoding="utf-8") as fh:
        run = next((r for r in json.load(fh)["runs"] if r["label"] == a.label), None)
    if run is None:
        print("unknown label %s" % a.label)
        return 2
    from scripts import sibling_dup_report as S
    done = S._read_jsonl(os.path.join(root, "runs.jsonl"))
    if any(r.get("label") == a.label for r in done):
        print("refused: %s already ran (one attempt per label; a failed run is kept, not overwritten)" % a.label)
        return 2
    if os.path.exists(run["state_dir"]) and os.listdir(run["state_dir"]):
        print("refused: state directory is not empty")
        return 2
    if not disk_ok(root):
        print("STOP: C: has under %.1f GB free" % MIN_FREE_GB)
        return 3
    with open(os.path.join(REPO, ".config", "settings.txt"), encoding="utf-8-sig") as fh:
        bad = settings_problems(fh.read())
    if bad:
        print("STOP: settings.txt differs from the experiment's: " + "; ".join(bad))
        return 3
    if not port_open(SERVER_PORT):
        print("STOP: the private server is not up on %d (run `server` first)" % SERVER_PORT)
        return 3
    if not port_open(CDP_PORT):
        print("STOP: the hidden Edge is not listening on %d" % CDP_PORT)
        return 3
    from scripts import exp_public_data as D
    ok0, bad0 = D.verify_data(os.path.join(root, "data"))
    if not ok0:
        print("STOP: generated data already changed: %s" % ", ".join(bad0[:5]))
        return 3
    with open(os.path.join(root, "data", "goals.json"), encoding="utf-8") as fh:
        goal = next(g for g in json.load(fh) if g["goal_id"] == run["goal_id"])
    gdir = os.path.join(root, "goal_files")
    os.makedirs(gdir, exist_ok=True)
    gfile = os.path.join(gdir, a.label + ".txt")
    with open(gfile, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(goal["text"] + "\n")

    key = open(os.path.join(root, "server.key"), encoding="ascii").read().strip()
    os.environ["MCP_API_KEY"] = key
    os.environ.pop("MCP_API_KEY_PROTECTED", None)
    os.environ["MCP_FLEET_AGENT_URL"] = _read_url(a.url_file)
    os.environ["MCP_CDP_URL"] = "http://localhost:%d" % CDP_PORT
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.chdir(REPO)
    sys.path.insert(0, REPO)
    from relay import tool_bridge as TB
    TB.LOCAL_MCP_URL = "http://127.0.0.1:%d/mcp" % SERVER_PORT
    assert ":8000" not in TB.LOCAL_MCP_URL
    TB._instructions_cache.clear()
    _lower_priority()

    t_start = time.time()
    state = {"written": False}
    lock = threading.Lock()

    def finish(code):
        with lock:
            if state["written"]:
                return
            state["written"] = True
        ok1, bad1 = D.verify_data(os.path.join(root, "data"))
        _record(root, {"label": a.label, "goal_id": run["goal_id"], "arm": run["arm"], "ts_start": t_start,
                       "ts_end": time.time(), "exit_code": code, "data_intact": ok1,
                       "changed": bad1[:5], "free_gb_after": round(shutil.disk_usage(root).free / 1e9, 2)})

    def on_timeout():
        finish(124)
        os._exit(124)

    timer = threading.Timer(a.timeout_min * 60.0, on_timeout)
    timer.daemon = True
    timer.start()
    sys.argv = ["relay.fleet_runner"] + fleet_args(run["arm"], run["state_dir"], gfile)
    import runpy
    code = 0
    try:
        runpy.run_module("relay.fleet_runner", run_name="__main__", alter_sys=True)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    timer.cancel()
    finish(code)
    print("run %s ended with exit code %s" % (a.label, code))
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", required=True)
    p.add_argument("--donor-url-file", required=True)
    p.add_argument("--donor-template-dir", default="")
    p.add_argument("--url-file", required=True)
    s = sub.add_parser("server")
    s.add_argument("--root", required=True)
    r = sub.add_parser("run")
    r.add_argument("--root", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--url-file", required=True)
    r.add_argument("--timeout-min", type=float, default=90.0)
    t = sub.add_parser("stop")
    t.add_argument("--root", required=True)
    a = ap.parse_args(argv)
    return {"prepare": cmd_prepare, "server": cmd_server, "run": cmd_run, "stop": cmd_stop}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
