"""Shared live approval-policy reader for local jobs and autonomy contracts.

FleetCockpit writes ``job_approval_mode=`` to its existing per-user settings
file. Reading it at each decision makes a UI change effective immediately,
without restarting the relay. ``TASK_JOB_APPROVAL_MODE`` remains the deployable
environment default and compatibility path for headless installations.
"""
from __future__ import annotations

import os
from pathlib import Path


#: "default" is the MANUAL mode and is kept for the operator who wants it, not recommended:
#: it asks about every first-seen job class and keeps asking, which is how an approval queue
#: becomes something nobody reads.
VALID_APPROVAL_MODES = ("default", "auto", "bypass")


def settings_path() -> Path:
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "copilot-bridge" / "settings.txt"
    return Path.home() / ".copilot-bridge" / "settings.txt"


#: WHAT AN INSTALLATION GETS WITH NO SETTING AND NO ENV VAR.
#:
#: This was "default" -- ask a human about every first-seen job class, forever. The owner's
#: reason for changing it is the one that decides it: a design that asks every time is a design
#: nobody reads, and an approval that is always there is not an approval.
#:
#: "auto" is not the permissive choice. Under "default" a STOP-pattern operation is put to a
#: person, who can approve it; under "auto" it is refused outright and cannot be approved. The
#: two differ only on operations the deterministic classifier finds clean.
FALLBACK_APPROVAL_MODE = "auto"


def current_approval_mode(default: str | None = None) -> str:
    fallback = (default
                or os.environ.get("TASK_JOB_APPROVAL_MODE", FALLBACK_APPROVAL_MODE)
                ).strip().lower()
    if fallback not in VALID_APPROVAL_MODES:
        fallback = FALLBACK_APPROVAL_MODE

    # Tests explicitly set their module-level mode and must never inherit the
    # developer workstation's persistent UI preference.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return fallback

    try:
        path = settings_path()
        if path.is_file():
            for raw in path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line.startswith("job_approval_mode="):
                    continue
                mode = line.split("=", 1)[1].strip().lower()
                return mode if mode in VALID_APPROVAL_MODES else fallback
    except OSError:
        pass
    return fallback
