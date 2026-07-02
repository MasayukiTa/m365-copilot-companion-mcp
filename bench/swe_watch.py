"""Emit a fleet event line whenever a worker's (status,turn,verify_attempts) changes, plus a
heartbeat, and a final summary; exit 0 when the run is no longer running. Covers all terminal
states (done/stuck/verify_failed) so silence never masks a crash."""
import json, os, time, sys

STATUS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      ".fleet", "status.json")
prev = {}
hb = 0
while True:
    try:
        d = json.load(open(STATUS, encoding="utf-8"))
    except Exception:
        time.sleep(5); continue
    running = d.get("running")
    changed = []
    for w in d.get("workers", []):
        inst = w.get("cwd", "").split("wt_")[-1] or w.get("name")
        key = (w.get("status"), w.get("turn"), w.get("verify_attempts"))
        if prev.get(w["name"]) != key:
            prev[w["name"]] = key
            changed.append("%s[%s] %s turn=%s verify=%s %s" % (
                w["name"], inst, w.get("status"), w.get("turn"),
                w.get("verify_attempts"), (w.get("reason") or "")[:60]))
    for c in changed:
        print(c, flush=True)
    hb += 1
    if hb % 6 == 0 and running:  # ~2min heartbeat
        print("...still running, elapsed=%ss done=%s/%s" % (
            round(d.get("elapsed_s", 0)), d.get("done_count"), d.get("total")), flush=True)
    if not running:
        print("=== RUN COMPLETE: done=%s/%s ===" % (d.get("done_count"), d.get("total")), flush=True)
        for w in d.get("workers", []):
            inst = w.get("cwd", "").split("wt_")[-1] or w.get("name")
            print("FINAL %s[%s] outcome=%s reason=%s" % (
                w["name"], inst, w.get("outcome"), (w.get("reason") or "")[:80]), flush=True)
        sys.exit(0)
    time.sleep(20)
