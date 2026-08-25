"""Compare what the fleet wrote to disk against what reached the database.

THE JSONL FILES ARE THE KNOWN-GOOD REFERENCE. They are how fleet conversations have been
recorded for the whole life of this project; the database write is three commits old. So the
question is not whether the database looks reasonable, it is whether it holds exactly what the
files hold -- same keys, same turns, same text, nothing added, nothing dropped, nothing
truncated. Anything the files have and the table does not is lost history, which is the
complaint this work exists to fix.

Text is compared in full rather than by length or hash prefix. A store that silently truncated
long answers would pass a length check on short ones and lose exactly the turns worth keeping.

  python scripts/verify_fleet_transcripts.py                     # .fleet/transcripts
  python scripts/verify_fleet_transcripts.py --dir <path> --since <epoch>
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(REPO, ".fleet", "transcripts")


def read_file_turns(path):
    """[(turn, role, text)] for the content lines of one transcript file."""
    out = []
    for line in io.open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("meta") or rec.get("guid"):
            continue
        role = rec.get("role")
        if role not in ("user", "assistant"):
            continue
        out.append((rec.get("turn"), role, rec.get("text") or ""))
    return out


def compare(directory=DEFAULT_DIR, since=0.0):
    from bridge import session_store as ss

    files = sorted(glob.glob(os.path.join(directory, "*.jsonl")))
    if since:
        files = [f for f in files if os.path.getmtime(f) >= since]

    report = {"files": len(files), "keys_checked": 0, "turns_in_files": 0,
              "turns_in_db": 0, "missing": [], "mismatched": [], "extra": []}
    for path in files:
        key = os.path.basename(path)[: -len(".jsonl")]
        want = read_file_turns(path)
        if not want:
            continue
        report["keys_checked"] += 1
        report["turns_in_files"] += len(want)

        rows = [r for r in ss.fleet_turns(key, limit=100000)
                if r["role"] in ("user", "assistant")]
        report["turns_in_db"] += len(rows)
        got = {(r["turn"], r["role"]): r["text"] for r in rows}

        for turn, role, text in want:
            if (turn, role) not in got:
                report["missing"].append({"key": key, "turn": turn, "role": role,
                                          "chars": len(text)})
            elif got[(turn, role)] != text:
                # FULL TEXT, NOT A LENGTH. A store that truncated long answers would pass a
                # length check on the short ones and lose the turns actually worth keeping.
                a, b = text, got[(turn, role)]
                report["mismatched"].append({
                    "key": key, "turn": turn, "role": role,
                    "file_chars": len(a), "db_chars": len(b),
                    "first_diff": next((i for i in range(min(len(a), len(b)))
                                        if a[i] != b[i]), min(len(a), len(b)))})
        seen = {(t, r) for t, r, _x in want}
        for (turn, role) in got:
            if (turn, role) not in seen:
                report["extra"].append({"key": key, "turn": turn, "role": role})
    return report


def main(argv=None):                                            # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--since", type=float, default=0.0,
                    help="only files modified at or after this epoch time")
    a = ap.parse_args(argv)

    rep = compare(a.dir, a.since)
    print(json.dumps({k: v for k, v in rep.items()
                      if k not in ("missing", "mismatched", "extra")},
                     ensure_ascii=False, indent=1))
    clean = True
    for label in ("missing", "mismatched", "extra"):
        rows = rep[label]
        if rows:
            clean = False
            print("\n%s: %d" % (label.upper(), len(rows)))
            for r in rows[:10]:
                print("  %s" % json.dumps(r, ensure_ascii=False))
            if len(rows) > 10:
                print("  ... and %d more" % (len(rows) - 10))
    print("\n%s" % ("IDENTICAL -- every turn on disk is in the database, unchanged"
                    if clean else "DIFFERENCES FOUND"))
    return 0 if clean else 1


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main() or 0)
