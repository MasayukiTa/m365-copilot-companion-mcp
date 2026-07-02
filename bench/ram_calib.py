"""Live per-user RAM calibrator -- the seed of the concurrency self-tuning genome.

Observe the REAL Edge per-tab RAM under the running fleet (vs the flat 700MB assumption), so the
autoscale can self-tune PER USER. Read-only psutil + CDP sampling -> .fleet/swe/ram_calib.jsonl.
Per sample it records the observed range and three candidate estimators so we can pick the best:
  * per_tab          : instantaneous Edge working-set / fleet page count (the rough average)
  * ewma_per_tab     : AtCoder-style RECENCY-weighted EWMA of per_tab (recent samples weigh more)
  * marginal_per_tab : least-squares slope of (Edge WS vs page count) over a recent window -- the
                       true MARGINAL cost of one more tab, separating the fixed Edge overhead
plus swap pressure (swap_used/pct) -- the real OVER-admission signal (the ceiling is when the
pagefile thrashes, not when free-physical hits zero). Safe to run alongside the 50-run; stops when
the 50-run logs DONE or after 4h.
"""
import json, os, time, urllib.request
import psutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, ".fleet", "swe")
OUT = os.path.join(SW, "ram_calib.jsonl")
STATUS = os.path.join(REPO, ".fleet", "status.json")
RUNLOG = os.path.join(SW, "pro_run_50.log")
INTERVAL = 15
ALPHA = 0.25  # EWMA recency weight (higher = more recent-biased; to be tuned)


def edge_ws_mb():
    """RAM of the FLEET Edge only (the :9222 copilot-companion-edge process tree) -- NOT the user's
    other Edge windows, which would otherwise be wrongly attributed to the fleet's tabs. Find the
    main browser process (carries --remote-debugging-port=9222 / the companion profile) and sum it
    plus all its descendants (renderers/GPU/utility)."""
    roots = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() == "msedge.exe":
                cl = " ".join(p.info["cmdline"] or [])
                if "9222" in cl or "copilot-companion-edge" in cl:
                    roots.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    seen, tot = set(), 0
    for r in roots:
        try:
            procs = [r] + r.children(recursive=True)
        except psutil.NoSuchProcess:
            procs = [r]
        for p in procs:
            try:
                if p.pid in seen:
                    continue
                seen.add(p.pid)
                tot += p.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return tot / 1024 / 1024


def fleet_pages():
    try:
        tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=3))
        return sum(1 for t in tabs if t.get("type") == "page")
    except Exception:
        return 0


def fleet_state():
    try:
        st = json.load(open(STATUS, encoding="utf-8-sig"))
        active = sum(1 for w in (st.get("workers") or [])
                     if w.get("status") in ("waiting", "researching", "refuting"))
        return active, st.get("max_concurrent"), st.get("running")
    except Exception:
        return None, None, None


def regress(pts):
    pts = [(x, y) for x, y in pts if x > 0]
    n = len(pts)
    if n < 3:
        return None, None
    sx = sum(x for x, _ in pts); sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts); sxy = sum(x * y for x, y in pts)
    d = n * sxx - sx * sx
    if d == 0:
        return None, None
    slope = (n * sxy - sx * sy) / d
    intercept = (sy - slope * sx) / n
    return slope, intercept


def run_done():
    try:
        with open(RUNLOG, encoding="utf-8") as f:
            return any("DONE Pro 50-run" in ln for ln in f)
    except Exception:
        return False


def settings_path():
    return os.path.join(os.environ.get("APPDATA", ""), "copilot-bridge", "settings.txt")


def write_setting(key, value):
    """Update key=value in the shared settings.txt (the file fleet_runner reads), preserving every
    other line. No BOM (fleet reads utf-8-sig but writes must stay BOM-free per the repo rule)."""
    sp = settings_path()
    try:
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        lines, found = [], False
        if os.path.exists(sp):
            for ln in open(sp, encoding="utf-8-sig"):
                if ln.strip().lower().startswith(key.lower() + "="):
                    lines.append("%s=%s\n" % (key, value)); found = True
                else:
                    lines.append(ln if ln.endswith("\n") else ln + "\n")
        if not found:
            lines.append("%s=%s\n" % (key, value))
        with open(sp, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        return True
    except Exception:
        return False


def calibrate(ewma, recent_pertab, swap_pct):
    """Turn observations into a SAFE per-tab estimate to write back. Use the recency-weighted EWMA
    of the average per-tab (already conservative vs the regression marginal), clamped to [400,700].
    SWAP TRIPWIRE: if the box is already swapping hard, do NOT lower the reserve -- back off to the
    safe 700 so we never over-admit into a thrash."""
    if swap_pct is not None and swap_pct >= 25:
        return 700
    if ewma is None:
        return None
    v = int(round(ewma))
    return max(400, min(700, v))


def main():
    open(OUT, "w").close()
    ewma = None
    pts = []
    npt = 0           # how many samples actually had a per-tab reading (stability gate)
    last_written = None
    end = time.time() + 4 * 3600
    while time.time() < end:
        vm = psutil.virtual_memory(); sw = psutil.swap_memory()
        ews = edge_ws_mb(); pg = fleet_pages()
        active, cap, frun = fleet_state()
        per_tab = (ews / pg) if pg > 0 else None
        if per_tab:
            ewma = per_tab if ewma is None else ALPHA * per_tab + (1 - ALPHA) * ewma
            pts.append((pg, ews)); pts = pts[-60:]; npt += 1
        slope, base = regress(pts[-30:])
        row = dict(ts=round(time.time()), edge_mb=round(ews), pages=pg, active=active, cap=cap,
                   avail_mb=round(vm.available / 1024 / 1024), free_mb=round(vm.free / 1024 / 1024),
                   used_pct=round(vm.percent),
                   swap_used_mb=round(sw.used / 1024 / 1024), swap_pct=round(sw.percent),
                   per_tab=round(per_tab) if per_tab else None,
                   ewma_per_tab=round(ewma) if ewma else None,
                   marginal_per_tab=round(slope) if slope else None,
                   base_overhead_mb=round(base) if base else None)
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        # FEEDBACK: once the estimate is stable (>=20 per-tab samples), write the calibrated value
        # to settings.txt so the fleet autoscale uses THIS machine's measured cost (vs the flat 700).
        if npt >= 20:
            cal = calibrate(ewma, None, row["swap_pct"])
            if cal and cal != last_written:
                if write_setting("autoscale_per_tab_mb", cal):
                    with open(OUT, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"ts": round(time.time()),
                                            "note": "calibrated autoscale_per_tab_mb=%d (was %s)"
                                                    % (cal, last_written)}) + "\n")
                    last_written = cal
        if run_done():
            with open(OUT, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": round(time.time()), "note": "50-run DONE -- stopping"}) + "\n")
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
