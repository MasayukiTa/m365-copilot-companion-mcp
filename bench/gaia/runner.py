"""
bench/gaia/runner.py
--------------------
GAIA benchmark evaluation runner for the M365 Copilot companion.

The answering agent is ALWAYS the M365 Copilot companion via the OpenAI-compat
endpoint at http://127.0.0.1:8011/v1/chat/completions.  The companion must be
running (python -m relay.openai_endpoint_server) before this script is called.

Grading uses the OFFICIAL GAIA scorer from bench/gaia/scorer.py — the same
normalisation pipeline as the public leaderboard.

NOTE on orchestration path: this script drives each question via :8011 (one
HTTP POST per question, sequential).  The fleet (relay/fleet_runner.py) is the
companion's own batch orchestrator for coding/agentic goals; it is designed for
multi-turn tasks with acceptance checks and sandboxed workspaces, not pure QA.
Adapting the fleet to capture FINAL ANSWER from a transcript turned out to add
significant complexity with no benefit over the already-serialised :8011 path
(which itself uses the same underlying Copilot relay).  The :8011 path is
therefore used here — the Copilot agent is the brain in both cases.

Usage:
    python bench/gaia/runner.py [--smoke] [--output PATH] [--timeout 180]

    --smoke     Run only the first 8 text-only Level-1 questions (smoke test).
    --output    Path to write results JSON.  Default: .fleet/bench/gaia_results.json
    --timeout   Seconds per question (default 180 — generous for web-reasoning tasks).
    --level     Comma-separated levels to include, e.g. "1" or "1,2".  Default: all.
    --limit     Max questions to run (for quick tests).

Environment (read from .env at startup):
    MCP_API_KEY  — bearer token for :8011 endpoint (required)
    HF_TOKEN     — HuggingFace token for gaia-benchmark/GAIA dataset (required)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running from repo root or from bench/gaia/
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# .env loader (no BOM issues, same pattern as m365eval/runner.py)
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8-sig")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(REPO_ROOT / ".env")

# ---------------------------------------------------------------------------
# Official scorer import
# ---------------------------------------------------------------------------
from bench.gaia.scorer import question_scorer, normalise_answer  # noqa: E402

# ---------------------------------------------------------------------------
# GAIA system prompt (official, instructs FINAL ANSWER format)
# ---------------------------------------------------------------------------

GAIA_SYSTEM_PROMPT = (
    "You are a general AI assistant. I will ask you a question. "
    "Report your thoughts, and finish your answer with the following template: "
    "FINAL ANSWER: [YOUR FINAL ANSWER]. "
    "YOUR FINAL ANSWER should be a number OR as few words as possible OR a "
    "comma separated list of numbers and/or strings. "
    "If you are asked for a number, don't use comma to write your number "
    "neither use units such as $ or percent sign unless specified otherwise. "
    "If you are asked for a string, don't use articles, neither "
    "abbreviations (e.g. for cities), and write the digits in plain text "
    "unless specified otherwise. "
    "If you are asked for a comma separated list, apply the above rules "
    "depending of whether the element to be put in the list is a number or "
    "a string."
)

# Tool-augmented variant (MCP_GAIA_TOOLAUG=1): the agent runs on the
# tool-bearing companion agent (map mode -> minimal core + call_tool gateway),
# so it can compute and look things up instead of answering from memory. This
# addendum is DOMAIN-GENERAL (use tools to verify/compute; answer in English to
# match GAIA's gold language) -- it is NOT tuned to any specific question.
_GAIA_TOOLAUG_ADDENDUM = (
    " You have tools available through the call_tool gateway: run_python for "
    "exact calculation, and call_tool(name='web_search', arguments={'query': '...'}) "
    "to look up facts on the web. Use them to compute and verify before answering -- "
    "do NOT guess when a tool can get you the exact answer. Always answer in English."
)
if os.environ.get("MCP_GAIA_TOOLAUG") == "1":
    GAIA_SYSTEM_PROMPT = GAIA_SYSTEM_PROMPT + _GAIA_TOOLAUG_ADDENDUM

# ---------------------------------------------------------------------------
# Companion HTTP helpers (same pattern as m365eval/runner.py)
# ---------------------------------------------------------------------------

COMPANION_BASE = "http://127.0.0.1:8011"


def preflight() -> str | None:
    """Return None on success, error message on failure."""
    url = f"{COMPANION_BASE}/v1/models"
    api_key = os.environ.get("MCP_API_KEY", "")
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return None if resp.status == 200 else f"Status {resp.status}"
    except Exception as exc:
        return str(exc)


def ask_companion(question: str, api_key: str, timeout: int = 180) -> str:
    """Send question to Copilot companion and return raw reply text."""
    url = f"{COMPANION_BASE}/v1/chat/completions"
    payload = json.dumps({
        "model": "m365-copilot-opus",
        "messages": [
            {"role": "system", "content": GAIA_SYSTEM_PROMPT},
            {"role": "user",   "content": question},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach :8011: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# FINAL ANSWER extraction (official GAIA format)
# ---------------------------------------------------------------------------

_FINAL_ANSWER_RE = re.compile(
    r"FINAL\s+ANSWER\s*:\s*(.+?)(?:\n|$)", re.IGNORECASE | re.DOTALL
)


def extract_final_answer(reply: str) -> str | None:
    """Extract text after 'FINAL ANSWER:' from the model reply.

    Returns None if the pattern is not found.
    Strips trailing whitespace from the extracted text.
    """
    m = _FINAL_ANSWER_RE.search(reply)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# GAIA dataset loader
# ---------------------------------------------------------------------------

def load_gaia_dataset(split: str = "validation") -> list[dict]:
    """Load the GAIA dataset via HuggingFace datasets library.

    Requires: pip install datasets
    Uses HF_TOKEN from environment (never printed or logged).

    Returns a list of dicts with keys:
        task_id, Question, Final answer, Level, file_name (and others).
    """
    # Prefer a LOCAL cache (downloaded out-of-band on a network that allows HF data files;
    # the corporate proxy here blocks huggingface.co/datasets/*/resolve/). No network needed.
    import os.path as _op
    local = _op.join(_op.dirname(_op.abspath(__file__)), "gaia_validation.json")
    if _op.exists(local):
        with open(local, encoding="utf-8") as _f:
            items = json.load(_f)
        # GAIA's Level comes back as a string ("1"/"2"/"3"); the runner compares to int levels.
        for _it in items:
            try:
                _it["Level"] = int(_it.get("Level"))
            except (TypeError, ValueError):
                pass
        print(f"Loaded {len(items)} GAIA {split} items from local cache (no network).")
        return items

    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        raise RuntimeError("HF_TOKEN not set in environment / .env")

    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        raise RuntimeError(
            "The 'datasets' library is not installed. "
            "Run: pip install datasets huggingface_hub"
        )

    print(f"Loading GAIA {split} split (this may take a few minutes on first run)…")
    ds = load_dataset(
        "gaia-benchmark/GAIA",
        "2023_all",
        split=split,
        token=hf_token,
    )
    items = [dict(row) for row in ds]
    print(f"Loaded {len(items)} GAIA {split} items.")
    return items


# ---------------------------------------------------------------------------
# Main evaluation logic
# ---------------------------------------------------------------------------

def filter_text_only(items: list[dict]) -> tuple[list[dict], int]:
    """Split items into text-only and attachment-requiring sets.

    GAIA items with a non-empty 'file_name' field require a file attachment that
    the text-only :8011 endpoint cannot receive.  These are excluded from the
    runnable set.

    Returns (text_only_items, attachment_count).
    """
    text_only = [it for it in items if not (it.get("file_name") or "").strip()]
    attachment_count = len(items) - len(text_only)
    return text_only, attachment_count


def run_evaluation(
    items: list[dict],
    api_key: str,
    timeout: int = 180,
    verbose: bool = True,
    result_path: Path | None = None,
    smoke: bool = False,
    levels: list[int] | None = None,
    limit: int | None = None,
    only_ids: set | None = None,
) -> dict:
    """Run GAIA evaluation on a list of text-only items.

    Returns a results dict with per-question details and aggregate stats.
    Writes incremental JSON to result_path after each question.
    """
    # Filter to a specific set of task_ids (retry mode)
    if only_ids:
        items = [it for it in items if it.get("task_id") in only_ids]

    # Filter by level if requested
    if levels:
        items = [it for it in items if it.get("Level") in levels]

    # Smoke: take first 8 Level-1 questions
    if smoke:
        l1 = [it for it in items if it.get("Level") == 1]
        items = l1[:8]
        if not items:
            items = items[:8]

    # Hard limit
    if limit and not smoke:
        items = items[:limit]

    total = len(items)
    if total == 0:
        return {"error": "No items to evaluate after filtering."}

    results_per_question = []
    correct_count = 0
    error_count = 0

    level_stats: dict[int, dict] = {}

    run_start = datetime.now(timezone.utc).isoformat()
    print(f"\n{'='*60}")
    print(f"GAIA Evaluation — {total} questions")
    print(f"Start: {run_start}")
    print(f"{'='*60}\n")

    for idx, item in enumerate(items, 1):
        task_id    = item.get("task_id", f"q{idx}")
        question   = item.get("Question", "")
        gold       = str(item.get("Final answer", "")).strip()
        level      = item.get("Level", "?")

        if verbose:
            # Truncate long questions for display
            q_display = question[:120].replace("\n", " ")
            print(f"[{idx}/{total}] L{level} {task_id[:16]}…")
            print(f"  Q: {q_display}{'…' if len(question)>120 else ''}")

        row: dict = {
            "idx": idx,
            "task_id": task_id,
            "level": level,
            "question": question,
            "gold": gold,
            "prediction": None,
            "raw_reply": None,
            "correct": False,
            "error": None,
            "elapsed_s": None,
        }

        t0 = time.time()
        try:
            reply = ask_companion(question, api_key, timeout=timeout)
            elapsed = time.time() - t0
            row["raw_reply"] = reply
            row["elapsed_s"] = round(elapsed, 1)

            pred = extract_final_answer(reply)
            if pred is None:
                # Fall back: use the entire reply as the prediction
                pred = reply.strip()
                row["extraction_note"] = "FINAL ANSWER not found; used full reply"
            row["prediction"] = pred

            correct = question_scorer(pred, gold)
            row["correct"] = correct
            if correct:
                correct_count += 1

            norm_pred = normalise_answer(pred)
            norm_gold = normalise_answer(gold)

            verdict = "PASS" if correct else "FAIL"
            if verbose:
                print(f"  Gold: {gold!r}  →  norm: {norm_gold!r}")
                print(f"  Pred: {pred!r}  →  norm: {norm_pred!r}")
                print(f"  [{verdict}]  ({elapsed:.1f}s)\n")

        except Exception as exc:
            elapsed = time.time() - t0
            row["error"] = str(exc)
            row["elapsed_s"] = round(elapsed, 1)
            error_count += 1
            if verbose:
                print(f"  [ERROR] {exc}\n")

        results_per_question.append(row)

        # Per-level stats update
        lv = level
        if lv not in level_stats:
            level_stats[lv] = {"total": 0, "correct": 0, "errors": 0}
        level_stats[lv]["total"] += 1
        if row["correct"]:
            level_stats[lv]["correct"] += 1
        if row["error"]:
            level_stats[lv]["errors"] += 1

        # Write incremental results
        if result_path:
            _write_results(result_path, results_per_question, correct_count, total, error_count, level_stats, run_start, smoke)

    # Final aggregate
    run_end = datetime.now(timezone.utc).isoformat()
    score_pct = (correct_count / total * 100) if total > 0 else 0.0

    summary = {
        "run_start": run_start,
        "run_end": run_end,
        "total_questions": total,
        "correct": correct_count,
        "errors": error_count,
        "score_pct": round(score_pct, 2),
        "per_level": level_stats,
        "smoke": smoke,
        "questions": results_per_question,
    }

    if result_path:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nResults written to: {result_path}")

    print(f"\n{'='*60}")
    print(f"GAIA Score: {correct_count}/{total} = {score_pct:.1f}%")
    for lv in sorted(level_stats):
        ls = level_stats[lv]
        lv_pct = (ls["correct"] / ls["total"] * 100) if ls["total"] else 0
        print(f"  Level {lv}: {ls['correct']}/{ls['total']} = {lv_pct:.1f}%  (errors: {ls['errors']})")
    print(f"{'='*60}\n")

    return summary


def _write_results(
    path: Path, questions, correct, total, errors, level_stats, run_start, smoke
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "run_start": run_start,
        "status": "running",
        "total_questions": total,
        "answered": len(questions),
        "correct_so_far": correct,
        "errors_so_far": errors,
        "score_pct_so_far": round(correct / len(questions) * 100, 2) if questions else 0,
        "per_level": level_stats,
        "smoke": smoke,
        "questions": questions,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="GAIA benchmark evaluation for the M365 Copilot companion"
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="Run only 8 text-only Level-1 questions (smoke test)."
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Path for results JSON. Default: .fleet/bench/gaia_results.json"
    )
    parser.add_argument(
        "--timeout", type=int, default=180,
        help="Seconds per question (default 180)."
    )
    parser.add_argument(
        "--level", type=str, default=None,
        help="Comma-separated level(s) to include, e.g. '1' or '1,2'. Default: all."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of questions to run."
    )
    parser.add_argument(
        "--split", type=str, default="validation",
        help="Dataset split: 'validation' (has gold answers) or 'test'. Default: validation."
    )
    parser.add_argument(
        "--ids-file", type=str, default=None,
        help="Path to a JSON list of task_ids; run ONLY those (retry mode)."
    )
    args = parser.parse_args()

    # Retry-subset: load task_ids to run
    only_ids = None
    if args.ids_file:
        only_ids = set(json.loads(Path(args.ids_file).read_text(encoding="utf-8")))
        print(f"Retry mode: restricting to {len(only_ids)} task_ids from {args.ids_file}")

    # Resolve output path
    if args.output:
        result_path = Path(args.output)
    else:
        suffix = "smoke" if args.smoke else "full"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        result_path = REPO_ROOT / ".fleet" / "bench" / f"gaia_{suffix}_{ts}.json"

    # Check API key
    api_key = os.environ.get("MCP_API_KEY", "")
    if not api_key:
        print("ERROR: MCP_API_KEY not set in .env or environment.", file=sys.stderr)
        sys.exit(1)

    # Preflight check
    err = preflight()
    if err:
        print(f"ERROR: Companion endpoint :8011 not reachable: {err}", file=sys.stderr)
        print("Start it with: python -m relay.openai_endpoint_server", file=sys.stderr)
        sys.exit(1)
    print("Companion :8011 preflight OK.")

    # Load dataset
    try:
        all_items = load_gaia_dataset(split=args.split)
    except Exception as exc:
        print(f"ERROR: Failed to load GAIA dataset: {exc}", file=sys.stderr)
        sys.exit(1)

    total_all = len(all_items)

    # Filter text-only
    text_only, attachment_count = filter_text_only(all_items)
    print(f"\nTotal items: {total_all}")
    print(f"  Excluded (file attachment required, endpoint cannot receive files): {attachment_count}")
    print(f"  Text-only (runnable): {len(text_only)}")
    print()

    # Parse level filter
    levels = None
    if args.level:
        try:
            levels = [int(x.strip()) for x in args.level.split(",")]
        except ValueError:
            print(f"ERROR: --level must be comma-separated integers, got: {args.level}", file=sys.stderr)
            sys.exit(1)

    # Run
    run_evaluation(
        items=text_only,
        api_key=api_key,
        timeout=args.timeout,
        verbose=True,
        result_path=result_path,
        smoke=args.smoke,
        levels=levels,
        limit=args.limit,
        only_ids=only_ids,
    )


if __name__ == "__main__":
    main()
