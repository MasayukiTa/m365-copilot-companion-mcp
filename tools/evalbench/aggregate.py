"""Aggregate score rows into per-split, per-system summaries and a per-question comparison."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Optional

from .score import ERROR_CLASSES, I_ASKED, I_NA, I_SILENT, I_STATED, ScoreRow
from .stats import bootstrap_ci, mean, median, modal_share, sample_variance

SPLIT_ORDER = ("dev", "test")


def summarize(
    rows: Iterable[ScoreRow],
    all_questions: Optional[dict[str, str]] = None,
    n_boot: int = 2000,
    seed: int = 12345,
) -> dict:
    """Return {"summary": [...], "questions": [...], "systems": [...], "splits": [...]}.

    ``all_questions`` maps every gradable question id to its split so that a system which did
    not answer a question is scored 0 on it (and counted as unanswered) rather than ignored.
    Without it, the set of questions seen in the rows is used.
    """
    rows = list(rows)
    graded = [r for r in rows if r.graded]
    qsplit: dict[str, str] = dict(all_questions or {})
    for r in graded:
        qsplit.setdefault(r.id, r.split)
    systems = sorted({r.system for r in rows})
    splits = [s for s in SPLIT_ORDER if s in set(qsplit.values())] or ["dev"]

    by_sq: dict[tuple[str, str], list[ScoreRow]] = defaultdict(list)
    for r in graded:
        by_sq[(r.system, r.id)].append(r)

    summary = []
    for split in splits:
        qids = sorted(q for q, s in qsplit.items() if s == split)
        for system in systems:
            per_q_acc: list[float] = []
            repro: list[float] = []
            unanswered = 0
            answers: list[ScoreRow] = []
            for qid in qids:
                rs = by_sq.get((system, qid), [])
                if not rs:
                    unanswered += 1
                    per_q_acc.append(0.0)
                    continue
                answers.extend(rs)
                per_q_acc.append(mean([1.0 if r.correct else 0.0 for r in rs]))  # type: ignore[arg-type]
                share = modal_share([r.signature for r in rs])
                if share is not None:
                    repro.append(share)
            by_run: dict[int, list[float]] = defaultdict(list)
            for r in answers:
                by_run[r.run].append(1.0 if r.correct else 0.0)
            run_acc = [mean(v) for v in by_run.values() if v]
            ci = bootstrap_ci(per_q_acc, n_boot=n_boot, seed=seed)
            interp = [r.interpretation for r in answers if r.interpretation != I_NA]
            errs = {c: sum(1 for r in answers if r.error_class == c) for c in ERROR_CLASSES if c != "none"}
            summary.append({
                "split": split,
                "system": system,
                "questions": len(qids),
                "answered": len(qids) - unanswered,
                "unanswered": unanswered,
                "answers": len(answers),
                "executed_rate": mean([1.0 if r.executed else 0.0 for r in answers]),
                "accuracy": mean(per_q_acc),
                "ci_lo": ci[0] if ci else None,
                "ci_hi": ci[1] if ci else None,
                "run_variance": sample_variance([x for x in run_acc if x is not None]),
                "runs": len(by_run),
                "reproducibility": mean(repro),
                "confident_wrong": sum(1 for r in answers if r.confident_wrong),
                "unhedged_wrong": sum(1 for r in answers if r.unhedged_wrong),
                "wrong_total": sum(1 for r in answers if r.correct is False),
                "interp_stated": sum(1 for i in interp if i == I_STATED),
                "interp_asked": sum(1 for i in interp if i == I_ASKED),
                "interp_silent": sum(1 for i in interp if i == I_SILENT),
                "median_seconds": median([r.seconds for r in answers if r.seconds is not None]),
                "errors": errs,
            })

    questions = []
    for qid in sorted(qsplit):
        entry: dict[str, Any] = {"id": qid, "split": qsplit[qid], "systems": {}}
        for system in systems:
            rs = by_sq.get((system, qid), [])
            if not rs:
                entry["systems"][system] = {"accuracy": None, "reproducibility": None, "answers": 0}
                continue
            entry["systems"][system] = {
                "accuracy": mean([1.0 if r.correct else 0.0 for r in rs]),
                "reproducibility": modal_share([r.signature for r in rs]),
                "answers": len(rs),
                "confident_wrong": sum(1 for r in rs if r.confident_wrong),
                "error_class": _top_error(rs),
                "median_seconds": median([r.seconds for r in rs if r.seconds is not None]),
            }
        accs = {s: v["accuracy"] for s, v in entry["systems"].items()}
        vals = {round(a, 6) for a in accs.values() if a is not None}
        entry["differs"] = len(vals) > 1 or (len(vals) == 1 and any(a is None for a in accs.values()))
        questions.append(entry)
    return {"summary": summary, "questions": questions, "systems": systems, "splits": splits}


def _top_error(rs: list[ScoreRow]) -> str:
    bad = [r.error_class for r in rs if r.correct is False]
    if not bad:
        return "none"
    return max(set(bad), key=bad.count)
