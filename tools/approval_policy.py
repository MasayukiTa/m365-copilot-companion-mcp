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
    # This module had the ONLY correct fallback of the five copies -- the others produced a
    # relative path when APPDATA was unset. That behaviour now lives in settings_path.old_path
    # and is shared, rather than being right in one place by luck.
    from tools.settings_path import settings_file
    return Path(settings_file())


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


def is_bypass(default: str | None = None) -> bool:
    """True when the live mode is `bypass`. A one-word predicate so every ask-a-person
    call site reads the same way rather than each spelling out
    ``current_approval_mode() == "bypass"`` on its own."""
    return current_approval_mode(default) == "bypass"


#: WHERE BYPASS'S AUDIT TRAIL LIVES. `bypass` means "never ask a person" (the owner's own
#: words: "バイパスとは未来永劫それをユーザに尋ねることはないという意味"), and the owner is
#: giving up their own STOP-pattern reviews, Skill-trust prompts and STUCK escalations to get
#: that -- so what would have been asked, and what got decided instead, must stay readable
#: somewhere a person can audit it after the fact. This is that somewhere: one JSON line per
#: call site that backed off from asking. Same directory family as `.fleet/active_contract.json`
#: (tools/contract_gate.py) -- computed independently here, not imported from there, because
#: contract_gate already imports THIS module and a back-import would be circular.
#: Redirectable via MCP_BYPASS_LOG_FILE, read at IMPORT (same shape as tools.gate_ops.GATE_DIR
#: / MCP_GATE_DIR): conftest.py sets the env var at module scope before this module is first
#: imported, so every test that drives bypass mode writes its audit line to a per-run sandbox
#: file instead of the live repo's .fleet/bypass_decisions.jsonl.
_REPO_ROOT = Path(__file__).resolve().parent.parent
BYPASS_LOG_FILE = Path(os.environ.get("MCP_BYPASS_LOG_FILE")
                       or (_REPO_ROOT / ".fleet" / "bypass_decisions.jsonl"))


def record_bypass_decision(path: str, would_have_asked: str, decision: str) -> None:
    """Append one audit line: `path` (the call site, e.g. "gate_ops.gate_ask_local") would
    have put `would_have_asked` to a person; under bypass it instead decided `decision`.

    Best-effort and silent on failure BY DESIGN. The entire point of bypass is that nothing
    on this path may block, prompt, or raise in place of asking a person -- a logging
    failure (disk full, directory missing) must never become a second, accidental gate.
    """
    import json as _json
    import time as _time

    try:
        BYPASS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = _json.dumps(
            {
                "ts": _time.time(),
                "path": path,
                "would_have_asked": (would_have_asked or "")[:2000],
                "decision": decision,
            },
            ensure_ascii=False,
        )
        with open(BYPASS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
