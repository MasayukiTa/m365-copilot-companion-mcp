"""Has a tool call actually reached this server through the agent path, recently?

WHY THIS EXISTS. On 2026-08-31 I restarted the MCP server -- the process the tunnel forwards
to -- and started a 40-instance benchmark ninety seconds later without checking anything. A
Copilot session created while the backend is down gets no tool map and never retries: ten of
the eighteen workers in that run had only AskTool, CaptureContextTool and UniversalSearchTool,
reported STUCK, and the run produced patches written from memory. Nothing in the run's own
logs said the tool path was down; the only account of it was a worker saying so in prose.

`tool_inbound` is the one that matters. `tool_ok` can be true on a reply that came back
without the call ever arriving here, and the failure this guards against is precisely a
connector path that is up for text and down for tools.

Exit code 0 when the path is proven, 1 when it is not. Prints a line either way.
"""
import argparse
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def state(max_age_s):
    from tools.tool_probe import get_summary
    s = get_summary()
    age = s.get("tool_age_s")
    fresh = isinstance(age, (int, float)) and age <= max_age_s
    return s, fresh


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-sec", type=int, default=300,
                    help="how long to wait for proof before giving up")
    ap.add_argument("--max-age-sec", type=int, default=900,
                    help="evidence older than this is not evidence about now")
    a = ap.parse_args(argv)

    deadline = time.time() + a.wait_sec
    last = None
    while True:
        s, fresh = state(a.max_age_sec)
        last = s
        if fresh and s.get("tool_inbound") is True:
            print("tool path proven: a probe's own call reached this server %.0fs ago"
                  % (s.get("tool_age_s") or 0))
            return 0
        if time.time() >= deadline:
            break
        time.sleep(15)

    print("TOOL PATH NOT PROVEN. Last probe: ok=%r kind=%r inbound=%r alive=%r age=%s"
          % (last.get("tool_ok"), last.get("tool_kind"), last.get("tool_inbound"),
             last.get("tool_alive"),
             ("%.0fs" % last["tool_age_s"]) if isinstance(last.get("tool_age_s"), (int, float))
             else "none"))
    print("A run started now would produce workers with no tools, which report STUCK and")
    print("write patches from memory -- and nothing in the run's logs would say why.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
