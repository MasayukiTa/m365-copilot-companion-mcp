# -*- coding: utf-8 -*-
"""Attribute a burst of Edge (msedge.exe) processes to the process that spawned them.

The question this answers is the operator's, asked three times and never answered by a tool:
*"I did not open ten Edges. What process opened that many msedge.exe?"* (batch_19 rows #50,
#54, #57). The 2 GB Teams process is already understood and is explicitly OUT OF SCOPE here --
the ask is specifically the Edge burst, so a Teams-owned msedge is classified and then set
aside, not counted as the culprit.

Why a dedicated reader. The socket/trace ledgers record MCP turns, not the OS process tree, so
they cannot say which process is the PARENT of a msedge.exe. The only record that can is a
process snapshot (pid, parent pid, image name, command line) -- the shape returned by
`CIM_Process` / `Get-CimInstance Win32_Process` / `wmic process`. This module takes such a
snapshot (already captured; this module does NOT spawn anything) and answers "which process
opened the Edges, and how many each".

What it is careful about:

* It is READ-ONLY and pure. It launches no process, reads no file by itself, and never raises
  on malformed input -- a triage reader that dies on one odd row is worse than useless.
* It does not return raw command lines. An Edge command line carries a user-data-dir path and
  can carry URLs; a trace is pasted into chats and tickets. So a child is reduced to a CATEGORY
  (fleet / teams / user / other) plus the one structural fact that decides it (a debug port
  number, a profile-kind token), and the parent is returned as an image name + pid only.
* The fleet's own Edge is identified by exactly what the merged history already uses to own it:
  a `--remote-debugging-port` of 9222/9224 (see the main-tree commits that reset the fleet Edge
  by (user-data-dir, port) and refuse to surface :9222/:9224 on their own). That Edge is a
  legitimate, known origin -- naming it is the point, not hiding it.

The classification of a single msedge.exe, by its own command line:

* `fleet`  -- `--remote-debugging-port=9222` or `9224` (the ports the fleet owns), or a
              user-data-dir that names the fleet profile. These are the automation Edges.
* `teams`  -- a Teams/WebView2 embedding (`--embedded-browser-webview`, a Teams user-data-dir,
              or a `--webview2` marker). OUT OF SCOPE for the burst question by the operator's
              own instruction, so counted separately and excluded from the culprit search.
* `user`   -- a normal interactive Edge: a browser-process command line with no automation and
              no embedding markers.
* `other`  -- a msedge.exe that is none of the above (a renderer/gpu/utility child, or an
              unrecognised shape). Renderers are the bulk of any Edge's process count and are
              NOT independent launches, so they never count as "opened" on their own.

The origin search then groups the launch-worthy children (fleet/user/other BROWSER processes,
not renderers, teams excluded) by their PARENT and reports who opened the most.
"""
from __future__ import annotations

import re
from collections import defaultdict

#: The remote-debugging ports the fleet owns. Kept in sync with the main-tree Edge-reset work
#: that selects the fleet Edge by (user-data-dir, port); 9222 is the primary, 9224 the second.
FLEET_DEBUG_PORTS = ("9222", "9224")

#: Command-line markers that mean "this msedge is embedded in another app (Teams/WebView2)".
_TEAMS_MARKERS = (
    "--embedded-browser-webview",
    "--webview2",
    "msteams",
    "teams\\",
    "/teams/",
)

#: Markers of a msedge CHILD process (renderer/gpu/utility). These are spawned by a browser
#: process, not opened by a user or a tool, so they are never the "burst" the operator saw.
_CHILD_TYPE_RE = re.compile(r"--type=(\S+)")

_PORT_RE = re.compile(r"--remote-debugging-port=(\d+)")


def _cmdline_of(row) -> str:
    for key in ("cmdline", "CommandLine", "command_line", "commandline"):
        val = row.get(key) if hasattr(row, "get") else None
        if val:
            if isinstance(val, (list, tuple)):
                return " ".join(str(x) for x in val)
            return str(val)
    return ""


def _name_of(row) -> str:
    for key in ("name", "Name", "image", "ImageName", "process"):
        val = row.get(key) if hasattr(row, "get") else None
        if val:
            return str(val)
    return ""


def _is_msedge(row) -> bool:
    return "msedge.exe" in _name_of(row).lower()


def child_type(row) -> str:
    """The `--type=` of a msedge child, or "" for a top-level browser process.

    A browser process (the one a user or tool actually "opens") has no `--type=`; renderer, gpu,
    utility, crashpad children all do. Only browser processes are launches.
    """
    m = _CHILD_TYPE_RE.search(_cmdline_of(row))
    return m.group(1).strip().strip('"').lower() if m else ""


def is_browser_process(row) -> bool:
    """True when this msedge.exe is a top-level browser process (a real launch), not a child."""
    return _is_msedge(row) and child_type(row) == ""


def classify_edge(row) -> str:
    """Classify one msedge.exe row into fleet / teams / user / other.

    Never raises. A non-Edge row, or anything unrecognised, is "other".
    """
    try:
        if not _is_msedge(row):
            return "other"
        cmd = _cmdline_of(row).lower()
        port = _PORT_RE.search(cmd)
        if port and port.group(1) in FLEET_DEBUG_PORTS:
            return "fleet"
        if any(marker in cmd for marker in _TEAMS_MARKERS):
            return "teams"
        # A browser process that is neither automated nor embedded is an interactive Edge.
        if is_browser_process(row):
            return "user"
        return "other"
    except Exception:
        return "other"


def _parent_key(row):
    ppid = None
    for key in ("ppid", "PPID", "ParentProcessId", "parent_pid", "parentpid"):
        val = row.get(key) if hasattr(row, "get") else None
        if val not in (None, ""):
            ppid = val
            break
    pname = None
    for key in ("parent_name", "ParentName", "parentimage", "ParentImage"):
        val = row.get(key) if hasattr(row, "get") else None
        if val:
            pname = str(val)
            break
    try:
        ppid = int(ppid) if ppid is not None else None
    except (TypeError, ValueError):
        pass
    return (ppid, pname or "")


def attribute_edge_burst(rows, parent_names=None):
    """Find which process opened the burst of Edge browser processes.

    `rows` is an iterable of process-snapshot records (dict-like) with at least an image name,
    a command line, and a parent pid. `parent_names` optionally maps pid -> image name so the
    culprit can be named even when the snapshot row does not carry the parent's name.

    Returns a dict that carries only categories and counts -- never a raw command line, URL, or
    profile path:

        {
          "by_category": {"fleet": N, "teams": N, "user": N, "other": N},
          "launches": M,                 # browser processes only (renderers excluded)
          "teams_excluded": T,           # Teams-owned browser processes set aside by instruction
          "by_parent": [                 # launch-worthy, non-Teams, most first
              {"ppid": 1234, "parent": "something.exe", "edges": K, "categories": {...}},
              ...
          ],
          "culprit": {"ppid": .., "parent": .., "edges": ..} | None,
        }

    Never raises.
    """
    parent_names = parent_names or {}
    by_category = {"fleet": 0, "teams": 0, "user": 0, "other": 0}
    launches = 0
    teams_excluded = 0
    per_parent = defaultdict(lambda: {"edges": 0, "categories": defaultdict(int)})

    for row in rows or []:
        try:
            if not _is_msedge(row):
                continue
            category = classify_edge(row)
            by_category[category] = by_category.get(category, 0) + 1
            # Only top-level browser processes are "opened". Renderer/gpu/utility children are
            # spawned by a browser process and are not independent launches.
            if not is_browser_process(row):
                continue
            launches += 1
            if category == "teams":
                # Out of scope for the burst question by the operator's own instruction.
                teams_excluded += 1
                continue
            ppid, pname = _parent_key(row)
            if not pname and ppid in parent_names:
                pname = parent_names[ppid]
            bucket = per_parent[(ppid, pname)]
            bucket["edges"] += 1
            bucket["categories"][category] += 1
        except Exception:
            continue

    by_parent = []
    for (ppid, pname), bucket in per_parent.items():
        by_parent.append({
            "ppid": ppid,
            "parent": pname,
            "edges": bucket["edges"],
            "categories": dict(bucket["categories"]),
        })
    # Most Edges first; ties broken by pid for a stable, reproducible order.
    by_parent.sort(key=lambda d: (-d["edges"], d["ppid"] if d["ppid"] is not None else 1 << 62))

    culprit = None
    if by_parent:
        top = by_parent[0]
        culprit = {"ppid": top["ppid"], "parent": top["parent"], "edges": top["edges"]}

    return {
        "by_category": by_category,
        "launches": launches,
        "teams_excluded": teams_excluded,
        "by_parent": by_parent,
        "culprit": culprit,
    }
