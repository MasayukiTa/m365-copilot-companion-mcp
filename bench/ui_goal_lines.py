"""Turn staged fleet goals into lines the cockpit's input box can actually accept.

WHY THIS EXISTS. Testing through the CLI proves the fleet works; it does not prove the cockpit
hands it the same thing, and the gap between those two has bitten here before -- the back end
was correct while the surface was full of errors, and "the tests pass" was true of a path
nobody uses. `scripts/win/submit_via_ui.ps1` exists for that reason and its own header says so.

THE OBSTACLE. The cockpit splits its input box on newlines: one line, one goal. A SWE goal is a
problem statement of a thousand-odd characters across many lines, so typed in directly it does
not become one goal -- it becomes dozens of fragments, each dispatched as its own task. That is
not a formatting inconvenience; it is a run that looks like it started and is measuring
something else entirely.

THE SEAM THE COCKPIT ALREADY HAS. `GoalsToJsonl` passes a line through UNCHANGED when it parses
as a JSON object, because the Continue flow needs keys beyond `text`. So a multi-line goal
survives as one goal if it arrives already serialised, with its newlines escaped -- exactly one
physical line, carrying exactly one task.
"""
from __future__ import annotations

import io
import json


def to_ui_lines(goals):
    """[goal text] -> [one JSON line each], safe to paste into the cockpit's box.

    Each line is `{"text": "..."}` with newlines escaped by the JSON encoder, so the cockpit's
    newline split cannot break a goal apart and its JSON passthrough keeps it whole.
    """
    out = []
    for g in goals or []:
        if isinstance(g, dict):
            obj = dict(g)
        else:
            obj = {"text": str(g)}
        line = json.dumps(obj, ensure_ascii=False)
        assert "\n" not in line, "a serialised goal must be exactly one line"
        out.append(line)
    return out


def from_goals_file(path):
    """Read a staged goals JSONL (fleet_runner's own format) and return cockpit-ready lines."""
    goals = []
    for raw in io.open(path, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = {"text": raw}
        if isinstance(obj, dict):
            goals.append(obj)
        else:
            goals.append({"text": str(obj)})
    return to_ui_lines(goals)


def write_ui_file(goals_file, out_path):
    lines = from_goals_file(goals_file)
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        for l in lines:
            fh.write(l + "\n")
    return len(lines)
