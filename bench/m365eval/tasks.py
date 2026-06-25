"""
bench/m365eval/tasks.py
-----------------------
Task specs for the M365-native evaluation suite.

Each task dict contains:
  id            : unique string identifier
  surface       : M365 surface being tested
  prompt        : the "DO" instruction sent to the companion (creates / acts)
  read_prompt   : the "GRADE" instruction — an INDEPENDENT factual lookup that
                  returns structured "TITLE | START | END" lines for calendar,
                  or "FILENAME | CONTENT" for OneDrive. This is the ONLY source
                  of truth for PASS/FAIL — the agent's self-reported 'done' is
                  NOT trusted.
  delete_prompt : the "CLEANUP" instruction — deletes ONLY the exact marker.
                  Must include "Do NOT delete any other event/file."
  grade_params  : dict of parameters the grader uses to verify the task.

Use {runid} and {tomorrow} as placeholders — runner.py substitutes them.
{tomorrow} is replaced with the date string YYYY-MM-DD for the day after today.

SAFETY:
  * NEVER send email. No Work IQ Mail send prompts here ever.
  * DELETE prompts must name the exact marker and say "Do NOT delete any other".
  * All markers use the pattern M365EVAL-{runid} so they are uniquely identifiable
    and trivially distinguished from real calendar/file data.
"""

TASKS = [
    # ------------------------------------------------------------------
    # Task 1: Calendar — create a 30-minute event via Work IQ, grade
    #         via an independent Work IQ LIST, then delete.
    # PROVEN end-to-end: CREATE→CREATED, READ→found marker line, DELETE→DELETED.
    # ------------------------------------------------------------------
    {
        "id": "cal_create_30min",
        "surface": "work_iq_calendar",

        # DO prompt — proven pattern; agent must actually call the tool.
        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}', date {tomorrow}, start 14:00, end 14:30, "
            "no attendees, do not send any invitation. "
            "Actually call the tool to create it. "
            "Then reply with the created event's id or 'CREATED'."
        ),

        # READ/GRADE prompt — independent factual lookup; proven to faithfully
        # reflect real M365 state (correctly reports when an event is absent).
        "read_prompt": (
            "Using your Work IQ Calendar tool, list ALL my calendar events for {tomorrow}. "
            "For EACH output one line: TITLE | START | END "
            "(exact subject and times, format START as YYYY-MM-DD HH:MM). "
            "If there are none, say 'NO EVENTS'. "
            "Use the tool; do not guess."
        ),

        # DELETE prompt — targets the exact marker; safety clause included.
        "delete_prompt": (
            "Using your Work IQ Calendar tool, DELETE the calendar event titled exactly "
            "'M365EVAL-{runid}' on {tomorrow} 14:00-14:30. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}",
            # Event is scheduled for {tomorrow} at 14:00 local time.
            "expected_hour": 14,
            "expected_minute": 0,
            "start_tolerance_minutes": 5,
            "expected_duration_minutes": 30,
            "duration_tolerance_minutes": 5,
        },
    },

    # ------------------------------------------------------------------
    # Task 2: OneDrive — create a scratch file, read it back, delete it.
    # TODO: Work IQ OneDrive create/read/delete via :8011 not yet proven.
    #       Prompts are drafted below but the task is DISABLED (id prefixed
    #       with "todo_") so the runner skips it by default.
    #       To enable: rename id to "onedrive_create_read_delete" and run
    #       with --task onedrive_create_read_delete; validate end-to-end;
    #       then remove the "todo_" prefix here.
    # ------------------------------------------------------------------
    {
        "id": "todo_onedrive_create_read_delete",
        "surface": "work_iq_onedrive",

        "prompt": (
            "Using your Work IQ OneDrive tool, CREATE a text file named "
            "'M365EVAL-{runid}.txt' in my OneDrive root (or a 'scratch' subfolder "
            "if root creation is not allowed) with the content: 'hello-{runid}'. "
            "Actually call the tool to create the file. "
            "Reply 'CREATED' and the file path when done."
        ),

        "read_prompt": (
            "Using your Work IQ OneDrive tool, find the file named "
            "'M365EVAL-{runid}.txt' in my OneDrive. "
            "Output one line: FILENAME | CONTENT "
            "(exact filename and full text content of the file). "
            "If not found, say 'NOT FOUND'. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ OneDrive tool, DELETE the file named exactly "
            "'M365EVAL-{runid}.txt' from my OneDrive. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other file."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}.txt",
            # OneDrive task uses a different grader function (file content check).
            # grade_workiq_calendar will not be called for this surface.
            # Leaving calendar fields as sentinels; implement a file grader when enabling.
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },
]
