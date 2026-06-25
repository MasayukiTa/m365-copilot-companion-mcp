"""
bench/m365eval/runner.py
------------------------
M365-native evaluation runner.

Default grader: Work IQ (the companion's built-in M365 connectors via :8011).
Legacy grader : Outlook COM (--grader outlook), kept but NOT the default.

Usage:
    python bench/m365eval/runner.py --runid <RUNID> [options]

Arguments:
    --runid      : Unique run identifier baked into all test artifact titles.
                   Also reads M365EVAL_RUNID env var. REQUIRED.
    --task       : Task id from tasks.py. Default: cal_create_30min
    --grader     : workiq (default) or outlook.
                   workiq  — grade via an independent Work IQ Calendar LIST prompt
                             sent to :8011 (no local Outlook required).
                   outlook — grade via local Outlook COM (legacy; requires pywin32
                             + Outlook desktop running and connected).
    --sync-delay : Seconds to wait between DO and GRADE steps. Default: 25
    --dry-run    : Skip the Copilot DO POST; jump straight to grading.
    --no-cleanup : Skip deletion of the test artifact after grading.

How it works (Work IQ path)
---------------------------
1. Preflight  — GET /v1/models from :8011; confirm 200 OK.
2. DO         — POST the task's "do" prompt to :8011 (Work IQ action). Capture reply.
3. Wait       — Allow M365 propagation (default 25s). Self-reported 'done' NOT trusted.
4. GRADE      — POST the task's "read" prompt to :8011 as an INDEPENDENT factual lookup;
                parse structured "TITLE | START | END" output; PASS iff a line matches
                the expected marker and timing criteria (±5 min).
5. CLEANUP    — POST the task's "delete" prompt; then re-POST "read" and confirm the
                marker is GONE. If not gone after cleanup, report manual cleanup needed.
6. Report     — Print PASS/FAIL + matched-read evidence + cleanup status.

Safety rules (hard-coded, not configurable):
  * NEVER calls outlook_send_mail or asks the agent to send email or invitations.
  * NEVER touches any artifact whose title/subject differs from the exact marker.
  * All Work IQ prompts include "Do NOT delete/touch any other event/file."
  * MCP_API_KEY is read from .env at runtime; never printed, logged, or committed.
  * Cleanup is verified by an independent re-read before declaring success.
"""

import argparse
import os
import sys
import time
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup: allow running from repo root or from bench/m365eval/
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Load .env (no external library required — simple key=value parser)
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    """Load key=value pairs from a .env file into os.environ (no-BOM safe)."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8-sig")  # strips BOM if present
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
# Shared HTTP helpers
# ---------------------------------------------------------------------------

COMPANION_BASE = "http://127.0.0.1:8011"


def preflight_workiq() -> str | None:
    """Return None if :8011 /v1/models responds 200, else an error string."""
    url = f"{COMPANION_BASE}/v1/models"
    req = urllib.request.Request(url, method="GET")
    api_key = os.environ.get("MCP_API_KEY", "")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return None
            return f"Unexpected status {resp.status} from {url}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} from {url}: {exc.reason}"
    except urllib.error.URLError as exc:
        return f"Cannot reach companion at {url}: {exc.reason}"


def post_to_companion(prompt: str, api_key: str, timeout: int = 120) -> dict:
    """POST a chat completion to the companion endpoint at :8011.

    Model name matches what the endpoint expects for the Work IQ agent.
    Returns the parsed JSON response dict.
    Raises RuntimeError on HTTP or network error.
    """
    url = f"{COMPANION_BASE}/v1/chat/completions"
    payload = json.dumps({
        "model": "m365-copilot-opus",
        "messages": [{"role": "user", "content": prompt}],
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
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from companion: {body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach companion at {url}: {exc.reason}") from exc


def extract_reply_text(response: dict) -> str:
    """Pull the assistant text content out of a chat completion response."""
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return str(response)


# ---------------------------------------------------------------------------
# Work IQ grader — parses "TITLE | START | END" lines from the agent's reply
# ---------------------------------------------------------------------------

_DT_TOKEN_RE = re.compile(
    r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2})?'
)


def parse_workiq_calendar_lines(text: str) -> list[dict]:
    """Parse lines of the form 'TITLE | START | END' from a Work IQ LIST reply.

    Handles two formats:
    - One structured entry per line (normal case)
    - All entries on a single line without newlines (observed in practice when
      the calendar contains Japanese-titled events mixed with ASCII-titled ones)

    Strategy:
      1. Try line-by-line parsing first (fastest path).
      2. If that yields 0 results, fall back to token-splitting: locate all
         " | YYYY-MM-DD HH:MM" substrings, then reconstruct triplets by
         reading backwards from each datetime token to find its title.
    """
    results = []

    # Pass 1: line-by-line (only works if the reply actually contains newlines)
    lines = text.splitlines()
    if len(lines) > 1:
        results = _parse_pipe_lines(lines)
        if results:
            return results

    # Pass 2: single-line fallback (or multi-line that yielded 0 results).
    # Find all pipe+datetime tokens and their positions in the string.
    # Each "| YYYY-MM-DD HH:MM" pair forms a start|end pair for one event.
    # The title is whatever appears between the previous END token position
    # and the current " | " that precedes the START token.

    # Split on " | " to get segments, then re-assemble as (title, start, end) triples.
    # The format is: ... TITLE | START | END TITLE | START | END ...
    # Splitting on " | " gives: [TITLE, START, END+TITLE, START, END+TITLE, ...]
    pipe_split = re.split(r'\s*\|\s*', text)
    # Walk through triplets: pipe_split[i] is title-or-end+title, [i+1] is start, [i+2] is end
    # We need to find which segments are datetime strings.
    i = 0
    while i < len(pipe_split) - 2:
        title_raw = pipe_split[i]
        start_raw = pipe_split[i + 1].strip()
        end_raw = pipe_split[i + 2].strip()

        start_dt = _parse_datetime_loose(start_raw)
        if start_dt is not None:
            # start_raw parses as datetime — this is a valid triplet.
            # The title is the last "word" in title_raw (after any previous END datetime).
            # Strip the leading datetime prefix from title_raw.
            title = _DT_TOKEN_RE.sub("", title_raw).strip()
            # Also strip any leading space left over
            title = title.strip()

            # end_raw may be "YYYY-MM-DD HH:MM NEXTTITLE" — take just the datetime part.
            end_match = re.match(
                r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2})?)',
                end_raw,
            )
            if end_match:
                end_clean = end_match.group(1)
            else:
                end_clean = end_raw
            end_dt = _parse_datetime_loose(end_clean)

            if title:
                results.append({
                    "title": title,
                    "start_raw": start_raw,
                    "end_raw": end_clean,
                    "start_dt": start_dt,
                    "end_dt": end_dt,
                })
            i += 2  # advance by 2; the loop will do +1, so net +3
        else:
            i += 1
    return results


def _parse_pipe_lines(lines: list[str]) -> list[dict]:
    """Parse a list of lines for TITLE | START | END entries."""
    results = []
    for line in lines:
        line = line.strip()
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        title = parts[0]
        start_raw = parts[1]
        end_raw = parts[2]
        if not title:
            continue
        start_dt = _parse_datetime_loose(start_raw)
        end_dt = _parse_datetime_loose(end_raw)
        if start_dt is None:
            continue  # require parseable start to count
        results.append({
            "title": title,
            "start_raw": start_raw,
            "end_raw": end_raw,
            "start_dt": start_dt,
            "end_dt": end_dt,
        })
    return results


# Datetime patterns to try when parsing the agent's output
_DT_FORMATS = [
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%d %I:%M %p",
    "%Y-%m-%d %I:%M%p",
]


def _parse_datetime_loose(text: str) -> datetime | None:
    """Try multiple datetime formats; return the first that parses, or None."""
    text = text.strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    # Try stripping seconds or timezone suffix (e.g. "2026-06-26 14:00:00+09:00")
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", text)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return None


def grade_workiq_calendar(
    read_reply: str,
    marker: str,
    expected_date: datetime,
    expected_hour: int,
    expected_minute: int,
    start_tolerance_minutes: int,
    expected_duration_minutes: int,
    duration_tolerance_minutes: int,
) -> tuple[bool, str]:
    """Grade a Work IQ calendar LIST reply against expected parameters.

    Returns (passed: bool, evidence: str).
    The reply is trusted ONLY for what it reports; the agent's self-reported
    'done' from the DO step is NOT used here.
    """
    events = parse_workiq_calendar_lines(read_reply)
    marker_events = [e for e in events if e["title"] == marker]

    if not marker_events:
        # Also check for partial match in the raw text (defensive)
        if marker not in read_reply:
            return False, (
                f"FAIL: Marker '{marker}' NOT found in Work IQ independent read.\n"
                f"  Raw reply: {read_reply[:500]}"
            )
        return False, (
            f"FAIL: Marker '{marker}' appears in reply but could not parse as "
            f"a structured TITLE|START|END line.\n"
            f"  Raw reply: {read_reply[:500]}"
        )

    expected_start = expected_date.replace(
        hour=expected_hour, minute=expected_minute, second=0, microsecond=0
    )

    for ev in marker_events:
        s_dt = ev["start_dt"]
        e_dt = ev["end_dt"]

        if s_dt is None:
            return False, (
                f"FAIL: Found marker '{marker}' but could not parse start time: "
                f"{ev['start_raw']!r}\n  Raw reply: {read_reply[:400]}"
            )

        delta_min = abs((s_dt - expected_start).total_seconds()) / 60
        if delta_min > start_tolerance_minutes:
            return False, (
                f"FAIL: Found marker but start time mismatch.\n"
                f"  Expected  : {expected_start.isoformat()}\n"
                f"  Got       : {s_dt.isoformat()} (delta={delta_min:.1f}min, "
                f"tol={start_tolerance_minutes}min)\n"
                f"  Raw START : {ev['start_raw']!r}"
            )

        if e_dt is not None:
            duration_actual = (e_dt - s_dt).total_seconds() / 60
            if abs(duration_actual - expected_duration_minutes) > duration_tolerance_minutes:
                return False, (
                    f"FAIL: Start OK but duration mismatch.\n"
                    f"  Expected duration : {expected_duration_minutes}min "
                    f"(+/-{duration_tolerance_minutes}min)\n"
                    f"  Actual duration   : {duration_actual:.0f}min\n"
                    f"  START : {s_dt.isoformat()}\n"
                    f"  END   : {e_dt.isoformat()}"
                )
            duration_str = f"{duration_actual:.0f}min"
        else:
            duration_str = "(end time unparseable)"

        evidence = (
            f"PASS: Marker found via INDEPENDENT Work IQ read.\n"
            f"  Marker    : {ev['title']}\n"
            f"  START     : {ev['start_raw']!r} (parsed: {s_dt.isoformat()})\n"
            f"  END       : {ev['end_raw']!r} (duration: {duration_str})\n"
            f"  Start delta: {delta_min:.1f}min (tol={start_tolerance_minutes}min)"
        )
        return True, evidence

    return False, f"FAIL: Unexpected state; marker_events={marker_events!r}"


# ---------------------------------------------------------------------------
# Legacy Outlook COM grader (--grader outlook)
# ---------------------------------------------------------------------------

def preflight_outlook() -> str | None:
    """Return None if Outlook COM is reachable and connected, else an error string."""
    EXCHANGE_MODES = {
        0: "olNoExchange", 100: "olOffline", 200: "olCachedOffline",
        300: "olDisconnected", 400: "olCachedDisconnected",
        500: "olCachedConnectedHeaders", 600: "olCachedConnectedDrizzle",
        700: "olOnline", 800: "olCachedConnectedFull",
    }
    CONNECTED_MODES = {600, 700, 800}
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
        pythoncom.CoInitialize()
        try:
            ol = win32com.client.Dispatch("Outlook.Application")
            ns = ol.GetNamespace("MAPI")
            try:
                mode = ns.ExchangeConnectionMode
                mode_name = EXCHANGE_MODES.get(mode, f"unknown({mode})")
                if mode not in CONNECTED_MODES:
                    return (
                        f"Outlook Exchange not connected: mode={mode} ({mode_name}). "
                        f"Requires mode 600/700/800."
                    )
                return None
            except Exception:
                return None
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:
        return f"Outlook COM not available: {exc}"


def read_calendar_for_marker_outlook(marker_subject: str, search_days: int = 3) -> list[dict]:
    """Read calendar via Outlook COM for the exact marker subject."""
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pywin32 not available") from exc

    results = []
    pythoncom.CoInitialize()
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        ns = ol.GetNamespace("MAPI")
        cal = ns.GetDefaultFolder(9)
        items = cal.Items
        subj_filter = f"[Subject] = '{marker_subject}'"
        filtered = items.Restrict(subj_filter)
        now = datetime.now()
        window_end = now + timedelta(days=search_days + 1)
        for i in range(1, filtered.Count + 1):
            item = filtered.Item(i)
            try:
                subj = getattr(item, "Subject", "") or ""
                if subj != marker_subject:
                    continue
                start = getattr(item, "Start", None)
                end = getattr(item, "End", None)
                if hasattr(start, "year"):
                    start_dt = datetime(start.year, start.month, start.day,
                                        start.hour, start.minute, start.second)
                    if start_dt > window_end:
                        continue
                results.append({"subject": subj, "start": start, "end": end,
                                 "organizer": getattr(item, "Organizer", "") or ""})
            except Exception:
                continue
    finally:
        pythoncom.CoUninitialize()
    return results


def delete_marker_outlook(marker_subject: str) -> str:
    """Delete ONLY the calendar item with the exact marker subject via COM."""
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as exc:
        return f"[delete error: pywin32 not available: {exc}]"
    deleted_count = 0
    pythoncom.CoInitialize()
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        ns = ol.GetNamespace("MAPI")
        cal = ns.GetDefaultFolder(9)
        items = cal.Items
        subj_filter = f"[Subject] = '{marker_subject}'"
        filtered = items.Restrict(subj_filter)
        to_delete = []
        for i in range(1, filtered.Count + 1):
            item = filtered.Item(i)
            try:
                if getattr(item, "Subject", "") == marker_subject:
                    to_delete.append(item)
            except Exception:
                continue
        for item in to_delete:
            try:
                item.Delete()
                deleted_count += 1
            except Exception as exc:
                return f"[delete error on item: {exc}]"
    finally:
        pythoncom.CoUninitialize()
    if deleted_count == 0:
        return f"No event with subject '{marker_subject}' found to delete."
    return f"Deleted {deleted_count} event(s) with subject '{marker_subject}'."


def grade_outlook_calendar(events: list[dict], grade_params: dict, runid: str) -> tuple[bool, str]:
    """Grade via Outlook COM read results."""
    expected_subject = grade_params["expected_subject"].replace("{runid}", runid)
    expected_hour = grade_params["expected_hour"]
    expected_minute = grade_params["expected_minute"]
    tol_min = grade_params["start_tolerance_minutes"]
    expected_duration = grade_params["expected_duration_minutes"]
    duration_tol = grade_params["duration_tolerance_minutes"]
    tomorrow = (datetime.now() + timedelta(days=1)).date()

    if not events:
        return False, f"FAIL: No calendar event found with subject '{expected_subject}'"

    for ev in events:
        start = ev["start"]
        end = ev["end"]
        if hasattr(start, "year"):
            start_dt = datetime(start.year, start.month, start.day,
                                start.hour, start.minute, start.second)
        else:
            return False, f"FAIL: Could not parse start time: {start!r}"
        if hasattr(end, "year"):
            end_dt = datetime(end.year, end.month, end.day,
                              end.hour, end.minute, end.second)
        else:
            return False, f"FAIL: Could not parse end time: {end!r}"
        if start_dt.date() != tomorrow:
            continue
        expected_start = start_dt.replace(hour=expected_hour, minute=expected_minute, second=0)
        delta_min = abs((start_dt - expected_start).total_seconds()) / 60
        if delta_min > tol_min:
            continue
        duration_actual = (end_dt - start_dt).total_seconds() / 60
        if abs(duration_actual - expected_duration) > duration_tol:
            return False, (
                f"FAIL: Event found with correct subject and start, "
                f"but duration={duration_actual:.0f}min "
                f"(expected {expected_duration}+/-{duration_tol} min).\n"
                f"  Subject : {ev['subject']}\n"
                f"  Start   : {start_dt.isoformat()}\n"
                f"  End     : {end_dt.isoformat()}"
            )
        return True, (
            f"PASS: Event verified via Outlook COM read.\n"
            f"  Subject  : {ev['subject']}\n"
            f"  Start    : {start_dt.isoformat()}\n"
            f"  End      : {end_dt.isoformat()} ({int(duration_actual)}min)\n"
            f"  Organizer: {ev['organizer']}"
        )

    return False, (
        f"FAIL: Found {len(events)} event(s) with subject '{expected_subject}', "
        f"but none matched tomorrow {expected_hour:02d}:{expected_minute:02d} "
        f"within +/-{tol_min} min."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="M365-native eval runner (Work IQ path, calendar + OneDrive)"
    )
    parser.add_argument(
        "--runid", default=os.environ.get("M365EVAL_RUNID", ""),
        help="Unique run identifier embedded in all test artifact titles. "
             "Also reads M365EVAL_RUNID env var. REQUIRED.",
    )
    parser.add_argument("--task", default="cal_create_30min",
                        help="Task id from tasks.py (default: cal_create_30min)")
    parser.add_argument(
        "--grader", choices=["workiq", "outlook"], default="workiq",
        help="workiq (default) - grade via independent Work IQ LIST prompt. "
             "outlook - grade via local Outlook COM (legacy; requires pywin32).",
    )
    parser.add_argument("--sync-delay", type=int, default=25,
                        help="Seconds to wait for M365 propagation (default: 25)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip the Copilot DO POST; jump straight to grading")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="Skip deletion of the test artifact after grading")
    args = parser.parse_args()

    if not args.runid:
        print("ERROR: --runid is required (or set M365EVAL_RUNID env var).")
        print("  Example: python bench/m365eval/runner.py --runid TEST20260625")
        return 1

    runid = args.runid

    # Import tasks
    import importlib.util as _ilu
    _tasks_path = Path(__file__).resolve().parent / "tasks.py"
    _spec = _ilu.spec_from_file_location("m365eval_tasks", _tasks_path)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    TASKS = _mod.TASKS
    task_map = {t["id"]: t for t in TASKS}
    if args.task not in task_map:
        print(f"ERROR: Unknown task '{args.task}'. Available: {list(task_map)}")
        return 1
    task = task_map[args.task]

    # Compute tomorrow's date for prompt substitution
    tomorrow = datetime.now() + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")  # e.g. 2026-06-26

    marker = task["grade_params"]["expected_subject"].replace("{runid}", runid)

    def sub(text: str) -> str:
        """Substitute {runid} and {tomorrow} in text."""
        return text.replace("{runid}", runid).replace("{tomorrow}", tomorrow_str)

    do_prompt = sub(task["prompt"])
    read_prompt = sub(task.get("read_prompt", ""))
    delete_prompt = sub(task.get("delete_prompt", ""))
    grade_params = task["grade_params"]

    print("=" * 60)
    print("M365EVAL - Work IQ grader")
    print("=" * 60)
    print(f"  Task       : {task['id']}")
    print(f"  RUNID      : {runid}")
    print(f"  Marker     : {marker}")
    print(f"  Surface    : {task['surface']}")
    print(f"  Grader     : {args.grader}")
    print(f"  Sync delay : {args.sync_delay}s")
    print(f"  Dry-run    : {args.dry_run}")
    print(f"  Tomorrow   : {tomorrow_str}")
    print()

    api_key = os.environ.get("MCP_API_KEY", "")
    if not api_key:
        print("  BLOCKER: MCP_API_KEY not found in environment / .env")
        return 3

    # ---- Step 1: Preflight -------------------------------------------------
    print("[1/5] Preflight...")
    if args.grader == "workiq":
        err = preflight_workiq()
        if err:
            print(f"  BLOCKER: {err}")
            print("  The companion must be running at http://127.0.0.1:8011. Aborting.")
            return 2
        print("  Work IQ endpoint :8011 - OK")
    else:
        err = preflight_outlook()
        if err:
            print(f"  BLOCKER: {err}")
            print("  Outlook must be running and connected. Aborting.")
            return 2
        print("  Outlook COM - OK")
    print()

    # ---- Step 2: DO --------------------------------------------------------
    reply_text = "(dry-run - skipped)"
    if not args.dry_run:
        print("[2/5] DO: POSTing task prompt to companion at :8011...")
        print(f"  Prompt: {do_prompt}")
        print()
        try:
            response = post_to_companion(do_prompt, api_key, timeout=120)
            reply_text = extract_reply_text(response)
        except RuntimeError as exc:
            print(f"  ERROR: {exc}")
            return 4
        print(f"  Companion reply (DO): {reply_text!r}")
        print()
    else:
        print("[2/5] Dry-run: skipping DO POST.")
        print()

    # ---- Step 3: Wait for propagation --------------------------------------
    if not args.dry_run and args.sync_delay > 0:
        print(f"[3/5] Waiting {args.sync_delay}s for M365 propagation...")
        time.sleep(args.sync_delay)
        print("  Done waiting.")
    else:
        print("[3/5] Propagation wait: skipped.")
    print()

    # ---- Step 4: GRADE (independent) ---------------------------------------
    passed = False
    evidence = "FAIL: grading not reached"

    if args.grader == "workiq":
        print(f"[4/5] GRADE: POSTing independent Work IQ READ prompt for '{marker}'...")
        print(f"  Read prompt: {read_prompt}")
        print()
        try:
            grade_resp = post_to_companion(read_prompt, api_key, timeout=120)
            grade_reply = extract_reply_text(grade_resp)
        except RuntimeError as exc:
            print(f"  ERROR reading via Work IQ: {exc}")
            return 5
        print(f"  Work IQ read reply: {grade_reply!r}")
        print()
        passed, evidence = grade_workiq_calendar(
            read_reply=grade_reply,
            marker=marker,
            expected_date=tomorrow,
            expected_hour=grade_params["expected_hour"],
            expected_minute=grade_params["expected_minute"],
            start_tolerance_minutes=grade_params["start_tolerance_minutes"],
            expected_duration_minutes=grade_params["expected_duration_minutes"],
            duration_tolerance_minutes=grade_params["duration_tolerance_minutes"],
        )

    else:
        print(f"[4/5] GRADE: Reading Outlook COM calendar for '{marker}'...")
        try:
            events = read_calendar_for_marker_outlook(marker, search_days=3)
        except RuntimeError as exc:
            print(f"  ERROR reading calendar: {exc}")
            return 5
        print(f"  Found {len(events)} matching event(s) via COM.")
        passed, evidence = grade_outlook_calendar(events, grade_params, runid)

    print(evidence)
    print()

    # ---- Step 5: CLEANUP ---------------------------------------------------
    cleanup_ok = True
    if args.no_cleanup:
        print(f"[5/5] Cleanup SKIPPED (--no-cleanup).")
        print(f"  Manual cleanup needed: delete '{marker}' on {tomorrow_str}")
        cleanup_ok = False
    else:
        if args.grader == "workiq":
            if delete_prompt:
                print(f"[5/5] CLEANUP: POSTing Work IQ DELETE prompt for '{marker}'...")
                print(f"  Delete prompt: {delete_prompt}")
                print()
                try:
                    del_resp = post_to_companion(delete_prompt, api_key, timeout=120)
                    del_reply = extract_reply_text(del_resp)
                    print(f"  Companion reply (DELETE): {del_reply!r}")
                except RuntimeError as exc:
                    print(f"  WARNING: DELETE prompt failed: {exc}")
                    del_reply = "(error)"

                # Verify deletion via independent re-read
                print()
                print(f"  Verifying cleanup via independent re-read...")
                time.sleep(10)  # short wait for delete propagation
                try:
                    verify_resp = post_to_companion(read_prompt, api_key, timeout=120)
                    verify_reply = extract_reply_text(verify_resp)
                    print(f"  Verify read reply: {verify_reply!r}")
                except RuntimeError as exc:
                    print(f"  WARNING: Verify read failed: {exc}")
                    verify_reply = ""

                # Check cleanup by parsing structured events — a bare substring match
                # is NOT sufficient: the agent may mention the marker in a deletion-
                # confirmation sentence (e.g. "X is no longer in the list").
                verify_events = parse_workiq_calendar_lines(verify_reply)
                marker_still_present = any(e["title"] == marker for e in verify_events)
                if marker_still_present:
                    print(f"  WARNING: Marker '{marker}' still found as a calendar event in re-read!")
                    print(f"  MANUAL CLEANUP NEEDED: delete '{marker}' on {tomorrow_str}")
                    cleanup_ok = False
                else:
                    print(f"  Cleanup confirmed: marker NOT present as an event in re-read. OK.")
                    cleanup_ok = True
            else:
                print(f"[5/5] CLEANUP: No delete_prompt defined for this task.")
                print(f"  Manual cleanup needed: delete '{marker}' on {tomorrow_str}")
                cleanup_ok = False
        else:
            # Outlook COM cleanup
            print(f"[5/5] CLEANUP: Deleting marker event via Outlook COM...")
            cleanup_msg = delete_marker_outlook(marker)
            print(f"  {cleanup_msg}")
            if "No event" in cleanup_msg:
                print(f"  (If grading failed, the event may not exist. No action needed.)")
            cleanup_ok = "Deleted" in cleanup_msg or (not passed and "No event" in cleanup_msg)

    print()
    print("=" * 60)
    result_label = "PASS" if passed else "FAIL"
    print(f"RESULT: {result_label}")
    print(f"  Companion reply (DO) : {reply_text!r}")
    print(f"  Grade evidence       : {evidence.splitlines()[0]}")
    if not cleanup_ok:
        print(f"  *** CLEANUP INCOMPLETE - manual action required ***")
        print(f"  *** Delete '{marker}' on {tomorrow_str} ***")
    else:
        print(f"  Cleanup              : OK")
    print("=" * 60)

    return 0 if passed else 10


if __name__ == "__main__":
    sys.exit(main())
