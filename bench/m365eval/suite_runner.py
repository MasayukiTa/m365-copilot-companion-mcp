"""
bench/m365eval/suite_runner.py
------------------------------
M365-native evaluation SUITE runner.

Runs the full task suite from tasks.py against the Work IQ endpoint at :8011
and computes a "companion M365 score" (passed / gradeable).

Usage:
    python bench/m365eval/suite_runner.py --runid <RUNID> [options]

Arguments:
    --runid      : Unique run identifier baked into all test artifact titles.
                   Also reads M365EVAL_RUNID env var. REQUIRED.
    --sync-delay : Seconds to wait between DO and GRADE steps. Default: 25
    --tasks      : Comma-separated task ids to run. Default: all tasks.
    --dry-run    : Skip all Copilot DO POSTs; jump straight to grading.
    --no-cleanup : Skip deletion of test artifacts after grading.
    --output-dir : Directory for the JSON results file (default: .fleet/bench)

Safety rules (hard-coded, not configurable):
  * NEVER calls outlook_send_mail or asks the agent to send email.
  * NEVER touches any artifact whose title differs from the exact marker.
  * MCP_API_KEY is read from .env at runtime; never printed, logged, or committed.
  * After the suite, verifies zero M365EVAL artifacts remain.
  * OneDrive tasks: if the independent read returns NOT_FOUND or a tool error,
    the task is marked 'unsupported' and excluded from the score denominator.
"""

import argparse
import os
import sys
import time
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Load .env
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

# Import runner helpers (avoid re-defining them)
import importlib.util as _ilu

_runner_path = Path(__file__).resolve().parent / "runner.py"
_spec = _ilu.spec_from_file_location("m365eval_runner", _runner_path)
_runner = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_runner)

_tasks_path = Path(__file__).resolve().parent / "tasks.py"
_tspec = _ilu.spec_from_file_location("m365eval_tasks", _tasks_path)
_tmod = _ilu.module_from_spec(_tspec)
_tspec.loader.exec_module(_tmod)
TASKS = _tmod.TASKS

# Aliases from runner
preflight_workiq = _runner.preflight_workiq
post_to_companion = _runner.post_to_companion
extract_reply_text = _runner.extract_reply_text
grade_workiq_calendar = _runner.grade_workiq_calendar
grade_workiq_email_draft = _runner.grade_workiq_email_draft
parse_workiq_calendar_lines = _runner.parse_workiq_calendar_lines

# ---------------------------------------------------------------------------
# New graders for extended grade_types
# ---------------------------------------------------------------------------

def grade_workiq_email_draft_body(
    read_reply: str,
    marker: str,
    required_sentence: str,
) -> tuple[bool, str]:
    """Grade an email_draft_body task.

    PASS iff:
      - 'FOUND' in reply, AND
      - exact marker subject in reply, AND
      - required_sentence present in reply body section, AND
      - 'NOT_FOUND' NOT in reply.
    """
    has_found = "FOUND" in read_reply
    has_marker = marker in read_reply
    has_not_found = "NOT_FOUND" in read_reply
    has_sentence = required_sentence in read_reply

    if has_not_found:
        return False, (
            f"FAIL: 'NOT_FOUND' in Work IQ Drafts reply — draft absent.\n"
            f"  Marker     : {marker}\n"
            f"  Raw reply  : {read_reply[:500]}"
        )

    if not has_found:
        return False, (
            f"FAIL: 'FOUND' not in Work IQ Drafts reply.\n"
            f"  Marker     : {marker}\n"
            f"  Raw reply  : {read_reply[:500]}"
        )

    if not has_marker:
        return False, (
            f"FAIL: Marker '{marker}' not in reply (wrong draft?).\n"
            f"  Raw reply  : {read_reply[:500]}"
        )

    if not has_sentence:
        return False, (
            f"FAIL: Draft found but required body sentence MISSING.\n"
            f"  Required   : {required_sentence!r}\n"
            f"  Marker     : {marker}\n"
            f"  Raw reply  : {read_reply[:800]}"
        )

    # Extract the FOUND line for evidence
    found_line = ""
    for line in read_reply.splitlines():
        if "FOUND" in line and marker in line:
            found_line = line.strip()
            break
    if not found_line:
        found_line = read_reply[:200]

    return True, (
        f"PASS: Draft found + body sentence verified via INDEPENDENT Work IQ Drafts.\n"
        f"  Marker    : {marker}\n"
        f"  Sentence  : {required_sentence!r}\n"
        f"  Reply line: {found_line}"
    )


def grade_workiq_onedrive_file(
    read_reply: str,
    expected_filename: str,
    expected_content: str,
) -> tuple[bool, str, bool]:
    """Grade an OneDrive file task.

    Returns (passed, evidence, is_unsupported).
    is_unsupported=True means Work IQ OneDrive is not available — exclude from score.

    PASS iff:
      - 'FOUND' in reply, AND
      - expected_filename in reply, AND
      - expected_content in reply after 'CONTENT:', AND
      - 'NOT_FOUND' NOT in reply.

    Marks unsupported if reply contains tool-error keywords or NOT_FOUND with
    error indicators suggesting the surface is not available (not just file-not-found).
    """
    # Heuristics for "tool not available" vs "file not found"
    error_keywords = [
        "not supported", "not available", "unsupported", "cannot access",
        "tool not found", "no tool", "onedrive tool", "work iq onedrive",
        "i don't have", "i do not have", "unable to", "cannot use",
        "doesn't support", "does not support",
    ]
    reply_lower = read_reply.lower()
    has_not_found = "NOT_FOUND" in read_reply or "not_found" in read_reply.lower()
    has_found = "FOUND" in read_reply

    # If the agent says it cannot use the tool at all, mark unsupported
    if not has_found and any(kw in reply_lower for kw in error_keywords):
        return False, (
            f"UNSUPPORTED: Work IQ OneDrive tool appears unavailable.\n"
            f"  Raw reply: {read_reply[:500]}"
        ), True

    if has_not_found and not has_found:
        # Could be file-not-found (DO failed) OR tool not available
        # If the reply is very short / just NOT_FOUND, lean unsupported
        if len(read_reply.strip()) <= 20:
            return False, (
                f"UNSUPPORTED: Work IQ OneDrive replied only NOT_FOUND — tool may be unavailable.\n"
                f"  Raw reply: {read_reply[:500]}"
            ), True
        return False, (
            f"FAIL: 'NOT_FOUND' — file absent in OneDrive.\n"
            f"  Expected file  : {expected_filename}\n"
            f"  Raw reply      : {read_reply[:500]}"
        ), False

    if not has_found:
        return False, (
            f"FAIL: 'FOUND' not in Work IQ OneDrive reply.\n"
            f"  Expected file  : {expected_filename}\n"
            f"  Raw reply      : {read_reply[:500]}"
        ), False

    # Check filename present
    if expected_filename not in read_reply:
        return False, (
            f"FAIL: Filename '{expected_filename}' not in reply.\n"
            f"  Raw reply: {read_reply[:500]}"
        ), False

    # Check content present
    if expected_content not in read_reply:
        return False, (
            f"FAIL: File found but expected content MISSING.\n"
            f"  Expected content: {expected_content!r}\n"
            f"  Raw reply       : {read_reply[:800]}"
        ), False

    return True, (
        f"PASS: OneDrive file found with correct content via INDEPENDENT Work IQ read.\n"
        f"  Filename : {expected_filename}\n"
        f"  Content  : {expected_content!r}\n"
        f"  Reply    : {read_reply[:300]}"
    ), False


# ---------------------------------------------------------------------------
# Per-task runner
# ---------------------------------------------------------------------------

ONEDRIVE_GRADE_TYPES = {"onedrive_file"}


def run_single_task(
    task: dict,
    runid: str,
    api_key: str,
    sync_delay: int,
    dry_run: bool,
    no_cleanup: bool,
    day_after_tomorrow: datetime,
    today: datetime,
) -> dict:
    """Run a single task end-to-end. Returns a result dict."""
    task_id = task["id"]
    grade_type = task.get("grade_type", "calendar")
    surface = task["surface"]
    grade_params = task["grade_params"]
    description = task.get("description", task_id)

    day_after_tomorrow_str = day_after_tomorrow.strftime("%Y-%m-%d")
    tomorrow_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    def sub(text: str) -> str:
        return (
            text
            .replace("{runid}", runid)
            .replace("{day_after_tomorrow}", day_after_tomorrow_str)
            .replace("{tomorrow}", tomorrow_str)
        )

    marker = sub(grade_params["expected_subject"])
    do_prompt = sub(task["prompt"])
    read_prompt = sub(task.get("read_prompt", ""))
    delete_prompt = sub(task.get("delete_prompt", ""))

    result = {
        "task_id": task_id,
        "surface": surface,
        "grade_type": grade_type,
        "description": description,
        "marker": marker,
        "passed": False,
        "unsupported": False,
        "evidence": "Not reached",
        "do_reply": "",
        "grade_reply": "",
        "cleanup_ok": False,
        "cleanup_reply": "",
        "error": None,
    }

    print(f"\n{'='*60}")
    print(f"TASK: {task_id}")
    print(f"  Surface     : {surface}")
    print(f"  Grade type  : {grade_type}")
    print(f"  Marker      : {marker}")
    print(f"  Description : {description}")
    print()

    # ---- DO ----------------------------------------------------------------
    do_reply = "(dry-run)"
    if not dry_run:
        print(f"  [DO] Posting to companion...")
        try:
            resp = post_to_companion(do_prompt, api_key, timeout=120)
            do_reply = extract_reply_text(resp)
            print(f"  [DO] Reply: {do_reply!r}")
        except RuntimeError as exc:
            result["error"] = f"DO failed: {exc}"
            result["evidence"] = f"FAIL: DO step error: {exc}"
            print(f"  [DO] ERROR: {exc}")
            return result
    else:
        print(f"  [DO] dry-run — skipped")
    result["do_reply"] = do_reply

    # ---- WAIT --------------------------------------------------------------
    if not dry_run and sync_delay > 0:
        print(f"  [WAIT] {sync_delay}s for M365 propagation...")
        time.sleep(sync_delay)

    # ---- GRADE (independent) -----------------------------------------------
    print(f"  [GRADE] Posting independent Work IQ read...")
    try:
        grade_resp = post_to_companion(read_prompt, api_key, timeout=120)
        grade_reply = extract_reply_text(grade_resp)
        print(f"  [GRADE] Reply: {grade_reply!r}")
    except RuntimeError as exc:
        result["error"] = f"GRADE read failed: {exc}"
        result["evidence"] = f"FAIL: GRADE step error: {exc}"
        print(f"  [GRADE] ERROR: {exc}")
        return result
    result["grade_reply"] = grade_reply

    # Dispatch to grade function
    if grade_type == "calendar":
        passed, evidence = grade_workiq_calendar(
            read_reply=grade_reply,
            marker=marker,
            expected_date=day_after_tomorrow,
            expected_hour=grade_params["expected_hour"],
            expected_minute=grade_params["expected_minute"],
            start_tolerance_minutes=grade_params["start_tolerance_minutes"],
            expected_duration_minutes=grade_params["expected_duration_minutes"],
            duration_tolerance_minutes=grade_params["duration_tolerance_minutes"],
        )
        result["passed"] = passed
        result["evidence"] = evidence

    elif grade_type == "email_draft":
        passed, evidence = grade_workiq_email_draft(
            read_reply=grade_reply,
            marker=marker,
        )
        result["passed"] = passed
        result["evidence"] = evidence

    elif grade_type == "email_draft_body":
        required_sentence = sub(grade_params.get("required_body_sentence", ""))
        passed, evidence = grade_workiq_email_draft_body(
            read_reply=grade_reply,
            marker=marker,
            required_sentence=required_sentence,
        )
        result["passed"] = passed
        result["evidence"] = evidence

    elif grade_type == "onedrive_file":
        expected_content = sub(grade_params.get("expected_file_content", ""))
        passed, evidence, is_unsupported = grade_workiq_onedrive_file(
            read_reply=grade_reply,
            expected_filename=marker,
            expected_content=expected_content,
        )
        result["passed"] = passed
        result["unsupported"] = is_unsupported
        result["evidence"] = evidence

    else:
        result["evidence"] = f"FAIL: Unknown grade_type '{grade_type}'"
        print(f"  [GRADE] Unknown grade_type: {grade_type}")
        return result

    print(f"  [GRADE] {result['evidence'].splitlines()[0]}")

    # ---- CLEANUP -----------------------------------------------------------
    if no_cleanup:
        result["cleanup_ok"] = False
        result["cleanup_reply"] = "skipped (--no-cleanup)"
        print(f"  [CLEANUP] Skipped (--no-cleanup). Manual cleanup: delete '{marker}'")
    else:
        print(f"  [CLEANUP] Posting Work IQ DELETE for '{marker}'...")
        try:
            del_resp = post_to_companion(delete_prompt, api_key, timeout=120)
            del_reply = extract_reply_text(del_resp)
            print(f"  [CLEANUP] Reply: {del_reply!r}")
        except RuntimeError as exc:
            result["cleanup_reply"] = f"error: {exc}"
            result["cleanup_ok"] = False
            print(f"  [CLEANUP] DELETE error: {exc}")
            return result
        result["cleanup_reply"] = del_reply

        # Verify deletion
        print(f"  [CLEANUP] Waiting 10s then verifying...")
        time.sleep(10)
        try:
            verify_resp = post_to_companion(read_prompt, api_key, timeout=120)
            verify_reply = extract_reply_text(verify_resp)
            print(f"  [CLEANUP] Verify reply: {verify_reply!r}")
        except RuntimeError as exc:
            result["cleanup_ok"] = False
            print(f"  [CLEANUP] Verify read error: {exc}")
            return result

        # Check per grade_type
        if grade_type == "calendar":
            verify_events = parse_workiq_calendar_lines(verify_reply)
            still_present = any(e["title"] == marker for e in verify_events)
            if still_present:
                result["cleanup_ok"] = False
                print(f"  [CLEANUP] WARNING: '{marker}' still found after DELETE!")
            else:
                result["cleanup_ok"] = True
                print(f"  [CLEANUP] OK — marker not found in re-read.")

        elif grade_type in ("email_draft", "email_draft_body"):
            draft_still, _ = grade_workiq_email_draft(
                read_reply=verify_reply,
                marker=marker,
            )
            if draft_still:
                result["cleanup_ok"] = False
                print(f"  [CLEANUP] WARNING: draft '{marker}' still found after DELETE!")
            else:
                result["cleanup_ok"] = True
                print(f"  [CLEANUP] OK — draft not found in re-read.")

        elif grade_type == "onedrive_file":
            # If unsupported, cleanup is trivially OK (nothing was created)
            if result["unsupported"]:
                result["cleanup_ok"] = True
                print(f"  [CLEANUP] Unsupported task — no artifact to clean.")
            else:
                still_found, _, _ = grade_workiq_onedrive_file(
                    read_reply=verify_reply,
                    expected_filename=marker,
                    expected_content="",  # just check presence
                )
                if still_found:
                    result["cleanup_ok"] = False
                    print(f"  [CLEANUP] WARNING: file '{marker}' still found after DELETE!")
                else:
                    result["cleanup_ok"] = True
                    print(f"  [CLEANUP] OK — file not found in re-read.")

    return result


# ---------------------------------------------------------------------------
# Suite-level final verification
# ---------------------------------------------------------------------------

def final_verification(
    runid: str,
    api_key: str,
    day_after_tomorrow_str: str,
    tomorrow_str: str,
) -> dict:
    """Independently verify zero M365EVAL artifacts remain across surfaces."""
    print(f"\n{'='*60}")
    print("FINAL VERIFICATION — checking zero M365EVAL artifacts remain")
    print()

    leftovers = {}

    # Calendar check
    cal_check_prompt = (
        f"Using your Work IQ Calendar tool, list ALL my calendar events for {day_after_tomorrow_str}. "
        "For EACH output one line: TITLE | START | END. "
        "If there are none, say 'NO EVENTS'. Use the tool; do not guess."
    )
    print(f"  Checking Calendar for {day_after_tomorrow_str}...")
    try:
        resp = post_to_companion(cal_check_prompt, api_key, timeout=120)
        reply = extract_reply_text(resp)
        events = parse_workiq_calendar_lines(reply)
        eval_events = [e for e in events if "M365EVAL" in e.get("title", "")]
        print(f"  Calendar reply: {reply!r}")
        if eval_events:
            leftovers["calendar"] = [e["title"] for e in eval_events]
            print(f"  WARNING: M365EVAL events still present: {leftovers['calendar']}")
        else:
            print(f"  Calendar: CLEAN (no M365EVAL events)")
    except RuntimeError as exc:
        leftovers["calendar_error"] = str(exc)
        print(f"  Calendar check error: {exc}")

    # Drafts check
    draft_check_prompt = (
        f"Using your Work IQ Mail tool, search my DRAFTS folder for any draft "
        f"whose subject contains 'M365EVAL-{runid}'. "
        "If any found, list them. If none, output: NOT_FOUND. Use the tool; do not guess."
    )
    print(f"\n  Checking Drafts for M365EVAL-{runid}...")
    try:
        resp = post_to_companion(draft_check_prompt, api_key, timeout=120)
        reply = extract_reply_text(resp)
        print(f"  Drafts reply: {reply!r}")
        if "NOT_FOUND" not in reply and "M365EVAL" in reply and "FOUND" in reply:
            leftovers["drafts"] = reply[:300]
            print(f"  WARNING: M365EVAL drafts may still be present!")
        else:
            print(f"  Drafts: CLEAN (no M365EVAL drafts)")
    except RuntimeError as exc:
        leftovers["drafts_error"] = str(exc)
        print(f"  Drafts check error: {exc}")

    # OneDrive check
    od_check_prompt = (
        f"Using your Work IQ OneDrive tool, search for any file whose name contains "
        f"'M365EVAL-{runid}' in my OneDrive. "
        "If any found, list them. If none or tool unavailable, output: NOT_FOUND. "
        "Use the tool; do not guess."
    )
    print(f"\n  Checking OneDrive for M365EVAL-{runid}...")
    try:
        resp = post_to_companion(od_check_prompt, api_key, timeout=120)
        reply = extract_reply_text(resp)
        print(f"  OneDrive reply: {reply!r}")
        if "NOT_FOUND" not in reply and "M365EVAL" in reply and "FOUND" in reply:
            leftovers["onedrive"] = reply[:300]
            print(f"  WARNING: M365EVAL OneDrive files may still be present!")
        else:
            print(f"  OneDrive: CLEAN (no M365EVAL files)")
    except RuntimeError as exc:
        leftovers["onedrive_error"] = str(exc)
        print(f"  OneDrive check error: {exc}")

    clean = not any(
        k for k in leftovers
        if not k.endswith("_error")
    )
    return {"clean": clean, "leftovers": leftovers}


# ---------------------------------------------------------------------------
# Score calculation
# ---------------------------------------------------------------------------

def compute_score(results: list[dict]) -> dict:
    passed = 0
    gradeable = 0
    unsupported = []

    for r in results:
        if r.get("unsupported"):
            unsupported.append(r["task_id"])
        elif r.get("error") and not r.get("passed"):
            # Error tasks count as failed (not unsupported unless OneDrive)
            gradeable += 1
        else:
            gradeable += 1
            if r.get("passed"):
                passed += 1

    score_pct = (passed / gradeable * 100) if gradeable > 0 else 0.0
    return {
        "passed": passed,
        "gradeable": gradeable,
        "unsupported_tasks": unsupported,
        "score_pct": score_pct,
        "score_label": f"{passed}/{gradeable} = {score_pct:.1f}%",
    }


# ---------------------------------------------------------------------------
# Print summary table
# ---------------------------------------------------------------------------

def print_summary(results: list[dict], score: dict, verification: dict) -> None:
    print(f"\n{'='*70}")
    print("SUITE RESULTS TABLE")
    print(f"{'='*70}")
    print(f"{'Task ID':<30} {'Surface':<22} {'Result':<12} {'Cleanup'}")
    print(f"{'-'*30} {'-'*22} {'-'*12} {'-'*10}")
    for r in results:
        result_label = (
            "UNSUPPORTED" if r.get("unsupported")
            else ("PASS" if r.get("passed") else "FAIL")
        )
        cleanup_label = (
            "N/A" if r.get("unsupported")
            else ("OK" if r.get("cleanup_ok") else "NEEDS_MANUAL")
        )
        print(f"{r['task_id']:<30} {r['surface']:<22} {result_label:<12} {cleanup_label}")

    print()
    print("EVIDENCE PER TASK:")
    for r in results:
        print(f"\n  [{r['task_id']}]")
        print(f"  Marker  : {r['marker']}")
        for line in r["evidence"].splitlines():
            print(f"  {line}")
        if r.get("error"):
            print(f"  Error   : {r['error']}")
        if not r.get("cleanup_ok") and not r.get("unsupported"):
            print(f"  *** CLEANUP NEEDED: manually delete '{r['marker']}' ***")

    print(f"\n{'='*70}")
    print(f"COMPANION M365 SCORE: {score['score_label']}")
    if score["unsupported_tasks"]:
        print(f"  Excluded (unsupported): {score['unsupported_tasks']}")
        print(f"  Reason: Work IQ OneDrive create/read/delete not available via :8011")
    print()

    if verification["clean"]:
        print("FINAL VERIFICATION: CLEAN — zero M365EVAL artifacts remain.")
    else:
        print("FINAL VERIFICATION: WARNING — artifacts may remain:")
        for surface, detail in verification["leftovers"].items():
            print(f"  {surface}: {detail}")
        print("  *** MANUAL CLEANUP REQUIRED ***")
    print(f"{'='*70}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="M365-native evaluation SUITE runner"
    )
    parser.add_argument(
        "--runid", default=os.environ.get("M365EVAL_RUNID", ""),
        help="Unique run identifier. Also reads M365EVAL_RUNID env var. REQUIRED.",
    )
    parser.add_argument(
        "--sync-delay", type=int, default=25,
        help="Seconds to wait for M365 propagation between DO and GRADE (default: 25)",
    )
    parser.add_argument(
        "--tasks", default="",
        help="Comma-separated task ids to run (default: all tasks)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip all companion DO POSTs; jump straight to grading",
    )
    parser.add_argument(
        "--no-cleanup", action="store_true",
        help="Skip deletion of test artifacts after grading",
    )
    parser.add_argument(
        "--output-dir", default="",
        help="Directory for the JSON results file (default: .fleet/bench under repo root)",
    )
    args = parser.parse_args()

    if not args.runid:
        print("ERROR: --runid is required (or set M365EVAL_RUNID env var).")
        print("  Example: python bench/m365eval/suite_runner.py --runid SUITE20260625A")
        return 1

    runid = args.runid
    today = datetime.now()
    day_after_tomorrow = today + timedelta(days=2)
    day_after_tomorrow_str = day_after_tomorrow.strftime("%Y-%m-%d")
    tomorrow_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    api_key = os.environ.get("MCP_API_KEY", "")
    if not api_key:
        print("BLOCKER: MCP_API_KEY not found in environment / .env")
        return 3

    # Preflight
    err = preflight_workiq()
    if err:
        print(f"BLOCKER: {err}")
        print("  The companion must be running at http://127.0.0.1:8011. Aborting.")
        return 2
    print("Preflight: Work IQ endpoint :8011 — OK")

    # Select tasks
    task_map = {t["id"]: t for t in TASKS}
    if args.tasks:
        requested = [t.strip() for t in args.tasks.split(",") if t.strip()]
        unknown = [t for t in requested if t not in task_map]
        if unknown:
            print(f"ERROR: Unknown task ids: {unknown}. Available: {list(task_map)}")
            return 1
        tasks_to_run = [task_map[t] for t in requested]
    else:
        tasks_to_run = list(TASKS)

    print(f"\nM365EVAL SUITE")
    print(f"  RUNID              : {runid}")
    print(f"  Tasks              : {[t['id'] for t in tasks_to_run]}")
    print(f"  Day-after-tomorrow : {day_after_tomorrow_str}")
    print(f"  Sync delay         : {args.sync_delay}s")
    print(f"  Dry-run            : {args.dry_run}")
    print(f"  Cleanup            : {'no' if args.no_cleanup else 'yes'}")
    print(f"  Total tasks        : {len(tasks_to_run)}")

    results = []
    for task in tasks_to_run:
        result = run_single_task(
            task=task,
            runid=runid,
            api_key=api_key,
            sync_delay=args.sync_delay,
            dry_run=args.dry_run,
            no_cleanup=args.no_cleanup,
            day_after_tomorrow=day_after_tomorrow,
            today=today,
        )
        results.append(result)

    # Final verification
    if not args.no_cleanup:
        verification = final_verification(
            runid=runid,
            api_key=api_key,
            day_after_tomorrow_str=day_after_tomorrow_str,
            tomorrow_str=tomorrow_str,
        )
    else:
        verification = {"clean": False, "leftovers": {"note": "cleanup skipped"}}

    score = compute_score(results)
    print_summary(results, score, verification)

    # Write JSON results
    output_dir = Path(args.output_dir) if args.output_dir else REPO_ROOT / ".fleet" / "bench"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_path = output_dir / f"m365eval_{runid}_{ts}.json"
    output_data = {
        "runid": runid,
        "timestamp": ts,
        "day_after_tomorrow": day_after_tomorrow_str,
        "score": score,
        "verification": verification,
        "tasks": results,
    }
    results_path.write_text(
        json.dumps(output_data, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nResults written to: {results_path}")

    # Return exit code: 0 if all gradeable tasks passed, else 10
    if score["gradeable"] > 0 and score["passed"] == score["gradeable"]:
        return 0
    return 10


if __name__ == "__main__":
    sys.exit(main())
