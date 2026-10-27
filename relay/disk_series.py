# -*- coding: utf-8 -*-
"""Resident sampler for C: free space and the processes writing it, with swing attribution.

WHY THIS EXISTS (owner, 2026-10-27). C: free space moved ~6 GB up and down within minutes
(10/26 14:29 +4.75 GB, 10/27 12:27-12:53 a fall, 13:58 +6.1 GB) and the cause was never
pinned. The hand tools that measured it -- disk_watch/proc_write_log.py (per-process
WriteTransferCount deltas every 5 s) and disk_watch/disk_watch.py (per-location sizes when
free space jumps >=300 MB) -- were started by hand and die with the shell. This is their
product form: one resident that records the series continuously, flags the swings, and
publishes a disk panel the cockpit already reads.

WHAT IT WRITES, all under the state dir (.fleet):
  * disk_series.jsonl   one line every INTERVAL_S: free_gb, delta since the last sample,
                        total MB/s written, and the top writers (name, pid, cmd head, MB/s).
  * disk_swings.jsonl   ONE line per swing -- free space moving >= swing_mb within
                        SWING_WINDOW_S -- carrying the +-60 s of samples around it and the
                        writers that led it, so a reader sees who moved the disk, not just that
                        it moved.
  * health_strip.json   the hs_disk block merged in (free_gb, 10-minute slope, swing count),
                        additive to whatever the cockpit already published.

WHO RUNS IT. scripts/supervisor.ps1 keeps exactly one of these alive (Ensure-DiskSeries), the
same single-resident shape it uses for the server and tunnel host. fleet_runner only lives for
the length of a run, so hosting it there would leave the series full of holes; the supervisor is
the one process demonstrably alive the whole time.

RATCHET RULES HONOURED. The WMI child is read through tools.childproc.run (UTF-8-first decode,
never the locale codec) and launched with childproc.headless_creationflags() (CREATE_NO_WINDOW,
not DETACHED_PROCESS) so it never flashes a console on an unattended desktop. Nothing is
deleted or changed outside the three files above; the sampler is read-only against the disk.

THE SETTINGS (operator-owned; this module chooses no floor). Each is read from the cockpit's
settings.txt via relay.fleet_runner._settings_float, falling back to the documented default:
  * disk_warn_gb   (default 5.0) free space at or below which the cockpit paints the disk
                   reading in its warning colour. THE OWNER SETS THE REAL FLOOR; 5 GB is only a
                   visible default so the panel is never silent, and it WARNS ONLY -- it never
                   stops intake (the owner's rule). Chosen because the observed low end of the
                   swing was 1.4-2 GB, so a 5 GB line lights before the disk reaches that floor.
  * disk_swing_mb  (default 300.0) the free-space move that counts as a swing. Matches the
                   300 MB threshold both hand tools already used.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time

from tools import childproc

# ---- fixed shape ---------------------------------------------------------

INTERVAL_S = 5.0            # one series sample every 5 s, as the hand tool sampled
SWING_WINDOW_S = 10.0       # a move counts as a swing when it happens within this many seconds
SWING_CONTEXT_S = 60.0      # how much before/after a swing is carried in the swing record
TOP_WRITERS = 6             # writers kept per sample, by MB/s descending

#: Per-file ceiling for disk_series.jsonl, TAIL KEPT -- the same runaway-writer stop
#: fleet_retention.cap_jsonl applies to the other .fleet ledgers, sized for THIS writer.
#:
#: Measured 2026-10-27 on the hand tool's own log: a sample line is ~537 bytes, and at one
#: every 5 s that is ~9.3 MB/day. 16 MB is therefore just under two days of continuous history
#: kept as the tail -- enough to see a swing and the hours either side of it -- while staying
#: well under fleet_retention's 64 MB general ceiling, because this is a high-frequency series
#: that should not dominate .fleet. Env override: MCP_FLEET_DISK_SERIES_MAX_MB.
DISK_SERIES_MAX_MB = float(os.environ.get("MCP_FLEET_DISK_SERIES_MAX_MB", "16"))

#: disk_swings.jsonl is one line per swing, not a per-sample series, so it grows far slower;
#: the same 16 MB tail cap is applied for symmetry and as a stop on a pathological swing storm.
DISK_SWINGS_MAX_MB = float(os.environ.get("MCP_FLEET_DISK_SWINGS_MAX_MB", "16"))

#: A capped ledger is cut to this fraction of the ceiling, not to the ceiling itself, so the
#: rewrite happens once per tenth of the ceiling written rather than at every append. Same
#: value and reasoning as fleet_retention.JSONL_TRIM_FRACTION.
TRIM_FRACTION = float(os.environ.get("MCP_FLEET_DISK_TRIM_FRACTION", "0.9"))

_COPY_CHUNK = 1024 * 1024

# Defaults for the operator-owned settings. The owner sets the real floor; these only keep the
# panel from being silent and never stop intake.
DEFAULT_WARN_GB = 5.0
DEFAULT_SWING_MB = 300.0


def _setting(key, default):
    """A float from the cockpit's settings.txt, falling back to `default`. Never raises."""
    try:
        from relay.fleet_runner import _settings_float
        return _settings_float(key, default)
    except Exception:
        return default


def warn_gb():
    return _setting("disk_warn_gb", DEFAULT_WARN_GB)


def swing_mb():
    return _setting("disk_swing_mb", DEFAULT_SWING_MB)


# ---- pure helpers (tested without touching the real C:) ------------------


def _state_dir():
    """The .fleet directory beside this checkout, matching fleet_retention's default."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".fleet")


def parse_processes(stdout):
    """WMI Win32_Process JSON -> {pid: (name, write_bytes, cmd_head)}. Never raises.

    Accepts the compressed JSON from Get-CimInstance (a list, or a single object when there is
    one row), and tolerates missing fields. cmd_head is the first 120 chars of the command
    line, or 'ppid=<n>' when the command line is not readable -- the same fallback the hand tool
    used so a row is never empty.
    """
    try:
        data = json.loads(stdout or "[]")
    except Exception:
        return {}
    if isinstance(data, dict):
        data = [data]
    out = {}
    if not isinstance(data, list):
        return out
    for p in data:
        if not isinstance(p, dict):
            continue
        try:
            pid = int(p.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        name = p.get("Name") or ""
        try:
            w = int(p.get("WriteTransferCount") or 0)
        except (TypeError, ValueError):
            w = 0
        cmd = p.get("CommandLine")
        if isinstance(cmd, str) and cmd.strip():
            head = cmd.strip()[:120]
        else:
            head = "ppid=%s" % p.get("ParentProcessId")
        out[pid] = (name, w, head)
    return out


def top_writers(prev, cur, dt, top_n=TOP_WRITERS):
    """(rows, total_bytes) for processes that wrote since the previous sample.

    rows is a list of dicts {mbps, name, pid, cmd} sorted by MB/s descending, capped at top_n.
    A process is counted only when it existed in `prev` and its write counter did not go
    backwards (a recycled pid or a counter reset is dropped, not charged a negative delta).
    dt is clamped to >= 0.5 s so a fast loop cannot divide by a near-zero interval.
    """
    dt = max(0.5, float(dt))
    rows, total = [], 0
    for pid, (name, w, cmd) in cur.items():
        if pid in prev and prev[pid][1] <= w:
            d = w - prev[pid][1]
            total += d
            if d > 0:
                rows.append((d / dt / 1e6, name, pid, cmd))
    rows.sort(reverse=True, key=lambda r: r[0])
    top = [{"mbps": round(a, 2), "name": b, "pid": c, "cmd": d} for a, b, c, d in rows[:top_n]]
    return top, total


def build_series_record(ts, free_bytes, prev_free, dt, top, total_bytes):
    """One disk_series.jsonl record. prev_free None on the first sample -> delta 0."""
    dt = max(0.5, float(dt))
    delta_mb = round((free_bytes - prev_free) / 1e6, 1) if prev_free is not None else 0.0
    return {
        "ts": ts,
        "free_gb": round(free_bytes / 1e9, 3),
        "delta_free_mb": delta_mb,
        "written_mbps_all": round(total_bytes / dt / 1e6, 2),
        "top": list(top),
    }


def is_swing(window, swing_threshold_mb, window_s=SWING_WINDOW_S):
    """True when free space moved >= swing_threshold_mb across any <= window_s span in `window`.

    `window` is a list of series records (dicts with ts_epoch and free_gb) oldest-first. The
    test is on the SPAN of free space inside a sliding time window, not on a single sample's
    delta: a 6 GB move spread over three 5 s samples is still a swing even though no one delta
    crosses the line. Returns the (lo, hi) records bounding the LARGEST qualifying move, or None.

    THE WIDEST PAIR, NOT THE FIRST. A fall of 8 -> 5 -> 2 GB crosses the threshold already at
    8 -> 5, but the swing the reader wants recorded is the whole 8 -> 2 move: returning the
    first qualifying pair would under-report delta_gb (build_swing_record reads hi - lo) and
    name only the writers of the first leg. So every pair inside the time window is considered
    and the one with the greatest |free_gb| span is returned.
    """
    if not window:
        return None
    thr_gb = swing_threshold_mb / 1000.0
    best = None
    best_span = 0.0
    for i in range(len(window)):
        base = window[i]
        for j in range(i + 1, len(window)):
            nxt = window[j]
            if (nxt["ts_epoch"] - base["ts_epoch"]) > window_s:
                break
            span = abs(nxt["free_gb"] - base["free_gb"])
            if span >= thr_gb and span > best_span:
                best = (base, nxt)
                best_span = span
    return best


def build_swing_record(ts, lo, hi, context):
    """One disk_swings.jsonl record summarising a swing and its +-60 s context.

    lo/hi are the bounding samples; context is the list of series records within SWING_CONTEXT_S
    of the swing. The leading writers are aggregated across the context by summed MB/s so the
    record names who moved the disk, with pid and command-line head, not just that it moved.
    """
    agg = {}
    for rec in context:
        for w in rec.get("top", []):
            key = (w.get("name", ""), w.get("pid"))
            e = agg.setdefault(key, {"name": w.get("name", ""), "pid": w.get("pid"),
                                     "cmd": w.get("cmd", ""), "mbps_sum": 0.0, "n": 0})
            e["mbps_sum"] += float(w.get("mbps", 0.0) or 0.0)
            e["n"] += 1
    leaders = sorted(agg.values(), key=lambda e: e["mbps_sum"], reverse=True)[:TOP_WRITERS]
    for e in leaders:
        e["mbps_avg"] = round(e["mbps_sum"] / max(1, e["n"]), 2)
        e["mbps_sum"] = round(e["mbps_sum"], 2)
    return {
        "ts": ts,
        "from_gb": round(lo["free_gb"], 3),
        "to_gb": round(hi["free_gb"], 3),
        "delta_gb": round(hi["free_gb"] - lo["free_gb"], 3),
        "span_s": round(hi["ts_epoch"] - lo["ts_epoch"], 1),
        "direction": "up" if hi["free_gb"] > lo["free_gb"] else "down",
        "context_samples": len(context),
        "leaders": leaders,
    }


def compute_hs_disk(series, swings, now_epoch, warn_threshold_gb):
    """The hs_disk block for health_strip.json. Pure: reads only the lists it is given.

    series: recent disk_series records (dicts with ts_epoch, free_gb), oldest-first.
    swings: recent disk_swings records (dicts with ts_epoch).
    Returns {free_gb, slope_gb_per_10min, swings_10min, warn, warn_gb, checked_age_s} or, when
    there is no sample at all, {checked_age_s: -1} so a reader can tell 'measured nothing' from
    'measured zero'.
    """
    if not series:
        return {"free_gb": None, "slope_gb_per_10min": None, "swings_10min": 0,
                "warn": False, "warn_gb": round(warn_threshold_gb, 2), "checked_age_s": -1.0}
    last = series[-1]
    free_gb = last["free_gb"]
    # Slope over the last 10 minutes: (free_now - free_10min_ago) scaled to a per-10-min rate.
    window = [r for r in series if (now_epoch - r["ts_epoch"]) <= 600.0]
    slope = None
    if len(window) >= 2:
        a, b = window[0], window[-1]
        span = b["ts_epoch"] - a["ts_epoch"]
        if span > 0:
            slope = round((b["free_gb"] - a["free_gb"]) * (600.0 / span), 3)
    swings_10min = sum(1 for s in swings if (now_epoch - s.get("ts_epoch", 0)) <= 600.0)
    return {
        "free_gb": round(free_gb, 3),
        "slope_gb_per_10min": slope,
        "swings_10min": swings_10min,
        "warn": bool(free_gb <= warn_threshold_gb),
        "warn_gb": round(warn_threshold_gb, 2),
        "checked_age_s": round(now_epoch - last["ts_epoch"], 1),
    }


def merge_health_strip(existing_text, hs_disk):
    """Return the health_strip.json text with hs_disk added, preserving everything else.

    The cockpit (ui/FleetCockpit.Health.cs) owns this file and writes the dots and the queue;
    this only ADDS an "hs_disk" key, so an unreadable or absent file yields a minimal object
    carrying just hs_disk rather than destroying what the cockpit published. Never raises.
    """
    obj = {}
    if existing_text:
        try:
            loaded = json.loads(existing_text)
            if isinstance(loaded, dict):
                obj = loaded
        except Exception:
            obj = {}
    obj["hs_disk"] = hs_disk
    return json.dumps(obj, ensure_ascii=False)


# ---- cap (tail kept), mirroring fleet_retention.cap_jsonl -----------------


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _keep_tail(path, size, keep):
    """Rewrite `path` as its last `keep` bytes, starting on a line boundary. True if it did.

    Streamed a megabyte at a time into a sibling and swapped in, so the peak held in memory is
    one chunk -- the same shape as fleet_retention._keep_tail, for the same reason (never hold
    the whole ceiling resident for the length of a disk write).
    """
    tmp = path + ".captmp"
    try:
        with io.open(path, "rb") as src:
            src.seek(max(0, size - keep))
            first = src.readline()  # partial line at the head -> drop it, land on a boundary
            if not first:
                return False
            with io.open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def cap_jsonl(path, max_mb, trim_fraction=TRIM_FRACTION):
    """Hold one ledger under max_mb, KEEPING THE TAIL. Returns bytes freed. Never raises."""
    try:
        limit = int(float(max_mb) * 1024 * 1024)
        before = _size(path)
        if before <= limit:
            return 0
        keep = int(limit * trim_fraction)
        if _keep_tail(path, before, keep):
            return before - _size(path)
    except Exception:
        pass
    return 0


def append_jsonl(path, record, max_mb):
    """Append one JSON line and cap the file to max_mb (tail kept). Never raises."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        cap_jsonl(path, max_mb)
    except Exception:
        pass


# ---- the WMI sample (the only part that touches the machine) --------------

_PS = ("Get-CimInstance Win32_Process | Select-Object "
       "ProcessId,Name,WriteTransferCount,ParentProcessId,CommandLine | ConvertTo-Json -Compress")


def sample_processes():
    """Current {pid: (name, write_bytes, cmd_head)} via WMI, read through childproc. {} on error.

    childproc.run keeps the locale codec out of the child's output (UTF-8 first), and
    headless_creationflags() keeps the powershell child from flashing a console on an
    unattended desktop -- the two ratchet rules this launch is bound by.
    """
    try:
        r = childproc.run(["powershell.exe", "-NoProfile", "-Command", _PS],
                          timeout=60, creationflags=childproc.headless_creationflags())
        return parse_processes(r.stdout)
    except Exception:
        return {}


def free_bytes(drive="C:/"):
    try:
        return shutil.disk_usage(drive).free
    except OSError:
        return None


# ---- the resident loop ----------------------------------------------------


def run(state_dir=None, sample_fn=None, free_fn=None, now_fn=None, stop_after=None,
        interval_s=0.0):
    """The resident sampler. Injectable for tests; the real C: is never touched under injection.

    sample_fn() -> {pid: (name, write, cmd)}; free_fn() -> free bytes; now_fn() -> epoch seconds.
    stop_after bounds the loop for tests. interval_s 0 means sleep INTERVAL_S between real
    samples (tests pass a tiny value or stop_after to avoid waiting).
    """
    state_dir = state_dir or _state_dir()
    sample_fn = sample_fn or sample_processes
    free_fn = free_fn or free_bytes
    now_fn = now_fn or time.time
    series_path = os.path.join(state_dir, "disk_series.jsonl")
    swings_path = os.path.join(state_dir, "disk_swings.jsonl")
    strip_path = os.path.join(state_dir, "health_strip.json")

    recent = []        # series records kept in memory for slope / swing context (bounded)
    recent_swings = [] # swing records kept in memory for hs_disk's 10-minute count
    prev, prev_free, prev_t = None, None, None
    last_swing_lo_t = 0.0
    count = 0

    while True:
        t = now_fn()
        cur = sample_fn() or {}
        free = free_fn()
        if free is not None and prev is not None and prev_t is not None:
            dt = t - prev_t
            top, total = top_writers(prev, cur, dt)
            rec = build_series_record(_iso(t), free, prev_free, dt, top, total)
            rec_mem = dict(rec, ts_epoch=t)
            recent.append(rec_mem)
            append_jsonl(series_path, rec, DISK_SERIES_MAX_MB)

            # Keep only the last ~10 minutes + swing context in memory.
            horizon = t - max(600.0, SWING_CONTEXT_S * 2)
            recent = [r for r in recent if r["ts_epoch"] >= horizon]
            recent_swings = [s for s in recent_swings if s.get("ts_epoch", 0) >= horizon]

            # Swing detection over the tail of the in-memory series.
            bounds = is_swing(recent, swing_mb())
            if bounds is not None:
                lo, hi = bounds
                # ONE RECORD PER SWING, ANCHORED ON ITS START (lo), NOT ITS END (hi). A single
                # continuous fall 8 -> 5 -> 2 GB keeps widening is_swing's best pair, so its hi
                # sample advances every tick while lo stays put; anchoring on hi re-emitted the
                # same swing once per sample (measured by test_run_writes_series_swings_and_hs_disk:
                # it saw 2 records for one 6 GB fall). A new swing is one that BEGINS from a later
                # sample than the last one we recorded, so a fall that is still deepening from the
                # same lo is the swing already on file, not a new one. The full depth of that fall
                # is still recoverable from disk_series.jsonl and from this record's +-60 s context.
                if lo["ts_epoch"] > last_swing_lo_t:
                    ctx = [r for r in recent
                           if abs(r["ts_epoch"] - hi["ts_epoch"]) <= SWING_CONTEXT_S]
                    swing = build_swing_record(_iso(t), lo, hi, ctx)
                    swing_mem = dict(swing, ts_epoch=t)
                    recent_swings.append(swing_mem)
                    append_jsonl(swings_path, swing, DISK_SWINGS_MAX_MB)
                    last_swing_lo_t = lo["ts_epoch"]

            # Publish the hs_disk block (merged, preserving the cockpit's own keys).
            hs = compute_hs_disk(recent, recent_swings, t, warn_gb())
            _publish_strip(strip_path, hs)

        prev, prev_free, prev_t = cur, free, t
        count += 1
        if stop_after is not None and count >= stop_after:
            break
        time.sleep(interval_s if interval_s > 0 else INTERVAL_S)


def _publish_strip(strip_path, hs_disk):
    """Merge hs_disk into health_strip.json atomically (tmp + replace). Never raises."""
    try:
        existing = ""
        if os.path.isfile(strip_path):
            with io.open(strip_path, encoding="utf-8") as f:
                existing = f.read()
        text = merge_health_strip(existing, hs_disk)
        os.makedirs(os.path.dirname(strip_path), exist_ok=True)
        tmp = strip_path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, strip_path)
    except Exception:
        pass


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def main(argv=None):
    """Entry point for `python -m relay.disk_series`. Runs until killed."""
    argv = list(sys.argv[1:] if argv is None else argv)
    state_dir = None
    if "--state-dir" in argv:
        i = argv.index("--state-dir")
        try:
            state_dir = argv[i + 1]
        except IndexError:
            state_dir = None
    try:
        run(state_dir=state_dir)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
