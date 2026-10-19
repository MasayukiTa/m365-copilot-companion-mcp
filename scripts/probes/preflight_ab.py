# -*- coding: utf-8 -*-
"""A/B of the first-turn preflight (MCP_BRIDGE_PREFLIGHT=1 vs 0) on the connector-less route.

Differs from the earlier turn_breakdown harness in the one way that matters: it drives a real
`SocketRoute` driver (relay.socket_driver.CopilotSocketDriver, equipped by relay.tool_bridge.equip),
so the first turn gets `_first_turn_prefix` -- the preflight -- exactly as in the product. The old
harness called Conversation.ask directly and skipped it.

Isolation (all enforced below):
  * its own copy of the MCP server on port 8100 (scripts/probes/preflight_ab_server.py), with a
    random API key made for this run; tool_bridge.LOCAL_MCP_URL is pointed at it in THIS process only
  * its own template copy under the output folder; the production .fleet is only read (one file copy)
  * refuses ports 8000, 8765, 9222-9224; the token comes only from the hidden Edge (bgedge) on 9350
  * write tools stay denied, Outlook tools are refused
  * logs and results are small and go under --out only; stops when C: has under 3 GB free

usage (use the repo venv, python -I -X utf8):
  preflight_ab.py smoke   --out DIR                       no Copilot turn, no token; checks the wiring
  preflight_ab.py run     --out DIR --url-file F [--n 10] [--goals a,b,c]
  preflight_ab.py summary --out DIR                       medians and spread from results.jsonl
  preflight_ab.py token   --url-file F                    (internal) print one fresh token
Never prints or logs the agent URL or a token.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PORT = 8100
FORBIDDEN_PORTS = (8000, 8765, 9222, 9223, 9224)
EDGE_PORT = 9350
MIN_FREE_GB = 3.0
DONOR_NAME = "template_443df19d202e0936.json"     # read-only source of the request shape
PY = sys.executable

DENY = ("write_file", "edit_and_verify", "delete", "roll_back", "fleet_submit", "restore_point", "unlock",
        "office_password", "commit_turn", "claim_turn")
SERVER_SETTINGS = {"MCP_ALLOWED_BASE": "*", "MCP_TOOL_MAP": "1", "MCP_TOOL_MAP_MAX": "13",
                   "MCP_TOOL_MAP_INCLUDE": "list_directory", "MCP_EXECUTION_PROFILES": "1",
                   "MCP_REQUIRE_UNLOCK_TOKEN": "1", "MCP_EXT_HELPER": "1", "MCP_UNLOCK_REQUIRED": "0"}
SENT_GOALS =("a", "b", "c")

URL_RE = re.compile(r"(?:https?|wss?)://\S+")
TOK_RE = re.compile(r"eyJ[A-Za-z0-9_\-\.]{20,}")


# ---------------------------------------------------------------- pure helpers (unit-tested)

def scrub(s):
    return TOK_RE.sub("<tok>", URL_RE.sub("<url>", str(s)))[:240]


def arm_order(i):
    """Alternate which arm goes first, so time-of-day drift hits both arms alike."""
    return ("on", "off") if i % 2 == 1 else ("off", "on")


def is_denied(inner_name):
    low = str(inner_name or "").strip().lower()
    if low.startswith("outlook"):
        return "refused: outlook tools are never run"
    if any(low.startswith(d) for d in DENY):
        return "refused: mutating tool not allowed in this measurement"
    return ""


def disk_free_gb(path="C:\\"):
    return shutil.disk_usage(path).free / 2 ** 30


def disk_ok(free_gb, floor=MIN_FREE_GB):
    return free_gb >= floor


def spread(values):
    """median / min / max / quartiles of a list, or None when empty."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    q = statistics.quantiles(v, n=4, method="inclusive") if len(v) >= 2 else [v[0]] * 3
    return {"n": len(v), "median": round(statistics.median(v), 2), "min": round(v[0], 2),
            "max": round(v[-1], 2), "q1": round(q[0], 2), "q3": round(q[2], 2)}


def summarize(rows):
    """{goal: {arm: {wall, turns, tools, ok_n, n}}} from result rows (event == 'run')."""
    out = {}
    for r in rows:
        if r.get("event") != "run":
            continue
        d = out.setdefault(r["goal"], {}).setdefault(r["arm"], {"wall": [], "turns": [], "tools": [],
                                                                  "ok": 0, "n": 0, "pre": []})
        d["n"] += 1
        d["ok"] += 1 if r.get("ok") else 0
        d["wall"].append(r.get("wall_s"))
        d["turns"].append(r.get("turns"))
        d["tools"].append(r.get("tool_calls"))
        if r.get("preflight_s") is not None:
            d["pre"].append(r.get("preflight_s"))
    res = {}
    for g, arms in out.items():
        res[g] = {}
        for a, d in arms.items():
            res[g][a] = {"n": d["n"], "ok": d["ok"], "wall": spread(d["wall"]), "turns": spread(d["turns"]),
                         "tools": spread(d["tools"]), "pre": spread(d["pre"])}
    return res


def goal_text(goal, root=ROOT):
    return {
        "a": "Read the first 5 lines of %s\\README.md and say how many characters they have." % root,
        "b": "Read the first 3 lines of each of README.md, LICENSE and main.py under %s and list the three first words." % root,
        "c": "Find the largest .py file under %s\\tools (by line count), read its first 3 lines, and report both." % root,
    }[goal]


def expected(goal, root=ROOT):
    try:
        if goal == "a":
            with open(os.path.join(root, "README.md"), encoding="utf-8", errors="replace", newline="") as f:
                ls = [f.readline() for _ in range(5)]
            return {"chars_with_nl": sum(len(x) for x in ls),
                    "chars_no_nl": sum(len(x.rstrip("\r\n")) for x in ls)}
        if goal == "c":
            best = None
            for d, _, fs in os.walk(os.path.join(root, "tools")):
                for fn in fs:
                    if fn.endswith(".py"):
                        p = os.path.join(d, fn)
                        n = sum(1 for _ in open(p, encoding="utf-8", errors="replace"))
                        if best is None or n > best[1]:
                            best = (p, n)
            return {"file": os.path.basename(best[0]), "lines": best[1]}
    except Exception as e:
        return {"err": scrub(e)}
    return {}


def check(goal, ans, exp):
    a = (ans or "").replace(",", "")
    if goal == "a":
        return any(str(v) in a for k, v in exp.items() if k.startswith("chars"))
    if goal == "c":
        return exp.get("file", "\0") in a and str(exp.get("lines")) in a
    if goal == "b":
        return len(a) > 10      # a loose check, as in the earlier harness; not a judgement of quality
    return False


def log(path, row):
    row["t"] = round(time.time(), 1)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_rows(path):
    rows = []
    if os.path.isfile(path):
        for ln in open(path, encoding="utf-8"):
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
    return rows


# ---------------------------------------------------------------- environment

def lower_priority():
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def start_server(out_dir, key):
    """Start the private server on 8100 (our own process, BelowNormal) and wait until it answers."""
    assert PORT not in FORBIDDEN_PORTS
    env = dict(os.environ, MCP_API_KEY=key, PYTHONUTF8="1")
    # The same non-secret server settings the production .env carries (names and values read with a
    # key-only grep): without MCP_TOOL_MAP the `call_tool` gateway is not registered at all.
    env.update(SERVER_SETTINGS)
    env.pop("MCP_API_KEY_PROTECTED", None)
    logf = open(os.path.join(out_dir, "server.log"), "ab")
    from tools.childproc import headless_creationflags
    proc = subprocess.Popen([PY, "-I", "-X", "utf8", os.path.join(HERE, "preflight_ab_server.py"), str(PORT)],
                            cwd=ROOT, env=env, stdout=logf, stderr=logf,
                            creationflags=headless_creationflags())
    return proc


def git_head_short():
    from tools import childproc
    r = childproc.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"], timeout=30,
                      creationflags=childproc.headless_creationflags())
    return (r.stdout or "").strip()


def donor_template_path():
    """The request-shape template to COPY: the main checkout's record directory, found through git
    (the worktree's common dir), so no checkout name is written into this file."""
    from tools import childproc
    r = childproc.run(["git", "-C", ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                      timeout=30, creationflags=childproc.headless_creationflags())
    main_root = os.path.dirname((r.stdout or "").strip())
    return os.path.join(main_root, ".fleet", "templates", DONOR_NAME)


def wait_server(proc, timeout_s=120):
    from relay import tool_bridge as TB
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if proc.poll() is not None:
            return ""
        TB._instructions_cache.clear()
        text = TB.server_instructions(ttl_s=3600)
        if text:
            return text
        time.sleep(3)
    return ""


def point_bridge_at_private_server(key):
    """In THIS process only: tool_bridge talks to port 8100 with our own key."""
    from relay import tool_bridge as TB
    os.environ["MCP_API_KEY"] = key
    os.environ.pop("MCP_API_KEY_PROTECTED", None)
    TB.LOCAL_MCP_URL = "http://127.0.0.1:%d/mcp" % PORT
    assert ":8000" not in TB.LOCAL_MCP_URL
    TB.is_bridge = lambda path=None: True       # equip() consults settings.txt otherwise
    TB._instructions_cache.clear()
    return TB


def fetch_token(url_file):
    from tools import childproc
    r = childproc.run([PY, "-I", "-X", "utf8", os.path.abspath(__file__), "token", "--url-file", url_file],
                      timeout=240, cwd=ROOT, creationflags=childproc.headless_creationflags())
    out = (r.stdout or "").strip().splitlines()
    tok = out[-1] if out else ""
    return tok if tok.startswith("eyJ") else ""


def cmd_token(args):
    sys.path.insert(0, ROOT)
    from relay import profile_token as PT
    from playwright.sync_api import sync_playwright
    url = open(args.url_file, encoding="ascii", errors="replace").read().strip()
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % EDGE_PORT)
        print(PT.token_via_light_page(b.contexts[0], url, timeout_s=180, log=lambda m: None))


def edge_listening():
    import socket
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", EDGE_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------- instrumented product path

def install_recording_conversation():
    """socket_route builds `Conversation(...)`; swap in a subclass that records each model round trip."""
    import relay.socket_route as SR
    from relay.chathub import Conversation

    class RConv(Conversation):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.rows = []

        def _one_exchange(self, payload, **kw):
            cur = {"prompt_chars": len(payload)}
            t0 = time.time()
            try:
                return super()._one_exchange(payload, **kw)
            finally:
                cur["total_s"] = round(time.time() - t0, 2)
                cur["first_content_s"] = getattr(self, "first_content_s", None)
                self.rows.append(cur)

    SR.Conversation = RConv
    return SR


def instrument(drv):
    """Wrap the driver's tool runner (denial + counting) and its preflight (timing). Product code is untouched."""
    stats = {"tools": [], "tool_s": 0.0, "preflight_s": None, "preflight_chars": None}
    real = drv._run_tool

    def run_tool(name, args):
        inner = str((args or {}).get("name", "")) if isinstance(args, dict) else ""
        t0 = time.time()
        deny = is_denied(inner)
        res = (False, deny) if deny else real(name, args)
        dt = time.time() - t0
        stats["tools"].append({"name": inner[:40], "s": round(dt, 2), "ok": bool(res[0])})
        stats["tool_s"] = round(stats["tool_s"] + dt, 2)
        return res

    drv._run_tool = run_tool
    pre = getattr(drv, "_preflight", None)
    if pre is not None:
        def timed(text):
            t0 = time.time()
            s = pre(text)
            stats["preflight_s"] = round(time.time() - t0, 2)
            stats["preflight_chars"] = len(s or "")
            return s
        drv._preflight = timed
    return stats


def run_once(route, TB, arm, goal, i, exp, out_dir, agent_key):
    os.environ["MCP_BRIDGE_PREFLIGHT"] = "1" if arm == "on" else "0"
    assert TB.preflight_enabled() == (arm == "on")
    import psutil
    ram = round(psutil.virtual_memory().available / 2 ** 30, 2)
    cpu = psutil.cpu_percent(interval=None)
    drv = route.driver_for("ab-%s-%s-%d" % (arm, goal, i), agent_key)
    if drv is None:
        log(os.path.join(out_dir, "results.jsonl"), {"event": "run", "arm": arm, "goal": goal, "i": i, "ok": False,
                                                    "err": "no driver (route closed or token expired)"})
        return
    stats = instrument(drv)
    t0 = time.time()
    drv.send(goal_text(goal))
    finished = drv.wait_for_idle(timeout_s=900)
    wall = round(time.time() - t0, 2)
    ans = drv.read_last_response() if finished else ""
    err = scrub(drv.failed) if drv.failed else ("" if finished else "timeout after 900 s")
    rows = list(getattr(drv.conv, "rows", []))
    turns = int(getattr(drv.conv, "turns", 0) or 0)
    try:
        drv.close()
    except Exception:
        pass
    ok = (not err) and check(goal, ans, exp.get(goal, {}))
    log(os.path.join(out_dir, "results.jsonl"), {
        "event": "run", "arm": arm, "goal": goal, "i": i, "ok": bool(ok), "err": err, "wall_s": wall,
        "turns": turns, "tool_calls": len(stats["tools"]), "tool_s": stats["tool_s"],
        "preflight_s": stats["preflight_s"], "preflight_chars": stats["preflight_chars"],
        "turn_s": [r.get("total_s") for r in rows],
        "turn1_first_content_s": rows[0].get("first_content_s") if rows else None,
        "turn1_prompt_chars": rows[0].get("prompt_chars") if rows else None,
        "calls": [t["name"] for t in stats["tools"]],
        "ram_free_gb": ram, "cpu_pct_since_prev": cpu, "disk_free_gb": round(disk_free_gb(), 2),
        "answer_head": scrub(ans)[:160]})
    time.sleep(3)


# ---------------------------------------------------------------- commands

def setup_route(out_dir, token, url):
    sys.path.insert(0, ROOT)
    from relay import profile_token as PT, template_seed as TS
    from relay.socket_route import SocketRoute, websocket_connect
    tdir = os.path.join(out_dir, "templates")
    os.makedirs(tdir, exist_ok=True)
    standin = "https://donor.example.invalid/agent"
    shutil.copy(donor_template_path(), PT.template_path(standin, tdir))      # a COPY; the production .fleet is only read
    TS.seed(standin, url, tdir)
    template = PT.load_template(url, tdir)
    route = SocketRoute(enabled=True, connect_fn=websocket_connect,
                        log_path=os.path.join(out_dir, "socket_route_log.jsonl"))
    route.ledger_path = os.path.join(out_dir, "tool_events.jsonl")
    route.install_capture(token, template, url)
    return route, template


def cmd_ab_run(args):
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    sys.path.insert(0, ROOT)
    lower_priority()
    res = os.path.join(out_dir, "results.jsonl")
    if not edge_listening():
        log(res, {"event": "stop", "reason": "hidden Edge on 9350 is not running"})
        print("STOP: the hidden Edge on port %d is not running" % EDGE_PORT)
        return 5
    if not disk_ok(disk_free_gb()):
        log(res, {"event": "stop", "reason": "disk", "free_gb": round(disk_free_gb(), 2)})
        print("STOP: C: free space under %.0f GB" % MIN_FREE_GB)
        return 4
    url = open(args.url_file, encoding="ascii", errors="replace").read().strip()
    token = fetch_token(args.url_file)
    if not token:
        log(res, {"event": "stop", "reason": "no token from the hidden Edge (not signed in?)"})
        print("STOP: no token from the hidden Edge (not signed in?)")
        return 6
    key = secrets.token_hex(24)
    proc = start_server(out_dir, key)
    try:
        TB = point_bridge_at_private_server(key)
        instr = wait_server(proc)
        if not instr:
            log(res, {"event": "stop", "reason": "private server did not start"})
            print("STOP: private server on %d did not start" % PORT)
            return 7
        SR = install_recording_conversation()
        route, _ = setup_route(out_dir, token, url)
        TB.equip(route)
        goals = [g for g in args.goals.split(",") if g in SENT_GOALS]
        exp = {g: expected(g) for g in goals}
        from relay import profile_token as PT
        log(res, {"event": "setup", "instructions_chars": len(instr), "goals": goals, "n": args.n, "expected": exp,
                  "port": PORT, "code": git_head_short()})
        done = {(r["arm"], r["goal"], r["i"]) for r in read_rows(res) if r.get("event") == "run"}
        for i in range(1, args.n + 1):
            for g in goals:
                for arm in arm_order(i):
                    if (arm, g, i) in done:
                        continue
                    free = disk_free_gb()
                    if not disk_ok(free):
                        log(res, {"event": "stop", "reason": "disk", "free_gb": round(free, 2)})
                        print("STOP: C: free space %.1f GB under %.0f GB" % (free, MIN_FREE_GB))
                        return 4
                    if PT.token_life_s(route.token_for(url)) <= 660:
                        tok = fetch_token(args.url_file)
                        if not tok:
                            log(res, {"event": "stop", "reason": "token refresh failed"})
                            print("STOP: token refresh failed")
                            return 6
                        route.install_capture(tok, route.template_for(url), url)
                    run_once(route, TB, arm, g, i, exp, out_dir, url)
        log(res, {"event": "end"})
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(15)
            except Exception:
                proc.kill()


def cmd_smoke(args):
    """No Copilot turn and no token: starts the private server and checks the wiring and the arm switch."""
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    sys.path.insert(0, ROOT)
    lower_priority()
    key = secrets.token_hex(24)
    proc = start_server(out_dir, key)
    rep = {}
    try:
        TB = point_bridge_at_private_server(key)
        instr = wait_server(proc)
        rep["server_instructions_chars"] = len(instr)
        rep["url"] = TB.LOCAL_MCP_URL
        from types import SimpleNamespace
        from relay.socket_driver import CopilotSocketDriver
        for arm in ("on", "off"):
            os.environ["MCP_BRIDGE_PREFLIGHT"] = "1" if arm == "on" else "0"
            drv = CopilotSocketDriver(SimpleNamespace(turns=0), connect=None)
            drv.use_tools(TB.catalogue(), TB.make_run_tool("smoke"), protocol="PROTO\n",
                          preflight=TB.make_preflight("smoke") if TB.preflight_enabled() else None)
            stats = instrument(drv)
            t0 = time.time()
            pre = drv._first_turn_prefix(goal_text("a"))
            rep[arm] = {"prefix_chars": len(pre), "has_lookup": len(pre) > len("PROTO\n"),
                        "preflight_s": stats["preflight_s"], "wall_s": round(time.time() - t0, 2)}
            drv.conv.turns = 1
            rep[arm]["second_turn_prefix_chars"] = len(drv._first_turn_prefix("x"))
        run = TB.make_run_tool("smoke")
        for nm in ("read_file",):
            ok, text = run("call_tool", {"name": nm, "arguments": {"path": os.path.join(ROOT, "README.md"),
                                                                    "start_line": 1, "end_line": 2}})
            rep["read_" + nm] = {"ok": ok, "chars": len(text), "head": scrub(text)[:80]}
        drv = CopilotSocketDriver(SimpleNamespace(turns=0), connect=None)
        drv.use_tools(TB.catalogue(), TB.make_run_tool("smoke"))
        instrument(drv)
        rep["denied_write"] = list(drv._run_tool("call_tool", {"name": "write_file", "arguments": {}}))
        rep["denied_outlook"] = list(drv._run_tool("call_tool", {"name": "outlook_send", "arguments": {}}))
        rep["expected"] = {g: expected(g) for g in ("a", "c")}
        rep["disk_free_gb"] = round(disk_free_gb(), 2)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(15)
            except Exception:
                proc.kill()
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


def cmd_summary(args):
    rows = read_rows(os.path.join(os.path.abspath(args.out), "results.jsonl"))
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=1))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("smoke", "run", "summary", "token"):
        p = sub.add_parser(name)
        p.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "preflight_ab"))
        p.add_argument("--url-file", default="")
        p.add_argument("--n", type=int, default=10)
        p.add_argument("--goals", default="a,b,c")
    args = ap.parse_args(argv)
    return {"smoke": cmd_smoke, "run": cmd_ab_run, "summary": cmd_summary, "token": cmd_token}[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
