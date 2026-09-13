# -*- coding: utf-8 -*-
"""Rebuild `.fleet/history.json` from the ledgers that outlived it.

WHAT WAS LOST, AND WHY IT MATTERS MORE THAN IT LOOKS. `history.json` is the cockpit's archive of
finished workers. Read as a bench ledger it is "the outcome index", which undersells it: each row
carries `conv_url`, and `conv_url` is the only thing that lets a past conversation be REOPENED --
the chat window keys its send target, its steer mode and its snapshot refresh on that field. With
the file gone, every conversation the fleet ever had became unaddressable. That is not a
reporting gap; it is the loss of the ability to resume.

WHAT SURVIVED. Two ledgers nobody was reading for this:

  * `.fleet/socket_route.jsonl` -- 4,280 rows, 3,469 of them `worker_done` carrying
    `conv_client` (the Copilot conversationId), the goal, the outcome, the turn count and a
    timestamp, spanning 2026-08-25 to 2026-09-13.
  * `.fleet/transcripts/` -- 1,557 files, every one with its goal, most of them gzipped.

The socket ledger has MORE conversations than there are transcripts, because retention prunes
transcripts and that file is append-only. So it, not the transcripts, is the spine of the rebuild.

THE PART THE ORIGINAL NEVER HAD. `conv_url` is `""` in every row of the 2026-09-08 rescue copy,
because a socket worker has no page and `_capture_url` was page-only until 2026-09-12. This
rebuild fills it from `conv_client` as `sess:<guid>`, which is the shape
`bridge/copilot_bridge.py` already resumes (load the agent URL, click `button[id=<guid>]`). So
the rebuilt file is more resumable than the one that was lost.

WHAT IT CANNOT PROMISE. Resume works by clicking the conversation's row in Copilot's own sidebar.
An id whose conversation Copilot has since aged out will not have a row to click. This restores
the INDEX; how much of it Copilot still honours is Copilot's retention, not ours.

    python -m tools.rebuild_history            # write .fleet/history.json (refuses to clobber)
    python -m tools.rebuild_history --dry-run  # count what it would write
    python -m tools.rebuild_history --force    # overwrite an existing file
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET = os.path.join(REPO, ".fleet")
SOCKET_ROUTE = os.path.join(FLEET, "socket_route.jsonl")
TRANSCRIPTS = os.path.join(FLEET, "transcripts")
HISTORY = os.path.join(FLEET, "history.json")

#: The synthetic reference the bridge resumes. Imported rather than restated so a change to the
#: scheme cannot leave this writing a shape nothing can open.
try:
    from bridge.copilot_bridge import make_sessref
except Exception:                                   # the bridge is expensive and optional here
    def make_sessref(guid):
        guid = (guid or "").strip()
        return ("sess:" + guid) if guid else ""


def _rows(path):
    """Every JSON object in a .jsonl. Never raises: a truncated tail is a partial answer, not
    a reason to produce none."""
    try:
        for line in io.open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                yield row
    except OSError:
        return


def transcript_index(directory=TRANSCRIPTS):
    """{(goal_prefix, worker): path} for the transcripts still on disk, compressed or not.

    KEYED BY BOTH, and the first version keyed by goal alone. The socket ledger has no run id
    and the filename has no goal, so the goal is what they share -- but a goal run by seven
    workers has seven rows and one transcript per worker, and a goal-only key attached the
    same file to all of them. Measured: 2,077 rows claimed a transcript out of 1,557 files in
    existence. A history row pointing at another worker's transcript is worse than one
    pointing at nothing.

    The worker comes out of the filename (`<run>_a<attempt>_w<n>.jsonl`), which is the only
    place it appears on that side.
    """
    out = {}
    try:
        from relay.fleet_retention import open_maybe_gz
    except Exception:
        return out
    seen = []
    for found in glob.glob(os.path.join(directory, "*.jsonl")) + \
            glob.glob(os.path.join(directory, "*.jsonl.gz")):
        plain = found[:-3] if found.endswith(".gz") else found
        if plain not in seen:
            seen.append(plain)
    for path in seen:
        try:
            for line in open_maybe_gz(path):
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("meta") and row.get("goal"):
                    stem = os.path.basename(path)[:-len(".jsonl")]
                    worker = stem.rsplit("_", 1)[-1] if "_" in stem else ""
                    first_ts = float(row.get("ts") or 0.0)
                    out.setdefault((str(row["goal"]).strip()[:80], worker), []).append(
                        (first_ts, path))
                    break
        except Exception:
            continue
    return out


#: How far a transcript's own first timestamp may sit from the socket ledger's row before the
#: two are not the same worker. Generous, because the two clocks are written at different
#: moments of a turn -- but finite, because the same goal re-run later with the same worker
#: name is a DIFFERENT conversation and linking it would put another run's transcript on this
#: row. A blank is honest; a wrong path is a false record.
TRANSCRIPT_MATCH_WINDOW_S = 6 * 3600.0


def _nearest(candidates, ts):
    """The candidate transcript closest in time to `ts`, or "" when none is close enough."""
    if not candidates:
        return ""
    best_dt, best = None, ""
    for first_ts, path in candidates:
        if not first_ts:
            continue
        dt = abs(first_ts - ts)
        if best_dt is None or dt < best_dt:
            best_dt, best = dt, path
    return best if (best_dt is not None and best_dt <= TRANSCRIPT_MATCH_WINDOW_S) else ""


def build(socket_route=SOCKET_ROUTE, transcripts=TRANSCRIPTS, conv_url=False):
    """The rows a cockpit can load, newest last. One per finished worker that has an id.

    `conv_url=False` keeps the resumable id out of the field the GUI routes on -- see the
    comment at that key. Pass True only once a socket resume path exists in the UI.
    """
    index = transcript_index(transcripts)
    out = []
    for row in _rows(socket_route):
        if row.get("event") != "worker_done":
            continue
        guid = row.get("conv_client") or row.get("conv_server") or ""
        goal = str(row.get("goal") or "").strip()
        ts = float(row.get("ts") or 0.0)
        if not (guid and goal and ts):
            # A row with no id cannot be resumed and a row with no goal cannot be recognised;
            # either way it would be a line in the archive that does nothing.
            continue
        name = str(row.get("worker") or "w?")
        out.append({
            # SYNTHESISED FROM THE TIMESTAMP, because the socket ledger carries no run id and
            # `key` only has to be unique and stable -- the cockpit uses it for the
            # already-archived set. A historical microsecond timestamp cannot collide with a
            # future run's.
            "key": "%r#%s" % (ts, name),
            "goal": goal,
            "status": str(row.get("status") or ""),
            "conv_title": "",
            "outcome": str(row.get("outcome") or ""),
            # EMPTY ON PURPOSE, AND THIS IS THE WHOLE CARE OF THIS FILE. Filling conv_url
            # would restore the id AND route the cockpit onto the page path: CopilotChat's
            # send calls `/switch` for any target with a ConvUrl, and `/switch` is
            # `release_socket_driver()` followed by `_goto_settled(url)` -- it DROPS the
            # websocket and navigates a tab. `/resume` is no better (`PAGE.goto(AGENT_URL)`
            # then click the sidebar row). Neither GUI path resumes over the socket today.
            #
            # So the id goes in a field nothing routes on. `relay/socket_route.driver_for`
            # takes `conversation_id=` and continues a conversation over the websocket -- that
            # was MEASURED on 2026-08-24 -- and when a GUI path reaches it, this is the value
            # it needs. Until then, a populated conv_url would be 3,469 invitations to open a
            # tab.
            "conv_url": make_sessref(guid) if conv_url else "",
            "resume_guid": guid,
            "transcript": _nearest(index.get((goal[:80], name)), ts),
            "name": name,
            "turn": int(row.get("turns") or 0),
            "seq": 0,
            "ts": ts,
            "phase_events": [],
            "run_started": "%r" % ts,
        })
    out.sort(key=lambda r: r["ts"])
    for i, r in enumerate(out):
        r["seq"] = i
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="count, write nothing")
    ap.add_argument("--force", action="store_true", help="overwrite an existing history.json")
    ap.add_argument("--out", default=HISTORY)
    ap.add_argument("--conv-url", action="store_true",
                    help="also fill conv_url, which makes the cockpit resume through /switch "
                         "-- a PAGE path that releases the socket. Only once the UI can "
                         "resume over the websocket.")
    args = ap.parse_args(argv)

    rows = build(conv_url=args.conv_url)
    resumable = sum(1 for r in rows if r["resume_guid"])
    with_tx = sum(1 for r in rows if r["transcript"])
    print("rebuilt rows: %d  (resumable: %d, with a transcript on disk: %d)"
          % (len(rows), resumable, with_tx))
    if args.dry_run:
        return 0
    if os.path.exists(args.out) and not args.force:
        print("refusing to overwrite %s -- pass --force if that is what you mean" % args.out)
        return 2
    # NO BOM: the cockpit writes this file with `new UTF8Encoding(false)` and reads it back with
    # a plain UTF-8 decode. A BOM here would be the first character of the first key.
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rows, fh, ensure_ascii=False)
    print("wrote %s (%d bytes)" % (args.out, os.path.getsize(args.out)))
    print("the cockpit loads history at startup -- restart it to pick this up "
          "(ui/rebuild_ui.ps1, or just relaunch FleetCockpit.exe)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
