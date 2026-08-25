"""Hammer the session store the ways real use will, and check what a unit test cannot.

WHY THIS IS SEPARATE FROM THE TEST SUITE. The tests pin behaviour on a handful of rows in a
temporary directory. Every defect this store has actually shipped was invisible at that size:
incremental vacuum reclaiming one page out of 291, auto_vacuum silently fixed at NONE by an
earlier pragma, a size read taken before the file existed. Each needed either real volume or a
real filesystem to show itself. So this runs volume, concurrency and interruption against a
throwaway directory and asserts on what comes back.

Nothing here touches the live store. Each scenario builds its own directory under the system
temp area and removes it, so a run can be interrupted without leaving anything behind.

  python scripts/stress_session_store.py             # all scenarios
  python scripts/stress_session_store.py --only churn
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _store(base):
    from bridge import session_store as ss
    ss._base_dir = lambda: base
    ss._IMPORTED.discard(os.path.abspath(base))
    return ss


def _box():
    return tempfile.mkdtemp(prefix="sess_stress_")


def _say(name, ok, detail):
    print("  [%s] %-34s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)
    return ok


# ── scenario: volume ────────────────────────────────────────────────────────────

def scenario_volume(turns=20000, answer_bytes=3000):
    """What the file costs at a realistic year of use, and whether reads stay bounded.

    The projection from migration was about a megabyte per thousand turns, but those turns
    averaged 333 bytes, which is short for a Copilot answer. This runs the size the operator
    would actually see.
    """
    base = _box()
    ok = True
    try:
        ss = _store(base)
        sess = ss.new_session(title="volume")
        sid = sess["sid"]
        t0 = time.time()
        for i in range(turns):
            ss.append_turn(sid, "user" if i % 2 == 0 else "assistant",
                           "x" * (answer_bytes if i % 2 else 200))
        write_s = time.time() - t0

        st = ss.store_stats()
        t0 = time.time()
        recent = ss.recent_turns(sid, 20)
        recent_s = time.time() - t0

        ok &= _say("volume: rows all present", st["turns"] == turns,
                   "%d turns, %.1f MB, %.0f turns/s" % (st["turns"], st["mb"],
                                                        turns / max(write_s, 1e-9)))
        # A BOUNDED READ MUST STAY BOUNDED. This is the whole reason for the store: if
        # fetching the last 20 turns slows down as history grows, recycling is no cheaper
        # than it was over a JSONL file and nothing was gained.
        ok &= _say("volume: last-20 read is bounded", recent_s < 0.5 and len(recent) == 20,
                   "%.0f ms at %d turns" % (recent_s * 1000, turns))
        per_turn = st["bytes"] / float(turns)
        ok &= _say("volume: cost per turn is sane", per_turn < answer_bytes * 3,
                   "%.0f bytes/turn on disk vs %d written" % (per_turn, answer_bytes))
        print("      -> a year at 100 turns/day would be about %.0f MB"
              % (per_turn * 36500 / (1024.0 * 1024.0)))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return ok


# ── scenario: concurrency ───────────────────────────────────────────────────────

def _writer(base, worker, n, q):
    sys.path.insert(0, REPO)
    from bridge import session_store as ss
    ss._base_dir = lambda: base
    wrote = 0
    for i in range(n):
        if ss.record_fleet_turn("stress_w%d" % worker,
                                {"turn": i, "role": "user", "text": "w%d t%d" % (worker, i),
                                 "ts": time.time()}, name="w%d" % worker, goal="g"):
            wrote += 1
        time.sleep(random.uniform(0, 0.004))
    q.put((worker, wrote))


def scenario_concurrency(workers=8, per_worker=250):
    """Many fleet workers writing at once, which is how the fleet actually runs.

    A store that loses rows under contention, or that raises into a worker's turn, is worse
    than the JSONL files it replaces -- those could not collide at all.
    """
    base = _box()
    ok = True
    try:
        _store(base).new_session(title="seed")          # create the file before the fan-out
        q = multiprocessing.Queue()
        procs = [multiprocessing.Process(target=_writer, args=(base, w, per_worker, q))
                 for w in range(workers)]
        t0 = time.time()
        for p in procs:
            p.start()
        reported = [q.get() for _ in procs]
        for p in procs:
            p.join(60)
        elapsed = time.time() - t0

        ss = _store(base)
        rows = ss.fleet_turns(limit=workers * per_worker * 2)
        claimed = sum(n for _w, n in reported)
        expected = workers * per_worker
        ok &= _say("concurrency: every write reported ok", claimed == expected,
                   "%d/%d accepted" % (claimed, expected))
        ok &= _say("concurrency: every row is in the table", len(rows) == expected,
                   "%d rows from %d writers in %.1fs" % (len(rows), workers, elapsed))
        per_key = {}
        for r in rows:
            per_key.setdefault(r["key"], set()).add(r["turn"])
        complete = all(len(v) == per_worker for v in per_key.values())
        ok &= _say("concurrency: no worker lost a turn", complete and len(per_key) == workers,
                   "%d keys, %s" % (len(per_key),
                                    "all complete" if complete else "GAPS PRESENT"))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return ok


# ── scenario: churn ─────────────────────────────────────────────────────────────

def scenario_churn(rounds=12, per_round=400):
    """Write, prune, write again -- repeatedly. Does the file settle or ratchet upward?

    Pruning that frees rows without returning pages leaves a file that only ever grows. That
    is precisely the defect incremental vacuum was added for, and it hid for a whole session
    because a single prune looks fine.
    """
    base = _box()
    ok = True
    sizes = []
    try:
        ss = _store(base)
        for r in range(rounds):
            for i in range(per_round):
                s = ss.new_session(title="r%d-%d" % (r, i))
                ss.append_turn(s["sid"], "assistant", "y" * 2000)
                ss.touch(s["sid"], last_active_ts=1000.0 + r)
            ss.prune(max_age_days=0.0001)              # everything just written is "old"
            sizes.append(ss.store_stats()["bytes"])
        first, last = sizes[0], sizes[-1]
        ok &= _say("churn: the file does not ratchet up", last <= first * 1.5,
                   "round 1 %.0f KB -> round %d %.0f KB" % (first / 1024.0, rounds,
                                                            last / 1024.0))
        ok &= _say("churn: store is empty after pruning",
                   ss.store_stats()["sessions"] == 0, "0 sessions left")
        print("      -> sizes by round: %s KB"
              % " ".join("%.0f" % (s / 1024.0) for s in sizes))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return ok


# ── scenario: interruption ──────────────────────────────────────────────────────

def _killable(base, ready):
    sys.path.insert(0, REPO)
    from bridge import session_store as ss
    ss._base_dir = lambda: base
    s = ss.new_session(title="victim")
    i = 0
    while True:
        ss.append_turn(s["sid"], "assistant", "z" * 4000)
        i += 1
        if i == 20:
            ready.put(s["sid"])


def scenario_interruption():
    """Kill a writer mid-turn. The database must survive it, and say so.

    The fleet is killed routinely -- a watchdog reset, a machine sleeping, the operator
    closing a window. A store that needs a clean shutdown is a store that will be corrupt.
    """
    base = _box()
    ok = True
    try:
        ready = multiprocessing.Queue()
        p = multiprocessing.Process(target=_killable, args=(base, ready))
        p.start()
        sid = ready.get(timeout=60)
        time.sleep(0.4)
        p.kill()
        p.join(20)

        ss = _store(base)
        conn = sqlite3.connect(ss._db_path())
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        ok &= _say("interruption: database is intact", integrity == "ok", integrity)

        turns = ss.all_turns(sid)
        numbers = [t["turn"] for t in turns]
        ok &= _say("interruption: no torn or duplicate rows",
                   numbers == sorted(set(numbers)),
                   "%d turns, contiguous=%s" % (len(turns),
                                                numbers == list(range(1, len(numbers) + 1))))
        ok &= _say("interruption: the store still accepts writes",
                   ss.append_turn(sid, "user", "after the kill") is None
                   and len(ss.all_turns(sid)) == len(turns) + 1,
                   "wrote one more turn")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return ok


# ── scenario: migration is idempotent under repetition ──────────────────────────

def scenario_reimport(sessions=300):
    """Reopen the store many times over a populated directory. Nothing may duplicate.

    Each bridge start re-runs the import. If it were not idempotent, a turn would be counted
    again on every restart and history would inflate rather than disappear -- the same
    complaint wearing the opposite sign.
    """
    base = _box()
    ok = True
    try:
        import hashlib
        for i in range(sessions):
            sid = "s%010d%04x" % (i, i)
            h = hashlib.sha256(sid.encode("ascii")).hexdigest()
            with open(os.path.join(base, h + ".json"), "w", encoding="utf-8") as fh:
                json.dump({"sid": sid, "title": "t%d" % i, "conv_url": "", "created_ts": 1.0,
                           "last_active_ts": 2.0, "status": "active", "turns": 2,
                           "transcript": "sessions/" + h + ".jsonl", "pending": []}, fh)
            with open(os.path.join(base, h + ".jsonl"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"meta": True, "sid": sid, "ts": 1.0}) + "\n")
                for t in (1, 2):
                    fh.write(json.dumps({"turn": t, "role": "user", "text": "q%d" % t,
                                         "ts": 1.0 + t}) + "\n")
        counts = []
        for _ in range(5):
            ss = _store(base)                          # a fresh "process"
            st = ss.store_stats()
            counts.append((st["sessions"], st["turns"]))
        ok &= _say("reimport: counts never change", len(set(counts)) == 1,
                   "%s across 5 opens" % (counts[0],))
        ok &= _say("reimport: everything arrived", counts[0] == (sessions, sessions * 2),
                   "expected (%d, %d)" % (sessions, sessions * 2))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return ok


SCENARIOS = {
    "volume": scenario_volume,
    "concurrency": scenario_concurrency,
    "churn": scenario_churn,
    "interruption": scenario_interruption,
    "reimport": scenario_reimport,
}


def main(argv=None):                                            # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", choices=sorted(SCENARIOS))
    a = ap.parse_args(argv)
    names = a.only or sorted(SCENARIOS)
    failed = []
    for name in names:
        print("\n== %s ==" % name, flush=True)
        t0 = time.time()
        try:
            good = SCENARIOS[name]()
        except Exception as exc:
            good = False
            print("  [FAIL] raised %s: %s" % (type(exc).__name__, exc), flush=True)
        print("  (%.1fs)" % (time.time() - t0), flush=True)
        if not good:
            failed.append(name)
    print("\n%s  %d/%d scenarios clean"
          % ("ALL CLEAN" if not failed else "FAILED: " + ", ".join(failed),
             len(names) - len(failed), len(names)), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main() or 0)
