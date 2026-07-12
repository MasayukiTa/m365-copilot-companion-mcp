"""autonomy_gate.py -- small deterministic GO / ASK / STOP gate for unattended runs.

This is the "auto mode" guardrail: before an overnight or long-running autonomous job
starts, decide whether it is safe to let the executor proceed without a human click.

The gate is deliberately deterministic and conservative. A future model-judge can sit
behind the same result shape, but hard policy should stay local and predictable.
"""
from __future__ import annotations

import os
import re
import subprocess


GO = "GO"
ASK = "ASK"
STOP = "STOP"


_STOP_PATTERNS = (
    r"\brm\s+-rf\b",
    r"\bdel\s+/[fsq]\b",
    r"\bformat\b",
    r"\bdrop\s+database\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-fdx\b",
    r"\bdelete\s+all\b",
    r"全削除",
    r"初期化",
    r"本番.*(変更|削除|投入|deploy|デプロイ)",
    r"秘密鍵|secret|credential|password|パスワード|api key|token",
)

_ASK_PATTERNS = (
    r"push",
    r"deploy",
    r"release",
    r"merge",
    r"commit",
    r"\bssh\b",
    r"\bscp\b",
    r"email",
    r"slack",
    r"teams",
    r"sharepoint",
    r"outlook",
    r"gmail",
    r"送信",
    r"公開",
    r"デプロイ",
    r"リリース",
    r"社外",
    r"本番",
    r"全部(直|変|書)",
    r"大規模",
)


def _matches(text, patterns):
    return [p for p in patterns if re.search(p, text, re.IGNORECASE)]


def _git_status(folder):
    try:
        p = subprocess.run(
            ["git", "-C", folder, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if p.returncode == 0:
            return p.stdout.splitlines()
    except Exception:
        pass
    return []


def judge_autonomy(instruction, folder, checks=None, max_hours=None):
    """Return a dict with decision=GO|ASK|STOP, reasons, constraints, warnings."""
    text = instruction or ""
    folder = os.path.abspath(folder)
    checks = checks or []
    reasons = []
    warnings = []
    constraints = [
        "Work only inside the target repository unless the task explicitly names another path.",
        "Do not push, deploy, send messages, edit credentials, or change external services.",
        "Do not run destructive git or filesystem commands.",
        "Preserve user changes; do not revert unrelated dirty files.",
        "Prefer small verified cycles and stop when evidence is insufficient.",
    ]

    stop_hits = _matches(text, _STOP_PATTERNS)
    if stop_hits:
        return {
            "decision": STOP,
            "reasons": ["instruction matched hard-stop risk patterns: %s" % ", ".join(stop_hits)],
            "warnings": warnings,
            "constraints": constraints,
        }

    ask_hits = _matches(text, _ASK_PATTERNS)
    if ask_hits:
        reasons.append("instruction mentions side-effect or broad-scope terms: %s" %
                       ", ".join(ask_hits))

    dirty = _git_status(folder)
    if dirty:
        warnings.append("target git worktree has %d dirty/untracked path(s)" % len(dirty))
        constraints.append("Before editing an already-dirty file, inspect it and work with the existing changes.")

    if not checks:
        reasons.append("no automatic verification gate detected")

    if max_hours is not None and max_hours > 12:
        reasons.append("requested unattended budget is over 12 hours")

    if reasons:
        return {
            "decision": ASK,
            "reasons": reasons,
            "warnings": warnings,
            "constraints": constraints,
        }

    return {
        "decision": GO,
        "reasons": ["local repo task with verification gate and no external side-effect markers"],
        "warnings": warnings,
        "constraints": constraints,
    }


def constraints_text(gate):
    constraints = (gate or {}).get("constraints") or []
    if not constraints:
        return ""
    return "\n\n【auto-mode constraints】\n" + "\n".join("- " + c for c in constraints)
