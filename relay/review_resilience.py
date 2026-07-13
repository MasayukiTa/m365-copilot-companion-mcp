"""Pure review-resilience primitives shared by the fleet and review orchestrator.

The module intentionally has no browser or subprocess dependencies.  A TaskEnvelope is the
immutable identity of one review task; a fresh-session replay may change only
``session_attempt`` and conversation-local state, never the envelope hash.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RefusalCause(str, Enum):
    SESSION_STATE = "session_state"
    TASK_CONTENT = "task_content"
    CAPABILITY = "capability"
    OUTPUT_FILTER = "output_filter"
    CONTEXT_CONTAMINATION = "context_contamination"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


class RecoveryAction(str, Enum):
    FRESH_REPLAY = "fresh_replay"
    DECOMPOSE = "decompose"
    ALTERNATE_EXECUTOR = "alternate_executor"
    REDACT_OUTPUT = "redact_output"
    RETRY_TRANSIENT = "retry_transient"
    MARK_UNRESOLVED = "mark_unresolved"


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    parent_task_id: str | None
    campaign_id: str
    role: str
    goal_text: str
    cwd: str
    depth: int = 0
    session_attempt: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def goal_hash(self) -> str:
        """Hash the task identity, deliberately excluding the replay-attempt counter."""
        identity = {
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "campaign_id": self.campaign_id,
            "role": self.role,
            "goal_text": self.goal_text,
            "cwd": self.cwd,
            "depth": self.depth,
            "scope": self.metadata.get("scope", self.metadata.get("files", [])),
            "output_contract": self.metadata.get("output_contract", ""),
            "authorization_preamble": self.metadata.get("authorization_preamble", ""),
            "prohibited_actions": self.metadata.get("prohibited_actions", []),
        }
        raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RefusalDiagnosis:
    cause: RefusalCause
    action: RecoveryAction
    reason: str


POLICY_REFUSAL_MARKERS = (
    "このリクエストには対応できません",
    "このリクエストをお手伝いすることはできません",
    "この内容には協力できません",
    "このご依頼には対応できません",
    "お手伝いすることができません",
    "i can't help with that request",
    "i cannot assist with that request",
    "i’m unable to help with that",
    "i'm unable to help with that",
    "i can’t assist with that",
)

CAPABILITY_FAILURE_MARKERS = (
    "この機能は利用できません",
    "この環境では実行できません",
    "必要な機能がありません",
    "i don't have the capability",
    "i do not have the capability",
    "this capability is not available",
)

OUTPUT_FILTER_MARKERS = (
    "出力できませんでした",
    "応答を生成できませんでした",
    "response was filtered",
    "content filter blocked the response",
)

TRANSIENT_MARKERS = (
    "予期しないエラー",
    "システムエラー",
    "something went wrong",
    "unexpected error",
    "try again later",
    "network error",
)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    low = (text or "").lower()
    return bool(low) and any(marker.lower() in low for marker in markers)


def looks_like_policy_refusal(text: str) -> bool:
    return _contains_any(text, POLICY_REFUSAL_MARKERS)


def looks_like_capability_failure(text: str) -> bool:
    return _contains_any(text, CAPABILITY_FAILURE_MARKERS)


def looks_like_output_filter(text: str) -> bool:
    return _contains_any(text, OUTPUT_FILTER_MARKERS)


def looks_like_transient_error(text: str) -> bool:
    return _contains_any(text, TRANSIENT_MARKERS)


def diagnose_after_fresh_replay(
    original_was_refusal: bool,
    fresh_was_refusal: bool,
    fresh_succeeded: bool,
    fresh_was_transient_error: bool,
) -> RefusalDiagnosis:
    if original_was_refusal and fresh_succeeded and not fresh_was_refusal:
        return RefusalDiagnosis(
            RefusalCause.SESSION_STATE, RecoveryAction.FRESH_REPLAY,
            "The identical task succeeded in a fresh conversation.",
        )
    if fresh_was_transient_error:
        return RefusalDiagnosis(
            RefusalCause.TRANSIENT, RecoveryAction.RETRY_TRANSIENT,
            "The fresh conversation failed with a transient/infrastructure error.",
        )
    if original_was_refusal and fresh_was_refusal:
        return RefusalDiagnosis(
            RefusalCause.TASK_CONTENT, RecoveryAction.DECOMPOSE,
            "The identical task was refused in two independent conversations.",
        )
    return RefusalDiagnosis(
        RefusalCause.UNKNOWN, RecoveryAction.MARK_UNRESOLVED,
        "The available evidence does not identify a safe automatic recovery.",
    )


def freeze_goal_dict(goal: dict[str, Any]) -> dict[str, Any]:
    """Return a defensive deep copy suitable for replay/ledger persistence."""
    return copy.deepcopy(dict(goal or {}))


def same_task_envelope(a: TaskEnvelope, b: TaskEnvelope) -> bool:
    return isinstance(a, TaskEnvelope) and isinstance(b, TaskEnvelope) \
        and a.goal_hash == b.goal_hash


def task_envelope_from_goal(goal: dict[str, Any] | str, default_role: str = "producer") -> TaskEnvelope:
    if isinstance(goal, dict):
        text = str(goal.get("text") or goal.get("goal") or "")
        metadata = dict(goal.get("metadata") or {})
        for key in ("scope", "files", "output_contract", "authorization_preamble",
                    "prohibited_actions", "dimension", "resilience_profile"):
            if key in goal and key not in metadata:
                metadata[key] = copy.deepcopy(goal[key])
        return TaskEnvelope(
            task_id=str(goal.get("task_id") or ""),
            parent_task_id=goal.get("parent_task_id"),
            campaign_id=str(goal.get("campaign_id") or ""),
            role=str(goal.get("role") or default_role),
            goal_text=text,
            cwd=str(goal.get("cwd") or ""),
            depth=int(goal.get("depth") or 0),
            session_attempt=int(goal.get("session_attempt") or 0),
            metadata=metadata,
        )
    return TaskEnvelope("", None, "", default_role, str(goal), "")


def goal_dict_from_envelope(envelope: TaskEnvelope) -> dict[str, Any]:
    goal = {
        "text": envelope.goal_text,
        "cwd": envelope.cwd,
        "task_id": envelope.task_id,
        "parent_task_id": envelope.parent_task_id,
        "campaign_id": envelope.campaign_id,
        "role": envelope.role,
        "depth": envelope.depth,
        "session_attempt": envelope.session_attempt,
        "goal_hash": envelope.goal_hash,
        "metadata": copy.deepcopy(envelope.metadata),
    }
    profile = envelope.metadata.get("resilience_profile")
    if profile:
        goal["resilience_profile"] = profile
    return goal
