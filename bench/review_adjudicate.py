"""Independent adjudication of producer/refuter disagreements."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from bench.review_build_goals import CLEAR_FRAMING_PREAMBLE, write_goals_jsonl
from bench.review_aggregate import worker_final_text

_VERDICT_RE = re.compile(
    r"ADJUDICATION_VERDICT\s*[:：]\s*(CONFIRM|DISPROVE|INCONCLUSIVE)", re.IGNORECASE)
_REASON_RE = re.compile(r"ADJUDICATION_REASON\s*[:：]\s*(.+)", re.IGNORECASE)


def build_adjudication_goal(finding: dict, kind: str) -> dict[str, Any]:
    payload = {
        "file": finding.get("file"),
        "line": finding.get("line"),
        "severity": finding.get("severity"),
        "title": finding.get("title"),
        "detail": finding.get("detail"),
        "refuter_verdict": finding.get("refuter_verdict"),
        "refuter_reason": finding.get("verify_reason", ""),
        "behavioral_evidence": finding.get("behavioral_evidence", ""),
    }
    text = (
        CLEAR_FRAMING_PREAMBLE
        + "あなたはProducerにもRefuterにも属さない独立裁定者です。多数決やseverityの印象ではなく、"
          "該当ファイルと行を実際に確認し、このfindingが実在するかを裁定してください。\n"
        + "レビュー種別: " + kind + "\n"
        + "入力:\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        + "最後に必ず次の2行を出してください。\n"
          "ADJUDICATION_VERDICT: CONFIRM|DISPROVE|INCONCLUSIVE\n"
          "ADJUDICATION_REASON: <具体的証拠>\nDONE"
    )
    return {"text": text, "cwd": finding.get("repo_root") or os.getcwd(), "role": "adjudicator"}


def parse_adjudication(text: str) -> tuple[str, str]:
    matches = list(_VERDICT_RE.finditer(text or ""))
    if not matches:
        return "INCONCLUSIVE", ""
    verdict = matches[-1].group(1).upper()
    reasons = list(_REASON_RE.finditer(text or ""))
    return verdict, (reasons[-1].group(1).strip() if reasons else "")


def adjudicate_findings(findings, kind, out_dir, max_concurrent, effort, repo_root, stamp,
                        run_fleet):
    """Run one isolated adjudicator conversation per disputed finding, mutating in place."""
    selected = [f for f in findings if str(f.get("refuter_verdict") or "").upper()
                in ("REFUTED", "INCONCLUSIVE", "UNCLEAR")]
    if not selected:
        return []
    goals = []
    for f in selected:
        enriched = dict(f)
        enriched["repo_root"] = repo_root
        goals.append(build_adjudication_goal(enriched, kind))
    goals_path = os.path.join(out_dir, "adjudicate_goals_%s.jsonl" % stamp)
    write_goals_jsonl(goals, goals_path)
    state_dir = os.path.join(out_dir, "adjudicate_state_%s" % stamp)
    run_fleet(goals_path, max_concurrent, effort, state_dir=state_dir,
              resilience_profile=kind, max_turns=6)
    status_path = os.path.join(state_dir, "status.json")
    try:
        with open(status_path, encoding="utf-8") as f:
            workers = json.load(f).get("workers", [])
    except Exception:
        workers = []
    attached = []
    tx_dir = os.path.join(state_dir, "transcripts")
    for i, finding in enumerate(selected):
        worker = workers[i] if i < len(workers) and isinstance(workers[i], dict) else {}
        verdict, reason = parse_adjudication(worker_final_text(worker, tx_dir))
        finding["adjudicator_verdict"] = verdict
        finding["adjudicator_reason"] = reason
        attached.append(finding)
    return attached
