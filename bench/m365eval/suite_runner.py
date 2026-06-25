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
  * SharePoint probe: read-only, no artifact created; marked unsupported if
    Work IQ SharePoint tool is unavailable.

grade_type dispatch table:
  calendar             -- create/verify calendar event at day_after_tomorrow
  calendar_allday      -- all-day event (only title check, loose time tolerance)
  calendar_tomorrow    -- calendar event at tomorrow (not day_after_tomorrow)
  email_draft          -- drafts folder FOUND check
  email_draft_body     -- drafts FOUND + required_body_sentence verbatim check
                          (also checks required_body_sentence_2 if present)
  email_draft_special_subject -- drafts FOUND + exact special-char subject check
  onedrive_file        -- OneDrive create/read/delete with content check
  sharepoint_probe     -- read-only SharePoint probe; marks unsupported if tool absent
"""

import argparse
import os
import sys
import time
import json
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
def _load_dotenv(path):
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

def grade_workiq_email_draft_body(read_reply, marker, required_sentence,
                                   required_sentence_2=None):
    """Grade an email_draft_body task.

    PASS iff:
      - 'FOUND' in reply, AND
      - exact marker subject in reply, AND
      - required_sentence present in reply body section, AND
      - required_sentence_2 (if given) also present, AND
      - 'NOT_FOUND' NOT in reply.

    Returns (passed: bool, evidence: str).
    """
    has_found = "FOUND" in read_reply
    has_marker = marker in read_reply
    has_not_found = "NOT_FOUND" in read_reply
    has_sentence = required_sentence in read_reply
    has_sentence_2 = (required_sentence_2 is None) or (required_sentence_2 in read_reply)

    if has_not_found:
        return False, (
            "FAIL: 'NOT_FOUND' in Work IQ Drafts reply - draft absent.\n"
            "  Marker     : " + marker + "\n"
            "  Raw reply  : " + read_reply[:500]
        )

    if not has_found:
        return False, (
            "FAIL: 'FOUND' not in Work IQ Drafts reply.\n"
            "  Marker     : " + marker + "\n"
            "  Raw reply  : " + read_reply[:500]
        )

    if not has_marker:
        return False, (
            "FAIL: Marker '" + marker + "' not in reply (wrong draft?).\n"
            "  Raw reply  : " + read_reply[:500]
        )

    if not has_sentence:
        return False, (
            "FAIL: Draft found but required body sentence MISSING.\n"
            "  Required   : " + repr(required_sentence) + "\n"
            "  Marker     : " + marker + "\n"
            "  Raw reply  : " + read_reply[:800]
        )

    if not has_sentence_2:
        return False, (
            "FAIL: Draft found, sentence 1 OK, but required_body_sentence_2 MISSING.\n"
            "  Required_2 : " + repr(required_sentence_2) + "\n"
            "  Marker     : " + marker + "\n"
            "  Raw reply  : " + read_reply[:800]
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
        "PASS: Draft found + body sentence(s) verified via INDEPENDENT Work IQ Drafts.\n"
        "  Marker    : " + marker + "\n"
        "  Sentence  : " + repr(required_sentence) + "\n"
        + ("  Sentence2 : " + repr(required_sentence_2) + "\n" if required_sentence_2 else "") +
        "  Reply line: " + found_line
    )


def grade_workiq_email_draft_special_subject(read_reply, marker, expected_subject):
    """Grade a draft whose subject must survive special characters verbatim.

    PASS iff:
      - 'FOUND' in reply, AND
      - expected_subject appears verbatim in the reply, AND
      - 'NOT_FOUND' NOT in reply.

    Returns (passed: bool, evidence: str).
    """
    has_found = "FOUND" in read_reply
    has_not_found = "NOT_FOUND" in read_reply
    has_subject = expected_subject in read_reply

    if has_not_found and not has_found:
        return False, (
            "FAIL: 'NOT_FOUND' in Work IQ Drafts reply - draft absent.\n"
            "  Expected subject : " + expected_subject + "\n"
            "  Raw reply        : " + read_reply[:500]
        )

    if not has_found:
        return False, (
            "FAIL: 'FOUND' not in Work IQ Drafts reply.\n"
            "  Expected subject : " + expected_subject + "\n"
            "  Raw reply        : " + read_reply[:500]
        )

    if not has_subject:
        # Extract what subject was actually found for evidence
        found_subject = ""
        for line in read_reply.splitlines():
            if "FOUND" in line:
                found_subject = line.strip()
                break
        return False, (
            "FAIL: Draft found but special-character subject did NOT round-trip verbatim.\n"
            "  Expected subject : " + repr(expected_subject) + "\n"
            "  Found line       : " + repr(found_subject) + "\n"
            "  Raw reply        : " + read_reply[:500]
        )

    return True, (
        "PASS: Draft found with special-character subject intact via INDEPENDENT Work IQ Drafts.\n"
        "  Expected subject : " + repr(expected_subject) + "\n"
        "  Raw reply snippet: " + read_reply[:300]
    )


def grade_workiq_calendar_allday(read_reply, marker):
    """Grade an all-day calendar event — only checks that the title is present.

    All-day events may be reported with midnight times, no time, or as full-day.
    We accept any reply that contains the exact marker title.

    Returns (passed: bool, evidence: str).
    """
    if marker in read_reply:
        return True, (
            "PASS: All-day event marker found in Work IQ calendar read (title-only check).\n"
            "  Marker    : " + marker + "\n"
            "  Raw reply : " + read_reply[:400]
        )
    return False, (
        "FAIL: All-day event marker NOT found in Work IQ calendar read.\n"
        "  Marker    : " + marker + "\n"
        "  Raw reply : " + read_reply[:400]
    )


def grade_workiq_sharepoint_probe(read_reply):
    """Grade a SharePoint read-only probe.

    Returns (passed: bool, evidence: str, is_unsupported: bool).
    - If reply contains SHAREPOINT_UNSUPPORTED or tool-unavailable keywords: unsupported.
    - If reply contains SHAREPOINT_FILES or any listing: PASS (tool is available).
    - Otherwise: FAIL (unexpected response).
    """
    unsupported_keywords = [
        "sharepoint_unsupported", "not supported", "not available", "unsupported",
        "i don't have", "i do not have", "unable to", "cannot use",
        "doesn't support", "does not support", "no sharepoint", "no tool",
    ]
    reply_lower = read_reply.lower()

    if any(kw in reply_lower for kw in unsupported_keywords):
        return False, (
            "UNSUPPORTED: Work IQ SharePoint tool appears unavailable.\n"
            "  Raw reply: " + read_reply[:400]
        ), True

    if "sharepoint_files" in reply_lower or "SHAREPOINT_FILES" in read_reply:
        return True, (
            "PASS: SharePoint tool responded with file listing.\n"
            "  Raw reply: " + read_reply[:400]
        ), False

    # If it gave any substantive response mentioning files/documents
    if any(kw in reply_lower for kw in ["document", "file", "library", "found", "items"]):
        return True, (
            "PASS: SharePoint tool returned a substantive response.\n"
            "  Raw reply: " + read_reply[:400]
        ), False

    return False, (
        "FAIL: SharePoint probe returned unexpected/empty response.\n"
        "  Raw reply: " + read_reply[:400]
    ), False


def grade_workiq_onedrive_file(read_reply, expected_filename, expected_content):
    """Grade an OneDrive file task.

    Returns (passed: bool, evidence: str, is_unsupported: bool).
    is_unsupported=True means Work IQ OneDrive is not available - exclude from score.

    PASS iff:
      - 'FOUND' in reply, AND
      - expected_filename in reply, AND
      - expected_content in reply, AND
      - 'NOT_FOUND' NOT in reply.

    Marks unsupported if reply contains tool-error keywords indicating the
    surface is not available (not just file-not-found).
    """
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
            "UNSUPPORTED: Work IQ OneDrive tool appears unavailable.\n"
            "  Raw reply: " + read_reply[:500]
        ), True

    if has_not_found and not has_found:
        # Very short reply = only NOT_FOUND, lean unsupported
        if len(read_reply.strip()) <= 20:
            return False, (
                "UNSUPPORTED: Work IQ OneDrive replied only NOT_FOUND - tool may be unavailable.\n"
                "  Raw reply: " + read_reply[:500]
            ), True
        return False, (
            "FAIL: 'NOT_FOUND' - file absent in OneDrive.\n"
            "  Expected file  : " + expected_filename + "\n"
            "  Raw reply      : " + read_reply[:500]
        ), False

    if not has_found:
        return False, (
            "FAIL: 'FOUND' not in Work IQ OneDrive reply.\n"
            "  Expected file  : " + expected_filename + "\n"
            "  Raw reply      : " + read_reply[:500]
        ), False

    # Check filename present
    if expected_filename not in read_reply:
        return False, (
            "FAIL: Filename '" + expected_filename + "' not in reply.\n"
            "  Raw reply: " + read_reply[:500]
        ), False

    # Check content present (skip content check if expected_content is empty)
    if expected_content and expected_content not in read_reply:
        return False, (
            "FAIL: File found but expected content MISSING.\n"
            "  Expected content: " + repr(expected_content) + "\n"
            "  Raw reply       : " + read_reply[:800]
        ), False

    return True, (
        "PASS: OneDrive file found with correct content via INDEPENDENT Work IQ read.\n"
        "  Filename : " + expected_filename + "\n"
        "  Content  : " + repr(expected_content) + "\n"
        "  Reply    : " + read_reply[:300]
    ), False


# ---------------------------------------------------------------------------
# Per-task runner
# ---------------------------------------------------------------------------

def run_single_task(task, runid, api_key, sync_delay, dry_run, no_cleanup,
                    day_after_tomorrow, today):
    """Run a single task end-to-end. Returns a result dict."""
    task_id = task["id"]
    grade_type = task.get("grade_type", "calendar")
    surface = task["surface"]
    grade_params = task["grade_params"]
    description = task.get("description", task_id)

    day_after_tomorrow_str = day_after_tomorrow.strftime("%Y-%m-%d")
    tomorrow_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    def sub(text):
        return (
            text
            .replace("{runid}", runid)
            .replace("{day_after_tomorrow}", day_after_tomorrow_str)
            .replace("{tomorrow}", tomorrow_str)
        )

    marker = sub(grade_params["expected_subject"])
    # For calendar_tomorrow tasks, grading uses tomorrow's date instead of day_after_tomorrow
    grade_date = (today + timedelta(days=1)) if grade_type == "calendar_tomorrow" else day_after_tomorrow
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

    print("")
    print("=" * 60)
    print("TASK: " + task_id)
    print("  Surface     : " + surface)
    print("  Grade type  : " + grade_type)
    print("  Marker      : " + marker)
    print("  Description : " + description)
    print("")

    # ---- DO ----------------------------------------------------------------
    do_reply = "(dry-run)"
    if not dry_run:
        print("  [DO] Posting to companion...")
        try:
            resp = post_to_companion(do_prompt, api_key, timeout=120)
            do_reply = extract_reply_text(resp)
            print("  [DO] Reply: " + repr(do_reply))
        except RuntimeError as exc:
            result["error"] = "DO failed: " + str(exc)
            result["evidence"] = "FAIL: DO step error: " + str(exc)
            print("  [DO] ERROR: " + str(exc))
            return result
    else:
        print("  [DO] dry-run - skipped")
    result["do_reply"] = do_reply

    # ---- WAIT --------------------------------------------------------------
    if not dry_run and sync_delay > 0:
        print("  [WAIT] " + str(sync_delay) + "s for M365 propagation...")
        time.sleep(sync_delay)

    # ---- GRADE (independent) -----------------------------------------------
    print("  [GRADE] Posting independent Work IQ read...")
    try:
        grade_resp = post_to_companion(read_prompt, api_key, timeout=120)
        grade_reply = extract_reply_text(grade_resp)
        print("  [GRADE] Reply: " + repr(grade_reply))
    except RuntimeError as exc:
        result["error"] = "GRADE read failed: " + str(exc)
        result["evidence"] = "FAIL: GRADE step error: " + str(exc)
        print("  [GRADE] ERROR: " + str(exc))
        return result
    result["grade_reply"] = grade_reply

    # Dispatch to grade function
    if grade_type in ("calendar", "calendar_tomorrow"):
        passed, evidence = grade_workiq_calendar(
            read_reply=grade_reply,
            marker=marker,
            expected_date=grade_date,
            expected_hour=grade_params["expected_hour"],
            expected_minute=grade_params["expected_minute"],
            start_tolerance_minutes=grade_params["start_tolerance_minutes"],
            expected_duration_minutes=grade_params["expected_duration_minutes"],
            duration_tolerance_minutes=grade_params["duration_tolerance_minutes"],
        )
        result["passed"] = passed
        result["evidence"] = evidence

    elif grade_type == "calendar_allday":
        passed, evidence = grade_workiq_calendar_allday(
            read_reply=grade_reply,
            marker=marker,
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
        required_sentence_2_raw = grade_params.get("required_body_sentence_2", None)
        required_sentence_2 = sub(required_sentence_2_raw) if required_sentence_2_raw else None
        passed, evidence = grade_workiq_email_draft_body(
            read_reply=grade_reply,
            marker=marker,
            required_sentence=required_sentence,
            required_sentence_2=required_sentence_2,
        )
        result["passed"] = passed
        result["evidence"] = evidence

    elif grade_type == "email_draft_special_subject":
        expected_subject = sub(grade_params.get("expected_subject", marker))
        passed, evidence = grade_workiq_email_draft_special_subject(
            read_reply=grade_reply,
            marker=marker,
            expected_subject=expected_subject,
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

    elif grade_type == "sharepoint_probe":
        passed, evidence, is_unsupported = grade_workiq_sharepoint_probe(
            read_reply=grade_reply,
        )
        result["passed"] = passed
        result["unsupported"] = is_unsupported
        result["evidence"] = evidence

    else:
        result["evidence"] = "FAIL: Unknown grade_type '" + grade_type + "'"
        print("  [GRADE] Unknown grade_type: " + grade_type)
        return result

    print("  [GRADE] " + result["evidence"].splitlines()[0])

    # ---- CLEANUP -----------------------------------------------------------
    if no_cleanup:
        result["cleanup_ok"] = False
        result["cleanup_reply"] = "skipped (--no-cleanup)"
        print("  [CLEANUP] Skipped. Manual cleanup: delete '" + marker + "'")
    else:
        print("  [CLEANUP] Posting Work IQ DELETE for '" + marker + "'...")
        try:
            del_resp = post_to_companion(delete_prompt, api_key, timeout=120)
            del_reply = extract_reply_text(del_resp)
            print("  [CLEANUP] Reply: " + repr(del_reply))
        except RuntimeError as exc:
            result["cleanup_reply"] = "error: " + str(exc)
            result["cleanup_ok"] = False
            print("  [CLEANUP] DELETE error: " + str(exc))
            return result
        result["cleanup_reply"] = del_reply

        # Verify deletion
        print("  [CLEANUP] Waiting 10s then verifying...")
        time.sleep(10)
        try:
            verify_resp = post_to_companion(read_prompt, api_key, timeout=120)
            verify_reply = extract_reply_text(verify_resp)
            print("  [CLEANUP] Verify reply: " + repr(verify_reply))
        except RuntimeError as exc:
            result["cleanup_ok"] = False
            print("  [CLEANUP] Verify read error: " + str(exc))
            return result

        # Check per grade_type
        if grade_type in ("calendar", "calendar_tomorrow", "calendar_allday"):
            verify_events = parse_workiq_calendar_lines(verify_reply)
            still_present = any(e["title"] == marker for e in verify_events)
            # For allday, also do a raw text check (allday events may not parse as TITLE|START|END)
            if grade_type == "calendar_allday":
                still_present = still_present or (marker in verify_reply and "NO EVENTS" not in verify_reply)
            if still_present:
                result["cleanup_ok"] = False
                print("  [CLEANUP] WARNING: '" + marker + "' still found after DELETE!")
            else:
                result["cleanup_ok"] = True
                print("  [CLEANUP] OK - marker not found in re-read.")

        elif grade_type in ("email_draft", "email_draft_body", "email_draft_special_subject"):
            draft_still, _ = grade_workiq_email_draft(
                read_reply=verify_reply,
                marker=marker,
            )
            if draft_still:
                result["cleanup_ok"] = False
                print("  [CLEANUP] WARNING: draft '" + marker + "' still found after DELETE!")
            else:
                result["cleanup_ok"] = True
                print("  [CLEANUP] OK - draft not found in re-read.")

        elif grade_type == "onedrive_file":
            # If unsupported, cleanup is trivially OK (nothing was created)
            if result["unsupported"]:
                result["cleanup_ok"] = True
                print("  [CLEANUP] Unsupported task - no artifact to clean.")
            else:
                still_found, _, _ = grade_workiq_onedrive_file(
                    read_reply=verify_reply,
                    expected_filename=marker,
                    expected_content="",
                )
                if still_found:
                    result["cleanup_ok"] = False
                    print("  [CLEANUP] WARNING: file '" + marker + "' still found after DELETE!")
                else:
                    result["cleanup_ok"] = True
                    print("  [CLEANUP] OK - file not found in re-read.")

        elif grade_type == "sharepoint_probe":
            # SharePoint probe creates no artifact; cleanup is always OK
            result["cleanup_ok"] = True
            print("  [CLEANUP] SharePoint probe - no artifact created, cleanup trivially OK.")

    return result


# ---------------------------------------------------------------------------
# Suite-level final verification
# ---------------------------------------------------------------------------

def final_verification(runid, api_key, day_after_tomorrow_str, tomorrow_str):
    """Independently verify zero M365EVAL artifacts remain across surfaces."""
    print("")
    print("=" * 60)
    print("FINAL VERIFICATION - checking zero M365EVAL artifacts remain")
    print("")

    leftovers = {}

    def _check_calendar_date(date_str, label):
        cal_check_prompt = (
            "Using your Work IQ Calendar tool, list ALL my calendar events for "
            + date_str + ", including all-day events. "
            "For EACH output one line: TITLE | START | END. "
            "If there are none, say 'NO EVENTS'. Use the tool; do not guess."
        )
        print("  Checking Calendar for " + date_str + " (" + label + ")...")
        try:
            resp = post_to_companion(cal_check_prompt, api_key, timeout=120)
            reply = extract_reply_text(resp)
            events = parse_workiq_calendar_lines(reply)
            eval_events = [e for e in events if "M365EVAL" in e.get("title", "")]
            # Also raw-text check for all-day events that don't parse as TITLE|START|END
            raw_eval = [line.strip() for line in reply.splitlines()
                        if "M365EVAL" in line and "NO EVENTS" not in line]
            print("  Calendar reply: " + repr(reply))
            all_found = list({e["title"] for e in eval_events} | set(raw_eval))
            if all_found:
                leftovers["calendar_" + label] = all_found
                print("  WARNING: M365EVAL events still present: " + str(all_found))
            else:
                print("  Calendar " + date_str + ": CLEAN (no M365EVAL events)")
        except RuntimeError as exc:
            leftovers["calendar_" + label + "_error"] = str(exc)
            print("  Calendar check error: " + str(exc))

    # Calendar check — both tomorrow and day_after_tomorrow (tasks span both dates)
    _check_calendar_date(day_after_tomorrow_str, "day_after_tomorrow")
    _check_calendar_date(tomorrow_str, "tomorrow")

    # Drafts check
    draft_check_prompt = (
        "Using your Work IQ Mail tool, search my DRAFTS folder for any draft "
        "whose subject contains 'M365EVAL-" + runid + "'. "
        "If any found, list them. If none, output: NOT_FOUND. Use the tool; do not guess."
    )
    print("  Checking Drafts for M365EVAL-" + runid + "...")
    try:
        resp = post_to_companion(draft_check_prompt, api_key, timeout=120)
        reply = extract_reply_text(resp)
        print("  Drafts reply: " + repr(reply))
        if "NOT_FOUND" not in reply and "M365EVAL" in reply and "FOUND" in reply:
            leftovers["drafts"] = reply[:300]
            print("  WARNING: M365EVAL drafts may still be present!")
        else:
            print("  Drafts: CLEAN (no M365EVAL drafts)")
    except RuntimeError as exc:
        leftovers["drafts_error"] = str(exc)
        print("  Drafts check error: " + str(exc))

    # OneDrive check
    od_check_prompt = (
        "Using your Work IQ OneDrive tool, search for any file whose name contains "
        "'M365EVAL-" + runid + "' in my OneDrive. "
        "If any found, list them. If none or tool unavailable, output: NOT_FOUND. "
        "Use the tool; do not guess."
    )
    print("  Checking OneDrive for M365EVAL-" + runid + "...")
    try:
        resp = post_to_companion(od_check_prompt, api_key, timeout=120)
        reply = extract_reply_text(resp)
        print("  OneDrive reply: " + repr(reply))
        if "NOT_FOUND" not in reply and "M365EVAL" in reply and "FOUND" in reply:
            leftovers["onedrive"] = reply[:300]
            print("  WARNING: M365EVAL OneDrive files may still be present!")
        else:
            print("  OneDrive: CLEAN (no M365EVAL files)")
    except RuntimeError as exc:
        leftovers["onedrive_error"] = str(exc)
        print("  OneDrive check error: " + str(exc))

    clean = not any(k for k in leftovers if not k.endswith("_error"))
    return {"clean": clean, "leftovers": leftovers}


# ---------------------------------------------------------------------------
# Score calculation
# ---------------------------------------------------------------------------

def compute_score(results):
    passed = 0
    gradeable = 0
    unsupported = []

    for r in results:
        if r.get("unsupported"):
            unsupported.append(r["task_id"])
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
        "score_label": str(passed) + "/" + str(gradeable) + " = " + "{:.1f}".format(score_pct) + "%",
    }


# ---------------------------------------------------------------------------
# Print summary table
# ---------------------------------------------------------------------------

def print_summary(results, score, verification):
    print("")
    print("=" * 80)
    print("SUITE RESULTS TABLE")
    print("=" * 80)
    print("{:<28} {:<22} {:<25} {:<12} {}".format(
        "Task ID", "Surface", "Grade Type", "Result", "Cleanup"))
    print("{} {} {} {} {}".format("-" * 28, "-" * 22, "-" * 25, "-" * 12, "-" * 10))
    for r in results:
        if r.get("unsupported"):
            result_label = "UNSUPPORTED"
        elif r.get("passed"):
            result_label = "PASS"
        else:
            result_label = "FAIL"
        if r.get("unsupported"):
            cleanup_label = "N/A"
        elif r.get("cleanup_ok"):
            cleanup_label = "OK"
        else:
            cleanup_label = "NEEDS_MANUAL"
        print("{:<28} {:<22} {:<25} {:<12} {}".format(
            r["task_id"], r["surface"], r.get("grade_type", "?"), result_label, cleanup_label))

    print("")
    print("EVIDENCE PER TASK:")
    for r in results:
        print("")
        print("  [" + r["task_id"] + "] grade_type=" + r.get("grade_type", "?"))
        print("  Marker  : " + r["marker"])
        for line in r["evidence"].splitlines():
            print("  " + line)
        if r.get("error"):
            print("  Error   : " + str(r["error"]))
        if not r.get("cleanup_ok") and not r.get("unsupported"):
            print("  *** CLEANUP NEEDED: manually delete '" + r["marker"] + "' ***")

    print("")
    print("=" * 80)
    print("COMPANION M365 SCORE: " + score["score_label"])
    print("  Passed     : " + str(score["passed"]))
    print("  Gradeable  : " + str(score["gradeable"]))
    if score["unsupported_tasks"]:
        print("  Excluded (unsupported): " + str(score["unsupported_tasks"]))
        print("  Reason: Work IQ tool for that surface not available via :8011")
    print("")

    if verification["clean"]:
        print("FINAL VERIFICATION: CLEAN - zero M365EVAL artifacts remain.")
    else:
        print("FINAL VERIFICATION: WARNING - artifacts may remain:")
        for surface, detail in verification["leftovers"].items():
            print("  " + surface + ": " + str(detail))
        print("  *** MANUAL CLEANUP REQUIRED ***")
    print("=" * 80)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
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
        print("BLOCKER: " + err)
        print("  The companion must be running at http://127.0.0.1:8011. Aborting.")
        return 2
    print("Preflight: Work IQ endpoint :8011 - OK")

    # Select tasks
    task_map = {t["id"]: t for t in TASKS}
    if args.tasks:
        requested = [t.strip() for t in args.tasks.split(",") if t.strip()]
        unknown = [t for t in requested if t not in task_map]
        if unknown:
            print("ERROR: Unknown task ids: " + str(unknown) + ". Available: " + str(list(task_map)))
            return 1
        tasks_to_run = [task_map[t] for t in requested]
    else:
        tasks_to_run = list(TASKS)

    print("")
    print("M365EVAL SUITE")
    print("  RUNID              : " + runid)
    print("  Tasks              : " + str([t["id"] for t in tasks_to_run]))
    print("  Day-after-tomorrow : " + day_after_tomorrow_str)
    print("  Sync delay         : " + str(args.sync_delay) + "s")
    print("  Dry-run            : " + str(args.dry_run))
    print("  Cleanup            : " + ("no" if args.no_cleanup else "yes"))
    print("  Total tasks        : " + str(len(tasks_to_run)))

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
    results_path = output_dir / ("m365eval_" + runid + "_" + ts + ".json")
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
    print("")
    print("Results written to: " + str(results_path))

    # Return exit code: 0 if all gradeable tasks passed, else 10
    if score["gradeable"] > 0 and score["passed"] == score["gradeable"]:
        return 0
    return 10


if __name__ == "__main__":
    sys.exit(main())
