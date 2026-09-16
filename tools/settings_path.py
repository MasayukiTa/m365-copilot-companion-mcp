# -*- coding: utf-8 -*-
"""Where the shared settings file is -- resolved once, for everyone.

THE DEFECT THIS CLOSES. On 2026-09-16 the cockpit and the fleet read different files at the
same absolute path for a month. %APPDATA% is redirected for a process running inside an MSIX
package, so `C:\\Users\\x\\AppData\\Roaming\\copilot-bridge\\settings.txt` resolved to the
operator's file from one context and to a package-private copy from another. The panel showed
1 GB, every coordinator reserved 4 GB, and nothing could see both.

The repository is the one directory every context agrees about: the fleet already shares it
through --state-dir, and a file written there by a process inside the package is read
unchanged by one outside it (verified both directions, 2026-09-16). So the settings move
there, and %APPDATA% stays readable during the migration.

FIVE COPIES OF THIS PATH EXISTED. relay/fleet_runner.py, bridge/session_store.py,
tools/approval_policy.py, ui/FleetCockpit.cs and ui/CopilotChat.cs each built it themselves,
and they did not agree: approval_policy fell back to the home directory when APPDATA was
unset, while the others produced a RELATIVE path -- "copilot-bridge/settings.txt" against
whatever the process's working directory happened to be. That is a fact with five readers, of
the kind docs/architecture/fact_duplication_ledger.md exists to track.

MIGRATION ORDER, and why it is this way. Readers go two-location first; the writer switches
after; the old location is retired last. The one thing deliberately NOT done is copying the
file at startup: the cockpit and the fleet would each be a copier, and which copy won would
depend on which started last. That is the same shape as the defect being fixed.

Stdlib only and import-safe: main.py's /health imports readers that call this before
anything else is available.
"""
from __future__ import annotations

import os

#: The repository root -- this file lives in tools/, so one level up.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Where the settings are MOVING TO. Inside the repository because that is the only directory
#: every context resolves identically; under .config/ rather than .fleet/ because .fleet is
#: run state that is safe to delete and these are settings that are not.
NEW_PATH = os.path.join(REPO, ".config", "settings.txt")

#: Where they have been. Kept readable so a machine that has not migrated still works, and so
#: the first run after the change behaves exactly as the last run before it.
OLD_DIRNAME = "copilot-bridge"
OLD_BASENAME = "settings.txt"


def old_path() -> str:
    """The %APPDATA% location, or a home-directory fallback when APPDATA is unset.

    The fallback is not decoration. Four of the five copies of this path used
    os.path.join(os.environ.get("APPDATA", ""), ...), which yields a RELATIVE path when the
    variable is missing -- so a service or a scheduled task would silently read
    "copilot-bridge/settings.txt" relative to its working directory, find nothing, and use
    defaults while the operator's real settings sat untouched. Only tools/approval_policy.py
    had this right.
    """
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return os.path.join(appdata, OLD_DIRNAME, OLD_BASENAME)
    return os.path.join(os.path.expanduser("~"), "." + OLD_DIRNAME, OLD_BASENAME)


def settings_file() -> str:
    """The file to READ. The new location when it exists, otherwise the old one.

    Existence, not preference: a machine mid-migration must keep working, and one that has
    never migrated must behave exactly as before. When neither file exists this returns the
    NEW path, so a fresh machine starts where everything is going rather than where it came
    from.
    """
    try:
        if os.path.isfile(NEW_PATH):
            return NEW_PATH
    except OSError:
        pass
    old = old_path()
    try:
        if os.path.isfile(old):
            return old
    except OSError:
        pass
    return NEW_PATH


def settings_file_for_write() -> str:
    """The file to WRITE. Always the new location, and its directory is created.

    Writing follows reading by one step on purpose: readers learned both locations first, so
    by the time anything writes here every reader already knows to look. Never raises -- a
    settings write that fails must not take down the thing that was saving a preference.
    """
    try:
        os.makedirs(os.path.dirname(NEW_PATH), exist_ok=True)
    except OSError:
        pass
    return NEW_PATH


def describe() -> str:
    """One line naming the file, its size and its mtime -- the identity, not just the path.

    The 2026-09-16 split was invisible in the path and obvious in the identity: 281 bytes
    modified today from one context, 237 bytes modified four days earlier from another. Any
    report that prints where it read settings from should print this instead.
    """
    path = settings_file()
    try:
        st = os.stat(path)
    except OSError as exc:
        return "%s (unreadable: %s)" % (path, exc.__class__.__name__)
    import time
    return "%s (%d bytes, modified %s)" % (
        path, st.st_size, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)))
