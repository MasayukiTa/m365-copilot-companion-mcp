"""Read-only model-facing access to already trusted Skills.

Creation, import, and approval are intentionally absent: only the local human console
may perform those administrative operations. Loading a Skill does not bypass the
normal unlock or autonomy-contract gates of any tool it later recommends.
"""
from __future__ import annotations

import json
import os
from typing import Any
from pathlib import Path

from relay.skills import SkillError, SkillStore


def _store() -> SkillStore:
    project = os.environ.get("MCP_SKILLS_PROJECT_ROOT") or str(Path(__file__).resolve().parent.parent)
    return SkillStore(project)


def skill_list() -> str:
    """List Skill metadata and trust state without loading instruction bodies.

    Also reports bundles that FAILED to load, under "invalid". A malformed SKILL.md
    used to be skipped in silence, so a Skill that had just been written simply never
    appeared and there was nothing to debug -- the commonest cause being an unquoted
    ':' in the description, which makes the YAML frontmatter invalid. The reason is
    surfaced here (folder name + parser message); the bundle's contents stay
    unexposed, so an untrusted body still cannot reach the model through this path.
    """
    try:
        store = _store()
        payload: Any = store.list_metadata(model_safe=True)
        try:
            invalid = store.invalid_bundles()
        except Exception:
            invalid = {}
        if invalid:
            payload = {
                "skills": payload,
                "invalid": [
                    {"folder": folder, "error": reason, "hint": _invalid_hint(reason)}
                    for folder, reason in sorted(invalid.items())
                ],
            }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"[skill_list error: {type(exc).__name__}: {exc}]"


def _invalid_hint(reason: str) -> str:
    """Turn a parser message into the concrete edit that fixes it."""
    text = (reason or "").lower()
    if "yaml" in text or "mapping values" in text:
        return ("SKILL.md の frontmatter が YAML として壊れています。"
                "description に ':' や '#' が含まれる場合は "
                'description: "..." のように引用符で囲んでください。')
    if "frontmatter" in text:
        return "SKILL.md の先頭を --- で開き、--- で閉じてください。"
    if "no SKILL.md" in reason:
        return "フォルダ直下に SKILL.md を置いてください。"
    return "SKILL.md を修正してから再度 skill_list を実行してください。"


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
