"""Record-only A/B assignment for role and protocol definitions (slice D5, first slice).

WHAT DECISION THIS SERVES. Whether the version-A/B machinery (D5) is worth building: before any
prompt is ever swapped, record which arm -- baseline (the definitions a run actually uses) or
proposed -- a run WOULD have landed on if an A/B were running. It follows the shape of
shadow_eval.record_run: at the moment a run starts, write one line, change nothing, and let a
later slice join outcomes by run id.

WHAT THIS DOES NOT DO. A run goes down one path, under the one definition its modules hold. This
slice never swaps a prompt, never changes a role, never changes an admission decision, and never
touches the proposed definition. It only records an arm label. No value of this setting makes a
run take the proposed arm; the proposed arm is run only by a later slice.

SETTING `org_ab`: off | shadow, default off, re-read at the start of every run (each_gate), next to
the version record and the shadow evaluation. Off reads one settings line and writes nothing.

DETERMINISTIC ASSIGNMENT. The arm is a function of run_id alone: the low bit of a sha256 of the
run id. Same run id, same arm, every time -- so a re-run or a report recomputing the arm agrees
with what was written. With no run id there is no stable key, so nothing is written.

THE LINE, one JSON object per run, keys fixed:
  ts, kind ("assign"), run_id, arm ("baseline" | "proposed"), assign_hash (the full sha256 hex of
  the run id, so the arm can be rechecked without rerunning this code).
Never goal text and never any prompt text.

BOUNDED. The file stops growing at MAX_BYTES; one run records at most one line. Append-only,
never rewritten.

FAILURE-SAFE. Every function returns instead of raising; a failed write is counted in
`_STATE["write_errors"]` and the run goes on unchanged.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time

KEY = "org_ab"
MODES = ("off", "shadow")
FILENAME = "org_ab_assign.jsonl"
KIND = "assign"

#: The two arms an A/B would compare: the definitions a run actually uses, and a proposed one.
ARMS = ("baseline", "proposed")

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#: Runtime state, untracked. Tests redirect it (conftest LIVE_RECORD_REDIRECTS).
LOG_PATH = os.path.join(_REPO, ".fleet", FILENAME)

MAX_BYTES = 2 * 1024 * 1024

FIELDS = ("ts", "kind", "run_id", "arm", "assign_hash")

_LOCK = threading.Lock()
_STATE = {"written": 0, "write_errors": 0, "skipped_full": 0, "skipped_no_run": 0}


def _reset_for_test():
    with _LOCK:
        _STATE.update(written=0, write_errors=0, skipped_full=0, skipped_no_run=0)


def mode(settings_path=None) -> str:
    """`org_ab=` from settings.txt: "shadow" or "off". Anything else reads as off."""
    try:
        path = settings_path
        if path is None:
            from tools.settings_path import settings_file
            path = settings_file()
        if not path or not os.path.isfile(path):
            return "off"
        val = "off"
        with io.open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(KEY + "="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = v if v in MODES else "off"
                    break
        return val
    except Exception:
        return "off"


def assign_hash(run_id) -> str:
    """Full sha256 hex of the run id. The arm is derived from it, so a report can recheck the
    arm without rerunning this code."""
    return hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()


def arm_for(run_id) -> str:
    """Deterministic arm for one run id: baseline or proposed, from the low bit of its hash.

    Pure and side-effect free. Returns "" for a missing run id (no stable key to assign on)."""
    if not run_id:
        return ""
    return ARMS[int(assign_hash(run_id), 16) & 1]


def record_run(run_id, settings_path=None, now=None, path=None) -> int:
    """Append one `assign` line for this run. Returns lines written (0 or 1).

    Never raises, never changes its arguments, and writes nothing unless the setting is shadow.
    Changes nothing about the run itself: no prompt is swapped, no admission decision changes."""
    try:
        if mode(settings_path) != "shadow":
            return 0
        arm = arm_for(run_id)
        if not arm:
            with _LOCK:
                _STATE["skipped_no_run"] += 1
            return 0
        ts = float(time.time() if now is None else now)
        rec = {"ts": ts, "kind": KIND, "run_id": str(run_id),
               "arm": arm, "assign_hash": assign_hash(run_id)}
        data = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
        path = path or LOG_PATH
        with _LOCK:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size + len(data.encode("utf-8")) > MAX_BYTES:
                _STATE["skipped_full"] += 1
                return 0
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(data)
            _STATE["written"] += 1
        return 1
    except Exception:
        try:
            with _LOCK:
                _STATE["write_errors"] += 1
        except Exception:
            pass
        return 0


def load(path=None):
    """(assignments, unreadable) from one org_ab_assign.jsonl. Read-only; a missing file is empty.

    assignments  {run id: arm}. A line with an unknown kind is skipped without being counted (a
    newer writer is not damage); a line that is not a JSON object, or lacks a run id, an arm, or a
    numeric ts, is counted in `unreadable`."""
    path = path or LOG_PATH
    assignments, bad = {}, 0
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(obj, dict):
                    bad += 1
                    continue
                if obj.get("kind") != KIND:
                    continue
                run, arm = obj.get("run_id"), obj.get("arm")
                if not run or arm not in ARMS or not isinstance(obj.get("ts"), (int, float)):
                    bad += 1
                    continue
                assignments.setdefault(str(run), str(arm))
    except OSError:
        pass
    return assignments, bad
