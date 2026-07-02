"""Print ONE compact progress line for the running batch_30 SWE run.

Designed to be called on an interval by a Monitor heartbeat loop so the user
gets a periodic status pulse (resolved count, round, live workers, C: free)
even when no RESOLVED/round event has fired. Reads only -- never mutates.
"""
import json
import os
import re
import shutil
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWE = os.path.join(REPO, ".fleet", "swe")
STATUS = os.path.join(REPO, ".fleet", "status.json")
LOG = os.path.join(SWE, "run_until_done.log")
BATCH = os.path.join(SWE, "batch_30.txt")


def _batch_members():
    try:
        return set(l.strip() for l in open(BATCH, encoding="utf-8") if l.strip())
    except Exception:
        return set()


def _resolved(members):
    out = set()
    try:
        for l in open(LOG, encoding="utf-8"):
            if "RESOLVED:" in l:
                inst = l.split("RESOLVED:")[1].strip()
                if inst in members:
                    out.add(inst)
    except Exception:
        pass
    return out


def _round():
    rnd = "?"
    try:
        for l in open(LOG, encoding="utf-8"):
            m = re.search(r"--- round (\d+/\d+)", l)
            if m:
                rnd = m.group(1)
    except Exception:
        pass
    return rnd


def _workers():
    try:
        d = json.load(open(STATUS, encoding="utf-8"))
    except Exception:
        return "?", []
    age = int(time.time() - d.get("updated", 0))
    cells = []
    for w in d.get("workers", []):
        inst = (w.get("cwd", "") or "").split("wt_")[-1] or w.get("name", "")
        inst = inst.split("__")[-1] if "__" in inst else inst  # short
        r = (w.get("reason") or "")
        tag = ""
        m = re.search(r"retry (\d+)/10", r)
        if m:
            tag = " retry" + m.group(1)
        cells.append("%s:%s t%s%s%s" % (
            w.get("name"), inst, w.get("turn"),
            "/v%s" % w.get("verify_attempts") if w.get("verify_attempts") else "",
            tag))
    return age, cells


def main():
    members = _batch_members()
    total = len(members) or 30
    done = len(_resolved(members))
    age, cells = _workers()
    free_gb = round(shutil.disk_usage(os.path.splitdrive(REPO)[0] + "\\").free / 1e9, 1)
    ts = time.strftime("%H:%M:%S")
    print("[%s] batch_30 %d/%d resolved | round %s | C: %sGB | status %ss ago | %s" % (
        ts, done, total, _round(), free_gb, age, "  ".join(cells) or "(no workers)"),
        flush=True)


if __name__ == "__main__":
    main()
