# -*- coding: utf-8 -*-
"""Give past fleet transcripts back the conversation they ran in.

A socket worker never recorded its conversation identity -- `_capture_url`'s body sat entirely
under `if self.page is not None` and a socket worker has no page. Fixed 2026-09-12 (see
docs/incidents/20260912_a_fleet_conversation_could_be_read_and_never_answered.md), but only for
runs from that point on: every transcript written before it has no `guid` line, so the chat
window cannot address those conversations and they stay read-only forever.

The identity was not lost, only unrecorded in the place that is read. `.fleet/socket_route.jsonl`
holds `worker_done` events carrying `conv_client`, keyed by goal text -- the same key
`socket_route.conversation_for_goal` already matches on. So a transcript whose goal appears
there can be handed its own id back.

WHERE THE LINE GOES. Immediately after the meta line, which is where `_tx.note_guid` would have
put it: the chat window scans a bounded window of leading lines for it, so appending at the end
of a long transcript would write a line nothing reads. That means a rewrite rather than an
append, so this refuses to touch a transcript whose worker might still be writing.

DRY RUN BY DEFAULT. Pass --apply to write.

    python tools/backfill_transcript_conv_ids.py            # say what would change
    python tools/backfill_transcript_conv_ids.py --apply    # do it
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STATE = os.path.join(REPO, ".fleet")

#: socket_route.conversation_for_goal truncates the goal to 600 characters before matching, and
#: this has to match the same way or a long goal would never join.
GOAL_KEY_LEN = 600


def goal_key(goal) -> str:
    return (goal or "").strip()[:GOAL_KEY_LEN]


#: How long after a transcript's last line its worker_done may be written and still be the
#: same run. A worker is recorded as done immediately after its final turn; the observed gaps
#: on real data were 0, 82 and 116 seconds, while the nearest WRONG candidate was 1221 away.
MATCH_WINDOW_S = 600.0


def worker_done_rows(log_path: str) -> list:
    """Every worker_done that carries a conversation id, as (goal_key, worker, ts, conv_id).

    NOT A goal -> id DICT. That was the first version, mirroring conversation_for_goal's
    "newest wins" -- correct for ITS question (a follow-up continues the latest run of a goal)
    and wrong for this one (which conversation did THIS transcript run in). Measured: it
    stamped a run from 09:06 onto a transcript that finished at 08:46. A wrong id is worse
    than none, because the chat window then opens a real conversation that is not this one.

    A torn tail is normal (several processes append), so an unparseable line is skipped rather
    than treated as the end of the file.
    """
    out = []
    try:
        fh = io.open(log_path, encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict) or rec.get("event") != "worker_done":
                continue
            cid = str(rec.get("conv_client") or "").strip()
            if not cid:
                continue
            try:
                ts = float(rec.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            out.append((goal_key(rec.get("goal")), str(rec.get("worker") or "").strip(),
                        ts, cid))
    return out


def resolve(rows, goal: str, worker: str, last_ts: float):
    """The conversation id for one transcript, or "" when it cannot be told apart.

    worker_done is written when the worker finishes, so the row belonging to this transcript is
    the one whose timestamp falls just AFTER its last line. Smallest non-negative gap wins;
    anything beyond MATCH_WINDOW_S, or nothing after it at all, is refused rather than guessed.
    """
    if not goal or not last_ts:
        return ""
    best, best_gap = "", None
    for g, w, ts, cid in rows:
        if g != goal:
            continue
        if worker and w and w != worker:
            continue
        gap = ts - last_ts
        if gap < 0 or gap > MATCH_WINDOW_S:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = cid, gap
    return best


def read_head(path: str, limit: int = 400):
    """(meta, has_guid, lines, last_ts). Reads the whole file: transcripts are small, and a
    guid sitting past an arbitrary cutoff would be written twice.

    `last_ts` is the newest ts on any line -- when the transcript stopped being written, which
    is what the worker_done row has to sit just after. File mtime is NOT used for this: a
    back-fill rewrites the file and would then be matching against its own edit.
    """
    lines = []
    meta = None
    has_guid = False
    last_ts = 0.0
    with io.open(path, encoding="utf-8") as fh:
        for line in fh:
            lines.append(line)
            s = line.strip()
            if not s:
                continue
            try:
                d = json.loads(s)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if meta is None and d.get("meta"):
                meta = d
            if "guid" in d:
                has_guid = True
            try:
                last_ts = max(last_ts, float(d.get("ts") or 0))
            except (TypeError, ValueError):
                pass
    return meta, has_guid, lines, last_ts


def live_transcripts(state_dir: str) -> set:
    """Transcript paths belonging to a worker that is not terminal in the current status.json.

    Rewriting one of those could interleave with the worker's own append. When status.json says
    the run is over, nothing holds them.
    """
    live = set()
    try:
        with io.open(os.path.join(state_dir, "status.json"), encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return live
    if not d.get("running"):
        return live
    terminal = {"done", "resolved", "failed", "error", "cancelled", "stopped", "stuck"}
    for w in (d.get("workers") or []):
        if not isinstance(w, dict):
            continue
        if (w.get("status") or "").strip() in terminal:
            continue
        t = (w.get("transcript") or "").strip()
        if t:
            live.add(os.path.normcase(os.path.abspath(t)))
    return live


def backfill(state_dir: str, apply: bool = False, out=print) -> dict:
    tdir = os.path.join(state_dir, "transcripts")
    rows = worker_done_rows(os.path.join(state_dir, "socket_route.jsonl"))
    busy = live_transcripts(state_dir)
    tally = {"total": 0, "already": 0, "written": 0, "no_id": 0, "no_goal": 0,
             "skipped_live": 0, "ambiguous": 0}

    for path in sorted(glob.glob(os.path.join(tdir, "*.jsonl"))):
        if "__sub_" in os.path.basename(path):
            continue          # research children nest under their parent
        tally["total"] += 1
        name = os.path.basename(path)
        try:
            meta, has_guid, lines, last_ts = read_head(path)
        except OSError as exc:
            out("  UNREADABLE %s: %s" % (name, exc))
            continue
        if has_guid:
            tally["already"] += 1
            continue
        goal = goal_key((meta or {}).get("goal"))
        if not goal:
            tally["no_goal"] += 1
            out("  no goal text, cannot identify: %s" % name)
            continue
        if not last_ts:
            tally["ambiguous"] += 1
            out("  no timestamps, cannot tell which run this is: %s" % name)
            continue
        cid = resolve(rows, goal, (meta or {}).get("name") or "", last_ts)
        if not cid:
            tally["no_id"] += 1
            out("  no recorded conversation within %.0fs of its last line: %s"
                % (MATCH_WINDOW_S, name))
            continue
        if os.path.normcase(os.path.abspath(path)) in busy:
            tally["skipped_live"] += 1
            out("  SKIPPED (worker still running): %s" % name)
            continue
        out("  %s -> %s%s" % (name, cid, "" if apply else "   [dry run]"))
        if not apply:
            continue
        # After the meta line, where note_guid would have put it. Written through a temp file
        # so an interrupted run cannot leave a half transcript.
        insert = json.dumps({"guid": cid, "ts": 0, "backfilled": True},
                            ensure_ascii=False) + "\n"
        at = 1 if lines else 0
        body = lines[:at] + [insert] + lines[at:]
        tmp = path + ".backfill"
        with io.open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(body))
        os.replace(tmp, path)
        tally["written"] += 1

    out("transcripts=%d already=%d written=%d no_id=%d no_goal=%d skipped_live=%d "
        "ambiguous=%d"
        % (tally["total"], tally["already"], tally["written"], tally["no_id"],
           tally["no_goal"], tally["skipped_live"], tally["ambiguous"]))
    return tally


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", default=DEFAULT_STATE)
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    a = ap.parse_args(argv)
    backfill(a.state_dir, apply=a.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
