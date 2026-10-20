"""Shadow evaluation of change proposals for role and protocol definitions (slice D4).

WHAT DECISION THIS SERVES. Whether a proposal on file (relay/org/proposals.py) is worth the cost
of a live organisation A/B (D5): how many runs it would have touched, and how those runs went
under the definition they actually ran under. It follows the shape of effort_policy.shadow_assign:
at the moment a run starts, record what the proposed definition WOULD have been applied to, change
nothing, and let a report join the outcome later.

WHAT SHADOW CANNOT DO. A run goes down one path. The path under a proposed definition was not
taken, so there is no outcome for it, and nothing here can show a proposal is better. The
evidence is the count of runs a proposal would have touched and the BASELINE outcome of those
runs per (role, kind, org version). Deciding that a proposal is better needs the proposed arm
actually run (D5), which is why the report also says how much data that needs.

SETTING `org_shadow_eval`: off | shadow, default off, read once when a run starts (sweep_start),
next to the version record. `on` is NOT implemented: no value of this setting makes a prompt, a
role or an admission read a proposal. Off reads one settings line and writes nothing.

THE LINE, one JSON object per (run, proposal), keys fixed:
  ts, kind ("would_touch"), run_id, proposal (id), target, base_hash, proposed_hash,
  roles_hash, workflows_hash.
Never goal text and never the proposed text: the proposal id is a hash of (target, base hash,
proposed hash), and the text stays on its decision card (D3). The outcome and the turn count are
NOT written here: they do not exist when a run starts. The report joins them by run id from the
records the fleet already keeps, the way the version record is joined.

ONLY PROPOSALS THAT STILL APPLY. A proposal whose base hash is no longer the definition's hash was
written against older wording; it is not recorded for new runs (the report lists it as stale).

BOUNDED. The file stops growing at MAX_BYTES; one run records at most MAX_PER_RUN proposals.
Append-only, never rewritten.

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

KEY = "org_shadow_eval"
MODES = ("off", "shadow")
FILENAME = "org_shadow_eval.jsonl"
KIND = "would_touch"

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#: Runtime state, untracked. Tests redirect it (conftest LIVE_RECORD_REDIRECTS).
LOG_PATH = os.path.join(_REPO, ".fleet", FILENAME)

MAX_BYTES = 2 * 1024 * 1024
MAX_PER_RUN = 25

FIELDS = ("ts", "kind", "run_id", "proposal", "target", "base_hash", "proposed_hash",
          "roles_hash", "workflows_hash")

#: Which recorded roles (capability_profile) a proposal's target touches. A target not named
#: here touches the whole run (a planner, splitter or refuter turn, or a protocol every worker
#: receives, belongs to the run, not to one worker record).
TARGET_ROLES = {"role:aggregator": ("aggregator",)}

_LOCK = threading.Lock()
_STATE = {"written": 0, "write_errors": 0, "skipped_full": 0, "stale": 0}


def _reset_for_test():
    with _LOCK:
        _STATE.update(written=0, write_errors=0, skipped_full=0, stale=0)


def mode(settings_path=None) -> str:
    """`org_shadow_eval=` from settings.txt: "shadow" or "off". Anything else reads as off
    (and `on` is not a mode)."""
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


def proposal_id(target, base_hash, proposed_hash) -> str:
    """Short stable id of one proposal, made of hashes only."""
    raw = "\n".join((str(target), str(base_hash), str(proposed_hash)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def record_run(run_id, settings_path=None, now=None, proposals_path=None, path=None) -> int:
    """Append one `would_touch` line per proposal that still applies. Returns lines written.

    Never raises, never changes its arguments, and writes nothing unless the setting is shadow."""
    try:
        if mode(settings_path) != "shadow":
            return 0
        from relay.org import definitions, proposals
        recs, _bad = proposals.read(proposals_path)
        if not recs:
            return 0
        snap = definitions.snapshot()
        current = proposals.targets(snap)
        ts = float(time.time() if now is None else now)
        lines = []
        for r in recs:
            if current.get(r["target"], "") != r["base_hash"]:
                with _LOCK:
                    _STATE["stale"] += 1
                continue
            if len(lines) >= MAX_PER_RUN:
                break
            lines.append({"ts": ts, "kind": KIND, "run_id": str(run_id or ""),
                          "proposal": proposal_id(r["target"], r["base_hash"], r["proposed_hash"]),
                          "target": r["target"], "base_hash": r["base_hash"],
                          "proposed_hash": r["proposed_hash"],
                          "roles_hash": snap["roles_hash"],
                          "workflows_hash": snap["workflows_hash"]})
        if not lines:
            return 0
        data = "".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in lines)
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
            _STATE["written"] += len(lines)
        return len(lines)
    except Exception:
        try:
            with _LOCK:
                _STATE["write_errors"] += 1
        except Exception:
            pass
        return 0


def load(path=None):
    """(touches, unreadable) from one org_shadow_eval.jsonl. Read-only; a missing file is empty.

    touches  {proposal id: {run id: first ts}}. A line with an unknown kind is skipped without
    being counted (a newer writer is not damage); a line that is not a JSON object, or lacks a
    run id or proposal id, is counted in `unreadable`."""
    path = path or LOG_PATH
    touches, bad = {}, 0
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
                pid, run = obj.get("proposal"), obj.get("run_id")
                if not pid or not run or not isinstance(obj.get("ts"), (int, float)):
                    bad += 1
                    continue
                touches.setdefault(str(pid), {}).setdefault(str(run), float(obj["ts"]))
    except OSError:
        pass
    return touches, bad
