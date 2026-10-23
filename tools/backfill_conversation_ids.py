"""Give the registry rows that have no Copilot id the id their transcript already records.

WHY. .fleet/conversations.json rows are keyed by "url" ("sess:<guid>"). A row written before the
conversation's guid was known has url "". Measured 2026-10-23 on 2,978 rows: 2,235 sess ids, 136
http urls, 607 empty -- and for 602 of those 607 the transcript file has no guid line either, so
there is nothing to fill in. Those rows are identified by their transcript file's stem
("r6acf1938_a0_w1"), which is unique per run x attempt x worker; the worker name never is.
The remaining few DO have a guid line in their transcript; this script writes it into "url".

SAFE BY CONSTRUCTION.
  * Default is a dry run: nothing is written. Pass --apply to write.
  * Only a row with an EMPTY url is touched, and only its "url" field is set. No row is removed,
    reordered or merged, and no other field changes.
  * If the guid is already the url of another row, the row is left alone and reported (setting it
    would make two rows claim one conversation).
  * --apply first copies the file to conversations.json.bak-<epoch> (the original is never
    overwritten without a copy), writes through a temp file + os.replace, then re-reads and
    checks: row count unchanged, empty-url count dropped by exactly the number set, sess count
    rose by exactly that number. A failed check restores the copy and exits non-zero.

  python -I -X utf8 tools/backfill_conversation_ids.py [--fleet-dir C:\\...\\.fleet] [--apply]
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import time


def transcript_guid(path: str) -> str:
    """The guid recorded in a transcript's first 40 lines ({"guid": ...}), "" when none/unreadable.
    Reads x.jsonl or x.jsonl.gz, whichever exists."""
    for p in (path, path + ".gz", path[:-3] if path.endswith(".gz") else ""):
        if not p or not os.path.isfile(p):
            continue
        try:
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt", encoding="utf-8", errors="replace") as fh:
                for _ in range(40):
                    ln = fh.readline()
                    if not ln:
                        break
                    if '"guid"' not in ln or '"role"' in ln:
                        continue
                    try:
                        g = json.loads(ln).get("guid")
                    except Exception:
                        continue
                    if g:
                        return str(g)
        except OSError:
            return ""
        return ""
    return ""


def plan(rows, resolve=transcript_guid):
    """Pure: -> (updates, skipped). updates = [(index, "sess:<guid>")]; skipped = [(index, why)]."""
    taken = {r.get("url") for r in rows if isinstance(r, dict) and r.get("url")}
    updates, skipped = [], []
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or r.get("url"):
            continue
        tr = r.get("transcript") or ""
        chain = [str(x) for x in (r.get("transcripts") or []) if x]
        guid = ""
        for cand in ([tr] if tr else []) + chain:
            guid = resolve(cand)
            if guid:
                break
        if not guid:
            skipped.append((i, "no guid in the transcript (identified by its file stem)"))
            continue
        url = "sess:" + guid
        if url in taken:
            skipped.append((i, "another row already has %s" % url))
            continue
        taken.add(url)
        updates.append((i, url))
    return updates, skipped


def counts(rows):
    c = {"rows": len(rows), "empty_url": 0, "sess": 0, "http": 0}
    for r in rows:
        u = (r.get("url") or "") if isinstance(r, dict) else ""
        if not u:
            c["empty_url"] += 1
        elif u.startswith("sess:"):
            c["sess"] += 1
        else:
            c["http"] += 1
    return c


def run(fleet_dir: str, apply: bool = False, resolve=transcript_guid, out=print) -> int:
    path = os.path.join(fleet_dir, "conversations.json")
    with open(path, encoding="utf-8-sig") as fh:
        rows = json.load(fh)
    before = counts(rows)
    updates, skipped = plan(rows, resolve)
    out("before: %s" % before)
    out("would set url on %d row(s); leave %d row(s) as they are" % (len(updates), len(skipped)))
    for i, why in skipped[:5]:
        out("  row %d: %s" % (i, why))
    if not apply:
        out("dry run -- nothing written (use --apply)")
        return 0
    if not updates:
        out("nothing to write")
        return 0
    backup = "%s.bak-%d" % (path, int(time.time()))
    shutil.copy2(path, backup)
    for i, url in updates:
        rows[i]["url"] = url
    tmp = path + ".tmp-backfill"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:       # no BOM
        json.dump(rows, fh, ensure_ascii=False)
    os.replace(tmp, path)
    with open(path, encoding="utf-8-sig") as fh:
        after_rows = json.load(fh)
    after = counts(after_rows)
    out("after:  %s" % after)
    ok = (after["rows"] == before["rows"]
          and after["empty_url"] == before["empty_url"] - len(updates)
          and after["sess"] == before["sess"] + len(updates)
          and after["http"] == before["http"])
    if not ok:
        shutil.copy2(backup, path)
        out("CHECK FAILED -- the original was restored from %s" % backup)
        return 1
    out("ok. backup: %s" % backup)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fleet-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".fleet"))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    return run(a.fleet_dir, a.apply)


if __name__ == "__main__":
    sys.exit(main())
