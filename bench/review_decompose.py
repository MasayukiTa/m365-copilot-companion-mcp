"""Bounded, scope-preserving decomposition for twice-refused review tasks."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from bench.review_build_goals import CLEAR_FRAMING_PREAMBLE, FINDINGS_BEGIN, FINDINGS_END
from relay.review_resilience import TaskEnvelope, goal_dict_from_envelope

SUBTASKS_BEGIN = "<<<SUBTASKS>>>"
SUBTASKS_END = "<<<END_SUBTASKS>>>"
MAX_DECOMPOSITION_DEPTH = 2
MAX_CHILDREN_PER_PARENT = 8
MAX_TOTAL_RECOVERY_GOALS = 128

_SUBTASKS_RE = re.compile(
    re.escape(SUBTASKS_BEGIN) + r"\s*(.*?)\s*" + re.escape(SUBTASKS_END), re.DOTALL)


def build_decomposer_goal(envelope: TaskEnvelope, refusal_summaries: list[str]) -> dict[str, Any]:
    files = list(envelope.metadata.get("scope") or envelope.metadata.get("files") or [])
    prompt = (
        CLEAR_FRAMING_PREAMBLE
        + "同じ認可済みレビュー作業が、独立した2つのM365会話で拒否されました。"
          "拒否を通す言い換えを考えず、元の意味・認可・禁止操作・対象範囲を保ったまま、"
          "複数の小さく検証可能なレビュー作業へ分割してください。\n"
        + "許可される分割軸: ファイル群、単一観点の下位観点、静的/挙動、入力/処理/出力/例外、"
          "設定/呼出元/データフロー/テスト、発見/証拠取得/仮説判定。\n"
        + "禁止: 認可文や禁止操作の削除、範囲拡大、外部アクセス追加、資格情報利用、秘密値出力、"
          "元タスクと無関係な作業への変換。\n\n"
        + "元タスクID: " + envelope.task_id + "\n"
        + "元レビュー観点: " + str(envelope.metadata.get("dimension", "")) + "\n"
        + "対象ファイル:\n" + ("\n".join("- " + str(f) for f in files) or "- (none)") + "\n"
        + "元の出力契約: " + str(envelope.metadata.get("output_contract", "FINDINGS")) + "\n"
        + "拒否要約:\n" + ("\n".join("- " + str(s) for s in refusal_summaries) or "- (none)")
        + "\n\n次のデリミタ内にJSON配列だけを出してください。\n"
        + SUBTASKS_BEGIN + "\n"
          '[{"title":"...","objective":"...","files":["..."],'
          '"expected_evidence":["..."],"output_contract":"FINDINGS",'
          '"reason_for_split":"..."}]\n'
        + SUBTASKS_END + "\n最後に DONE と書いてください。"
    )
    child = TaskEnvelope(
        task_id=envelope.task_id + "-decomposer",
        parent_task_id=envelope.task_id,
        campaign_id=envelope.campaign_id,
        role="decomposer",
        goal_text=prompt,
        cwd=envelope.cwd,
        depth=envelope.depth,
        metadata={
            "scope": files,
            "output_contract": "SUBTASKS",
            "authorization_preamble": envelope.metadata.get(
                "authorization_preamble", CLEAR_FRAMING_PREAMBLE),
            "prohibited_actions": list(envelope.metadata.get("prohibited_actions") or []),
            "resilience_profile": envelope.metadata.get("resilience_profile", "review"),
        },
    )
    return goal_dict_from_envelope(child)


def parse_subtasks(text: str) -> tuple[list[dict], int]:
    if not text:
        return [], 1
    matches = list(_SUBTASKS_RE.finditer(text))
    if not matches:
        return [], 1
    try:
        data = json.loads(matches[-1].group(1))
    except Exception:
        return [], 1
    if not isinstance(data, list):
        return [], 1
    return [dict(x) for x in data if isinstance(x, dict)], 0


def _norm_file(value: Any) -> str:
    normalized = os.path.normpath(str(value or "")).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return "" if normalized == "." else normalized


def validate_subtasks(parent: TaskEnvelope, subtasks: list[dict],
                      max_children: int = MAX_CHILDREN_PER_PARENT) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    valid: list[dict] = []
    parent_files = {_norm_file(f) for f in
                    (parent.metadata.get("scope") or parent.metadata.get("files") or [])}
    seen = set()
    if not subtasks:
        return [], ["subtask count must be at least 1"]
    if len(subtasks) > max_children:
        errors.append("subtask count exceeds max_children=%d" % max_children)

    for index, raw in enumerate(subtasks[:max_children]):
        objective = str(raw.get("objective") or "").strip()
        files_raw = raw.get("files")
        files = [_norm_file(f) for f in files_raw] if isinstance(files_raw, list) else []
        if not objective:
            errors.append("subtask %d has an empty objective" % index)
            continue
        if not files or any(not f for f in files):
            errors.append("subtask %d has no valid files" % index)
            continue
        if parent_files and not set(files).issubset(parent_files):
            errors.append("subtask %d expands parent file scope" % index)
            continue
        key = (objective.lower(), tuple(sorted(files)))
        if key in seen:
            errors.append("subtask %d duplicates an earlier subtask" % index)
            continue
        seen.add(key)
        item = dict(raw)
        item["objective"] = objective
        item["files"] = files
        item["output_contract"] = "FINDINGS"
        valid.append(item)
    return valid, errors


def build_child_envelopes(parent: TaskEnvelope, subtasks: list[dict]) -> list[TaskEnvelope]:
    out = []
    auth = str(parent.metadata.get("authorization_preamble") or CLEAR_FRAMING_PREAMBLE)
    prohibited = list(parent.metadata.get("prohibited_actions") or [])
    for i, subtask in enumerate(subtasks, 1):
        files = list(subtask.get("files") or [])
        evidence = list(subtask.get("expected_evidence") or [])
        text = (
            auth
            + "元のレビュー作業を範囲を変えずに分割した子タスクです。認可と禁止操作は親と同一です。\n"
            + "目的: " + str(subtask.get("objective") or "") + "\n"
            + "対象ファイル（この集合を超えないこと）:\n"
            + "\n".join("- " + str(f) for f in files) + "\n"
            + ("期待する証拠:\n" + "\n".join("- " + str(e) for e in evidence) + "\n"
               if evidence else "")
            + "ソースを変更せず、読み取りと安全な検証だけを行ってください。\n"
            + "最後に次の形式で結果を出してください。\n"
            + FINDINGS_BEGIN + "\n"
              '[{"file":"path","line":null,"severity":"low|medium|high",'
              '"title":"...","detail":"..."}]\n'
            + FINDINGS_END + "\n最後に DONE と書いてください。"
        )
        metadata = dict(parent.metadata)
        metadata.update({
            "scope": files,
            "files": files,
            "output_contract": "FINDINGS",
            "authorization_preamble": auth,
            "prohibited_actions": prohibited,
            "reason_for_split": subtask.get("reason_for_split", ""),
        })
        out.append(TaskEnvelope(
            task_id="%s-d%d-c%d" % (parent.task_id, parent.depth + 1, i),
            parent_task_id=parent.task_id,
            campaign_id=parent.campaign_id,
            role="producer",
            goal_text=text,
            cwd=parent.cwd,
            depth=parent.depth + 1,
            metadata=metadata,
        ))
    return out
