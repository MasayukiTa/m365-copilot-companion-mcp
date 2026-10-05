# -*- coding: utf-8 -*-
"""Is a long-running process still running the code that is on disk?

A PowerShell script is parsed once, when its process starts. Updating scripts/supervisor.ps1 on
disk therefore changes NOTHING until that process is replaced: on 2026-10-04 the supervisor kept
running its pre-#122 text after the auto-resume change had been merged, so when the coordinator
died it logged "DRY RUN -- not relaunching" and nothing resumed. A Python process behaves the same
way for the modules it already imported (the bridge). The server has had an answer for this for a
long time (`server_code` in /health, scripts/stale_server_check.py); the supervisor and the
bridge had none.

THE RULE IS THE SAME EVERYWHERE: record a fingerprint (sha256 of each file the process loaded) at
start; later, compare against the disk. mtime first (one stat per file per check), hash only when
the mtime or size moved, so asking every cycle costs nothing in the ordinary case.

This module is the shared Python half: the bridge uses CodeWatch for its /status `code_stale`
field, and supervisor.ps1 reads the `supervisor_self_restart` setting through
self_restart_setting() (the PowerShell side computes its own fingerprint with Get-FileHash and
writes .fleet/supervisor_state.json; the cockpit reads that file).
"""
from __future__ import annotations

import hashlib
import os

SELF_RESTART_KEY = "supervisor_self_restart"
SELF_RESTART_DEFAULT = "on"
SELF_RESTART_MODES = ("off", "on")

#: Files whose change makes a running supervisor stale. supervisor.ps1 dot-sources the second.
SUPERVISOR_CODE_FILES = ("scripts/supervisor.ps1", "scripts/tunnel_name_util.ps1")

#: The files a running bridge has loaded (module level or lazily imported on its request path).
BRIDGE_CODE_FILES = (
    "bridge/copilot_bridge.py",
    "bridge/session_store.py",
    "bridge/bridge_auth.py",
    "bridge/review_command.py",
    "relay/copilot_autopilot_relay.py",
    "relay/relay_fleet.py",
    "relay/conversation_lineage.py",
)


def self_restart_setting():
    """The `supervisor_self_restart` setting, "on" or "off". Read from settings.txt on every
    call; absent, empty or unrecognised means the default. Never raises."""
    try:
        from tools.settings_path import settings_file
        path = settings_file()
        raw = None
        if os.path.isfile(path):
            with open(path, encoding="utf-8-sig") as fh:
                for ln in fh.read().splitlines():
                    if ln.startswith(SELF_RESTART_KEY + "="):
                        raw = ln.split("=", 1)[1]
        v = (raw or "").strip().lower()
        return v if v in SELF_RESTART_MODES else SELF_RESTART_DEFAULT
    except Exception:
        return SELF_RESTART_DEFAULT


def file_sha256(path):
    """Hex sha256 of a file, "" when it cannot be read (a missing file is itself a change)."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def fingerprint(root, rels):
    """{relative/path: sha256} for each file under root."""
    return {rel: file_sha256(os.path.join(root, rel.replace("/", os.sep))) for rel in rels}


def changed_files(recorded, current):
    """Sorted relative paths whose hash differs between two fingerprints."""
    keys = set(recorded) | set(current)
    return sorted(k for k in keys if recorded.get(k) != current.get(k))


class CodeWatch(object):
    """Compares the files a process loaded with the disk, cheaply.

    Created at process start (records the fingerprint and each file's mtime/size). changed()
    stats every file and re-hashes only the ones whose stat moved, so calling it on every
    request or cycle is one stat per file. Never raises: an unreadable answer is "no change
    known", because a false "stale" would send somebody to restart a healthy process."""

    def __init__(self, root, rels):
        self.root = root
        self.rels = tuple(rels)
        self.recorded = fingerprint(root, self.rels)
        self._stat = {rel: self._stat_of(rel) for rel in self.rels}
        self._current = dict(self.recorded)

    def _stat_of(self, rel):
        try:
            st = os.stat(os.path.join(self.root, rel.replace("/", os.sep)))
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def changed(self):
        try:
            for rel in self.rels:
                now = self._stat_of(rel)
                if now != self._stat[rel]:
                    self._stat[rel] = now
                    self._current[rel] = file_sha256(os.path.join(self.root, rel.replace("/", os.sep)))
            return changed_files(self.recorded, self._current)
        except Exception:
            return []

    def stale(self):
        return bool(self.changed())
