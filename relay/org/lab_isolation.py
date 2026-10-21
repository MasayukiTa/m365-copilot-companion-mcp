# -*- coding: utf-8 -*-
"""Layout check that keeps the Evolution Lab fully separate from production (slice F1).

WHAT DECISION THIS SERVES. Whether a proposed lab directory may be used for lab work at all. The
production worktree executes uncommitted edits live: on 2026-09-24 a delegate's half-written edit
in the production tree broke the production venv and stopped the server for 7 minutes. So lab work
must never share files or state with production. This module only inspects the path strings it is
handed (and resolves them with os.path.realpath, and reads one marker file). It creates no
directory, writes no file, starts no process and opens no network connection. Nothing in
production calls it (default off): only scripts/org_lab_isolation.py, run by hand, and only to
print a verdict.

RULES (each one is a violation code in the verdict).
  inside_production_root / contains_production_root / same_as_production_root
      The lab must live outside the production tree. Reason: a lab file inside that tree is run by
      production the moment it is saved; a lab that contains the tree can overwrite it. Paths are
      compared after os.path.realpath, so a symlink or junction that points into production is
      caught, and case-insensitively on Windows (C:\\Repo and c:\\repo are the same place).
  inside_production_state / contains_production_state / same_as_production_state
      The lab must live outside the production state dir (.fleet). Reason: that dir holds the live
      queues, locks and tokens; a lab sharing it can corrupt or consume production jobs.
  on_h_drive, unc_path
      The lab must not be on the H: drive or on a UNC path (\\\\host\\share). Reason: those are
      shared company storage; build output, caches and logs of a lab must never land there. The
      check looks at the path as given and at its resolved form, so a mapped drive that resolves to
      UNC is caught too.
  lab_state_shared_with_production
      The lab has its own state dir (default <lab_root>/.fleet) and it must not equal, sit inside
      or contain the production state dir. Reason: separate roots with a shared state dir would
      still share queues and locks.
  marker_missing / marker_invalid
      The lab must be a copy made on purpose: it contains LAB_COPY.json with the commit it was
      copied from and the creation time. Reason: a directory without that marker may be a working
      tree somebody edits by hand, and nobody can tell which commit lab results refer to.

The creation time is passed in by the caller (this module has no clock).
"""
from __future__ import annotations

import json
import os
import re

VERSION = 1
MARKER_NAME = "LAB_COPY.json"
DEFAULT_STATE_NAME = ".fleet"
_DRIVE_H = re.compile(r"^[hH]:")
_UNC = re.compile(r"^(\\\\|//)")
_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")


class LabIsolationError(ValueError):
    """An input is not a usable path string or marker."""


def _path(value, what):
    if not isinstance(value, str) or not value.strip():
        raise LabIsolationError("%s must be a non-empty path string" % what)
    return value


def make_marker(source_commit, created_at):
    """The content of LAB_COPY.json: schema version, source commit and creation time.

    Pure: it returns a dict and writes nothing. created_at is an opaque string from the caller.
    """
    if not isinstance(source_commit, str) or not _COMMIT.match(source_commit):
        raise LabIsolationError("source_commit must be a hex commit id (7-64 chars)")
    if not isinstance(created_at, str) or not created_at.strip():
        raise LabIsolationError("created_at must be a non-empty string")
    return {"version": VERSION, "source_commit": source_commit.lower(), "created_at": created_at}


def marker_problems(marker):
    """List of reasons a marker is not acceptable (empty list = valid)."""
    if not isinstance(marker, dict):
        return ["marker is not an object"]
    out = []
    if marker.get("version") != VERSION or isinstance(marker.get("version"), bool):
        out.append("version must be %d" % VERSION)
    c = marker.get("source_commit")
    if not isinstance(c, str) or not _COMMIT.match(c):
        out.append("source_commit must be a hex commit id")
    t = marker.get("created_at")
    if not isinstance(t, str) or not t.strip():
        out.append("created_at must be a non-empty string")
    return out


def read_marker(lab_root):
    """Read <lab_root>/LAB_COPY.json. Returns (marker_or_None, error_or_None). Read-only."""
    p = os.path.join(lab_root, MARKER_NAME)
    if not os.path.isfile(p):
        return None, "missing"
    try:
        with open(p, encoding="utf-8-sig") as fh:
            return json.loads(fh.read()), None
    except (OSError, ValueError) as exc:
        return None, "unreadable: %s" % exc


def _real(path):
    """realpath, but never touch a path already known to be on shared storage (it could block on
    the network) and fall back to the absolute path if it cannot be resolved."""
    ab = os.path.abspath(path)
    if _DRIVE_H.match(path) or _UNC.match(path):
        return ab
    try:
        return os.path.realpath(ab)
    except OSError:
        return ab


def _canon(path, case_insensitive):
    real = _real(path)
    return real.lower() if case_insensitive else real


def _relation(a, b):
    """'same', 'inside' (a is within b), 'contains' (a contains b) or None. a, b canonical."""
    if a == b:
        return "same"
    try:
        common = os.path.commonpath([a, b])
    except ValueError:  # different drives
        return None
    if common == b:
        return "inside"
    if common == a:
        return "contains"
    return None


def _shared_storage(raw, real):
    codes = []
    for p in (raw, real):
        if _DRIVE_H.match(p) and "on_h_drive" not in codes:
            codes.append("on_h_drive")
        if _UNC.match(p) and "unc_path" not in codes:
            codes.append("unc_path")
    return codes


def check_lab_layout(lab_root, production_root, production_state_dir, lab_state_dir=None,
                     marker=None, marker_error=None, case_insensitive=None):
    """Verdict on a lab layout: {"ok", "violations": [{"code", "detail"}], "lab_root", ...}.

    marker / marker_error are what read_marker() returned (marker=None, marker_error=None means
    "not looked at" and counts as missing). lab_state_dir defaults to <lab_root>/.fleet.
    case_insensitive defaults to True on Windows. Resolves paths with os.path.realpath only.
    """
    lab = _path(lab_root, "lab_root")
    prod = _path(production_root, "production_root")
    pstate = _path(production_state_dir, "production_state_dir")
    lstate = _path(lab_state_dir, "lab_state_dir") if lab_state_dir is not None \
        else os.path.join(lab, DEFAULT_STATE_NAME)
    ci = (os.name == "nt") if case_insensitive is None else bool(case_insensitive)

    c_lab, c_prod, c_pstate, c_lstate = (_canon(p, ci) for p in (lab, prod, pstate, lstate))
    v = []

    def add(code, detail):
        v.append({"code": code, "detail": detail})

    rel = _relation(c_lab, c_prod)
    if rel == "same":
        add("same_as_production_root", "lab root is the production root")
    elif rel == "inside":
        add("inside_production_root", "lab root is inside the production tree")
    elif rel == "contains":
        add("contains_production_root", "lab root contains the production tree")

    rel = _relation(c_lab, c_pstate)
    if rel == "same":
        add("same_as_production_state", "lab root is the production state dir")
    elif rel == "inside":
        add("inside_production_state", "lab root is inside the production state dir")
    elif rel == "contains":
        add("contains_production_state", "lab root contains the production state dir")

    for code in _shared_storage(lab, _real(lab)):
        add(code, "lab root is on shared company storage")

    if _relation(c_lstate, c_pstate) is not None:
        add("lab_state_shared_with_production",
            "lab state dir equals, is inside or contains the production state dir")

    if marker is None:
        add("marker_missing", "%s %s" % (MARKER_NAME, marker_error or "not provided"))
    else:
        probs = marker_problems(marker)
        if probs:
            add("marker_invalid", "; ".join(probs))

    return {"ok": not v, "violations": v, "lab_root": c_lab, "production_root": c_prod,
            "production_state_dir": c_pstate, "lab_state_dir": c_lstate}


def to_json(obj):
    """Stable JSON text for a verdict or marker."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2)
