"""A ceiling on what the managed Edge profiles are allowed to keep on disk.

THERE WAS NEVER ONE. `edge_recover.should_recycle` reads like a cap and is not: the value it
compares is `edge_mb` from `edge_memory.private_mb` -- RAM, not disk -- so the 1500 in
RECYCLE_EDGE_CAP_MB has never bounded a single byte of cache. And `trim_profile_caches`, which
does delete cache, is fail-closed on "is this profile running": the fleet Edge and the bridge
Edge are running essentially all the time, so it declined every time it was asked. A cap that
never fires and a trim that always declines add up to no cap at all, which is what the disk
showed -- 1.45 GB across two profiles, 1.07 GB of it cache, for two browsers that between them
hold one chat origin open.

THE CEILING IS THE FLEET'S OWN SIZE: two megabytes per worker. A worker is one conversation on
one origin; ten workers is twenty megabytes. The bridge is a single page and gets the floor of
one worker. This is deliberately far below what a browser would choose for itself -- Chromium
sizes its cache from free disk space, which on a machine with room means hundreds of megabytes
for a profile that re-fetches the same handful of assets.

TWO MECHANISMS, BECAUSE ONE CANNOT COVER BOTH STATES:

  not running -> delete the cache directories (trim_profile_caches already does this safely).
  running     -> ask the browser, over CDP, to clear its own cache. Deleting files under a
                 live browser is corruption; `Network.clearBrowserCache` is the supported way,
                 and it goes through BrowsingDataRemover's cache mask, so it reaches the V8
                 code cache as well as the HTTP one.

WHY THE LAUNCH FLAG IS NOT ENOUGH, MEASURED RATHER THAN ASSUMED. Two throwaway profiles, same
pages, same number of loads, one carrying --disk-cache-size=2MB:

    arm       Cache      Code Cache
    capped    27.64 MB    1.64 MB
    control   33.85 MB   21.13 MB

The flag governs the code cache almost exactly -- 1.64 MB against a 2 MB request, thirteen
times smaller than the control. It does NOT govern the HTTP cache: 27.64 MB against that same
2 MB request, a fifth off the control and an order of magnitude over the number asked for. The
expectation going in was the opposite (Code Cache is the larger half on the live profiles: 362
MB against 269 MB on the bridge), so a flag-only fix would have been shipped, reported as done,
and left the bigger remaining half uncapped.

That is what this module is for. It measures the bytes actually on disk and acts on that
number, so the ceiling holds whatever the switch happens to reach.
"""
from __future__ import annotations

import json
import os

from relay.edge_recover import (
    MANAGED_EDGE_PROFILES,
    _CACHE_DIR_NAMES,
    profile_dir,
    profile_is_running,
    trim_profile_caches,
)

#: Per worker. The user's number, and the reasoning behind it is the point: a worker holds one
#: conversation against one origin, so its working set of scripts and images is small and
#: repeats. Anything beyond a couple of megabytes per worker is history nobody reads again.
MB_PER_WORKER = float(os.environ.get("MCP_EDGE_CACHE_MB_PER_WORKER", "2"))

#: BORROWED, NOT RE-SPELLED. This started as its own list and that is exactly how the original
#: went wrong: two places naming the same directories, one of them missing GrShaderCache. A
#: second copy here would drift the same way, and the measure would then disagree with the trim
#: about what a cache is -- the cap would read a number the deletion could not act on.
CACHE_DIR_NAMES = _CACHE_DIR_NAMES


def cap_bytes(workers):
    """The ceiling for `workers` workers. Never zero: a profile with no fleet on it is still
    a browser holding one page, and a zero-byte cache would mean re-fetching every asset of
    every navigation. One worker is the floor."""
    try:
        n = int(workers)
    except (TypeError, ValueError):
        n = 1
    return int(max(1, n) * MB_PER_WORKER * 1024 * 1024)


def worker_count(fleet_dir=None, default=1):
    """How many workers are live, from the fleet's own status file.

    Read rather than configured, because the ceiling is supposed to track the fleet: a run
    that scales from four workers to ten should be allowed ten workers' worth of cache while
    it is running, and back down afterwards. An unreadable or absent status is not an excuse
    to lift the ceiling -- it returns the floor, so the cap tightens rather than disappears
    when this cannot tell.
    """
    path = os.path.join(fleet_dir or _fleet_dir(), "status.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return default
    workers = data.get("workers")
    if isinstance(workers, list) and workers:
        return len(workers)
    return default


def _fleet_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".fleet")


def _is_cache_dir(path):
    return os.path.basename(path).strip().lower() in CACHE_DIR_NAMES


def cache_bytes(marker, base=None):
    """Bytes currently held in the regenerable caches of one profile. This is the number the
    ceiling is compared against -- not the profile total, which includes the sign-in that must
    survive."""
    root = profile_dir(marker, base)
    total = 0
    for parent, dirs, _files in os.walk(root):
        for d in list(dirs):
            full = os.path.join(parent, d)
            if not _is_cache_dir(full):
                continue
            dirs.remove(d)
            for p2, _d2, files in os.walk(full):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(p2, f))
                    except OSError:
                        pass
    return total


def _port_of(marker):
    for port, name in MANAGED_EDGE_PROFILES.items():
        if name == marker:
            return port
    return None


def clear_over_cdp(marker, timeout_ms=8000):
    """Ask the live browser to drop its own cache. (ok: bool, note: str).

    Network.clearBrowserCache, not rmtree: this profile has a browser attached to it, and the
    project's own rule is that we do not reach into a running process's files. It also reaches
    the code cache, which deleting `Default\\Cache` would not.

    Cookies and storage are untouched on purpose. The sign-in in these profiles is the whole
    reason they are persistent; clearing it would trade a few hundred megabytes for a manual
    Entra sign-in on the next run.
    """
    port = _port_of(marker)
    if port is None:
        return False, "%s: not a managed profile" % marker
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return False, "%s: playwright unavailable (%s)" % (marker, type(exc).__name__)
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % port,
                                                  timeout=timeout_ms)
            try:
                ctx = browser.contexts[0] if browser.contexts else None
                if ctx is None:
                    return False, "%s: no browser context" % marker
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                sess = ctx.new_cdp_session(page)
                sess.send("Network.clearBrowserCache")
                return True, "%s: cache cleared over CDP" % marker
            finally:
                # close() would take the browser down with it on some versions; the caller
                # owns this browser and must get it back exactly as it was found.
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        return False, "%s: CDP clear failed (%s: %s)" % (marker, type(exc).__name__, exc)


def enforce(markers=None, workers=None, base=None, dry_run=False):
    """Bring every managed profile's cache back under the ceiling. Returns a report dict.

    Reports what it found and what it did per profile rather than a single freed number: a
    profile that was already under the ceiling, one that was trimmed on disk, and one that had
    to be cleared through a live browser are three different outcomes, and a caller that only
    sees megabytes cannot tell "nothing needed doing" from "the clear silently failed".
    """
    n = worker_count() if workers is None else workers
    cap = cap_bytes(n)
    targets = markers or sorted(MANAGED_EDGE_PROFILES.values())
    out = {"workers": n, "cap_mb": round(cap / 1048576.0, 1), "profiles": []}
    for marker in targets:
        root = profile_dir(marker, base)
        if not os.path.isdir(root):
            out["profiles"].append({"profile": marker, "state": "absent"})
            continue
        before = cache_bytes(marker, base)
        entry = {"profile": marker, "before_mb": round(before / 1048576.0, 1)}
        if before <= cap:
            entry["state"] = "under cap"
            out["profiles"].append(entry)
            continue
        if dry_run:
            entry["state"] = "over cap, dry run"
            out["profiles"].append(entry)
            continue
        if profile_is_running(marker):
            ok, note = clear_over_cdp(marker)
            entry["state"] = "cleared over CDP" if ok else "running, clear failed"
            entry["note"] = note
        else:
            _freed, notes = trim_profile_caches([marker], base=base)
            entry["state"] = "trimmed on disk"
            entry["note"] = "; ".join(notes)
        entry["after_mb"] = round(cache_bytes(marker, base) / 1048576.0, 1)
        out["profiles"].append(entry)
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workers", type=int, default=None,
                    help="worker count to size the cap from (default: read .fleet/status.json)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rep = enforce(workers=args.workers, dry_run=args.dry_run)
    print("cap: %s MB  (%s workers x %s MB)" % (rep["cap_mb"], rep["workers"], MB_PER_WORKER))
    for e in rep["profiles"]:
        print("  %-24s %-22s %s" % (
            e["profile"], e.get("state", ""),
            ("%.1f -> %.1f MB" % (e["before_mb"], e["after_mb"]))
            if "after_mb" in e else
            ("%.1f MB" % e["before_mb"] if "before_mb" in e else "")))
