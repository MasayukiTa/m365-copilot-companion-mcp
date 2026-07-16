"""Read-only model-facing access to already trusted Skills.

Creation, import, and approval are intentionally absent: only the local human console
may perform those administrative operations. Loading a Skill does not bypass the
normal unlock or autonomy-contract gates of any tool it later recommends.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from relay.skills import SkillError, SkillStore


def _store() -> SkillStore:
    project = os.environ.get("MCP_SKILLS_PROJECT_ROOT") or str(Path(__file__).resolve().parent.parent)
    return SkillStore(project)


def skill_list() -> str:
    """List Skill metadata and trust state without loading instruction bodies."""
    try:
        return json.dumps(_store().list_metadata(model_safe=True), ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"[skill_list error: {type(exc).__name__}: {exc}]"


def skill_match(text: str) -> str:
    """Find a confidently matching trusted Skill using metadata only; does not load it."""
    try:
        result = _store().match(text)
        return json.dumps(result, ensure_ascii=False, indent=2) if result else "(no confident Skill match)"
    except Exception as exc:
        return f"[skill_match error: {type(exc).__name__}: {exc}]"


def skill_load(name: str, arguments: str = "") -> str:
    """Load and render one trusted Skill. Untrusted or changed bundles are refused."""
    try:
        return _store().render(name, arguments)
    except SkillError as exc:
        return f"[skill_load refused: {exc}]"
    except Exception as exc:
        return f"[skill_load error: {type(exc).__name__}: {exc}]"


def skill_read_resource(name: str, path: str) -> str:
    """Read a bounded UTF-8 resource from a trusted Skill's resources directories."""
    try:
        return _store().read_resource(name, path)
    except SkillError as exc:
        return f"[skill_read_resource refused: {exc}]"
    except Exception as exc:
        return f"[skill_read_resource error: {type(exc).__name__}: {exc}]"
