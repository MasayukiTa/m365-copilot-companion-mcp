"""Control a RUNNING fleet from the command line by writing .fleet/commands.json -- the same
control channel the cockpit uses (fleet_runner._drain_commands consumes it each sweep).

  python bench/fleet_ctl.py pause      # freeze the fleet in place (no new turns / no new tabs)
  python bench/fleet_ctl.py resume     # un-freeze and continue
  python bench/fleet_ctl.py stop       # graceful abort: cancel all workers and end the run
  python bench/fleet_ctl.py status     # print the live status.json summary (paused? running?)

`pause` is handy right before switching networks: the fleet stops issuing cloud turns and stops
probing the Edge context, so the switch doesn't trip the lost-context recovery; `resume` picks up
from the next poll with NO state lost. Only takes effect while a fleet run is LIVE (otherwise the
command just sits in commands.json until one starts -- same semantics as the cockpit).

MERGE writer: reads the existing commands.json (if the runner hasn't drained it yet) and merges,
so a queued add_goal / set_maxtabs isn't clobbered.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(REPO, ".fleet")
CMDS = os.path.join(STATE, "commands.json")
STATUS = os.path.join(STATE, "status.json")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _merge_command(patch):
    cmd = _read(CMDS)
    if not isinstance(cmd, dict):
        cmd = {}
    cmd.update(patch)
    os.makedirs(STATE, exist_ok=True)
    tmp = CMDS + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cmd, f, ensure_ascii=False)
    os.replace(tmp, CMDS)
    return cmd


def _status():
    d = _read(STATUS)
    if not d:
        print("(no status.json -- no fleet has run, or state dir is elsewhere)")
        return
    ws = d.get("workers", [])
    from collections import Counter
    c = Counter(w.get("status") for w in ws)
    print("running=%s  paused=%s  workers=%d  open_tabs=%s/%s  done=%s/%s"
          % (d.get("running"), d.get("paused"), len(ws), d.get("open_tabs"),
             d.get("max_concurrent"), d.get("done_count"), d.get("total")))
    print("statuses:", dict(c))


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    action = argv[0].lower()
    if action == "pause":
        _merge_command({"pause": True})
        print("PAUSE requested -> fleet will freeze on its next sweep (state retained).")
    elif action in ("resume", "unpause", "continue"):
        _merge_command({"pause": False})
        print("RESUME requested -> fleet will continue on its next sweep.")
    elif action == "stop":
        _merge_command({"stop": True})
        print("STOP requested -> fleet will cancel all workers and end the run.")
    elif action == "status":
        _status()
    else:
        print("unknown action %r (use: pause | resume | stop | status)" % action)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
