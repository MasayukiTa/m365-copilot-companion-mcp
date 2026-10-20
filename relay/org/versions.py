"""One line per run saying which role and protocol versions it ran under (slices D1 and D2).

WHAT DECISION THIS SERVES. Whether a change to a prompt (a role's wording, the worker protocol)
moved the outcome of runs. The fleet's records name the run but not the wording it ran under, so
runs from before and after a prompt edit cannot be separated. This adds that name, beside the
run, without touching harness_id (which lives in the frozen relay/selfimprove/manifest.py).

SETTING `org_version_record`: off | on, default off, read once when a run starts (sweep_start).
Off reads one settings line and writes nothing. On appends to `<state dir>/org_versions.jsonl`.
The state dir is the parent of the run's transcript directory, so a caller without one records
nothing, and no path to the operator's .fleet is fixed in this module.

WHAT IS WRITTEN. Two kinds of line, both small, never goal text:
  {"kind":"defs", "ts", "roles_hash", "workflows_hash", "roles":{name:{version,hash}},
   "workflows":{...}}                      once per process per distinct set of definitions
  {"kind":"run",  "ts", "run_id", "roles_hash", "workflows_hash"}      once per run
`run_id` is the id relay_fleet.run_relay_fleet was given (`r<hex>_a<attempt>`), the id the
transcripts and mechanism rows carry, so it joins to them. A reader resolves a run's definitions
through its hashes.

BOUNDED. The file stops growing at MAX_BYTES (about 17,000 runs). Past that nothing is written and
`_STATE["skipped_full"]` counts it. It is append-only and is never rewritten or trimmed here.

FAILURE-SAFE. Every function returns instead of raising. A write that fails is counted in
`_STATE["write_errors"]` and the run goes on unchanged.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time

KEY = "org_version_record"
MODES = ("off", "on")
FILENAME = "org_versions.jsonl"
MAX_BYTES = 2 * 1024 * 1024

#: A second, independent setting (relay/org/definitions.genotype_id). off|on, default off, read
#: once when a run starts, exactly beside org_version_record. When on, record_run adds a single
#: genotype_id field to the defs and run lines it already writes -- one name for the pair of
#: hashes a run ran under. It never changes which lines are written or anything else about them,
#: so load reads the lines whether or not the field is there. off reads one settings line and
#: adds nothing.
GENOTYPE_KEY = "org_genotype_record"
GENOTYPE_MODES = ("off", "on")

_LOCK = threading.Lock()
_STATE = {"written_runs": 0, "write_errors": 0, "skipped_full": 0}
_DEFS_WRITTEN = set()          # (path, roles_hash, workflows_hash) already in this process

_ATTEMPT = re.compile(r"_a\d+$")


def _reset_for_test():
    with _LOCK:
        _STATE.update(written_runs=0, write_errors=0, skipped_full=0)
        _DEFS_WRITTEN.clear()


def mode(settings_path=None) -> str:
    """`org_version_record=` from settings.txt: "on" or "off". Anything else reads as off."""
    return _read_mode(KEY, MODES, settings_path)


def genotype_mode(settings_path=None) -> str:
    """`org_genotype_record=` from settings.txt: "on" or "off". Anything else reads as off.

    Read once beside `mode` when a run starts. on makes record_run add the genotype_id field;
    it is a separate switch so naming the version and recording the run can be turned on apart."""
    return _read_mode(GENOTYPE_KEY, GENOTYPE_MODES, settings_path)


def _read_mode(key, modes, settings_path=None) -> str:
    """One `key=` value from settings.txt, clamped to `modes` with off as the fallback."""
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
                if ln.startswith(key + "="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = v if v in modes else "off"
                    break
        return val
    except Exception:
        return "off"


def record_path(transcript_dir):
    """Where this run's record goes: beside the transcript directory. None if there is none."""
    if not transcript_dir:
        return None
    return os.path.join(os.path.dirname(os.path.abspath(str(transcript_dir))), FILENAME)


def record_run(run_id, transcript_dir, settings_path=None, now=None) -> bool:
    """Append the definitions in force and this run's id. True iff a run line was written.

    Never raises, never changes its arguments, and writes nothing unless the setting is on."""
    try:
        if mode(settings_path) != "on":
            return False
        path = record_path(transcript_dir)
        if not path:
            return False
        from relay.org import definitions
        snap = definitions.snapshot()
        ts = float(time.time() if now is None else now)
        # One name for the pair of hashes, added only when org_genotype_record is on. Empty when
        # a part could not be read, so an unreadable version is never given a real id (and the
        # field is simply omitted when the setting is off).
        want_gid = genotype_mode(settings_path) == "on"
        gid = definitions.genotype_id(snap) if want_gid else ""
        lines = []
        key = (path, snap["roles_hash"], snap["workflows_hash"])
        with _LOCK:
            first = key not in _DEFS_WRITTEN
        if first:
            defs_line = {"kind": "defs", "ts": ts, "roles_hash": snap["roles_hash"],
                         "workflows_hash": snap["workflows_hash"],
                         "roles": snap["roles"], "workflows": snap["workflows"]}
            if want_gid:
                defs_line["genotype_id"] = gid
            lines.append(defs_line)
        run_line = {"kind": "run", "ts": ts, "run_id": str(run_id or ""),
                    "roles_hash": snap["roles_hash"],
                    "workflows_hash": snap["workflows_hash"]}
        if want_gid:
            run_line["genotype_id"] = gid
        lines.append(run_line)
        data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in lines)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size + len(data.encode("utf-8")) > MAX_BYTES:
            with _LOCK:
                _STATE["skipped_full"] += 1
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        with _LOCK:
            _DEFS_WRITTEN.add(key)
            _STATE["written_runs"] += 1
        return True
    except Exception:
        try:
            with _LOCK:
                _STATE["write_errors"] += 1
        except Exception:
            pass
        return False


def run_base(run_id) -> str:
    """`r<hex>_a<attempt>` -> `r<hex>`, so rows of one fleet run share a key."""
    return _ATTEMPT.sub("", str(run_id or ""))


def load(path):
    """(runs, defs, unreadable) from one org_versions.jsonl. Read-only; a missing file is empty.

    runs   {run_id: (roles_hash, workflows_hash)}, the last line for a run winning
    defs   {(roles_hash, workflows_hash): {"roles": {...}, "workflows": {...}}}
    A line that is not a JSON object, or a kind this version does not know, is skipped; the first
    kind of skip is counted in `unreadable` and the second is not (a newer writer is not damage).
    """
    runs, defs, bad = {}, {}, 0
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
                pair = (str(obj.get("roles_hash") or ""), str(obj.get("workflows_hash") or ""))
                if obj.get("kind") == "run" and obj.get("run_id"):
                    runs[str(obj["run_id"])] = pair
                elif obj.get("kind") == "defs":
                    defs[pair] = {"roles": obj.get("roles") or {},
                                  "workflows": obj.get("workflows") or {}}
    except OSError:
        pass
    return runs, defs, bad
