"""
bench/m365eval/tasks.py
-----------------------
Task specs for the M365-native evaluation suite.

Each task dict contains:
  id            : unique string identifier
  surface       : M365 surface being tested
  grade_type    : "calendar" | "email_draft" | "email_draft_body" | "onedrive_file"
                  Selects the grading function in runner.py.
                  Defaults to "calendar" when absent (backward compat).
  prompt        : the "DO" instruction sent to the companion (creates / acts)
  read_prompt   : the "GRADE" instruction — an INDEPENDENT factual lookup.
                  For calendar: returns "TITLE | START | END" lines.
                  For email_draft: returns "FOUND | <subject> | To=<...>" or
                  "NOT_FOUND". This is the ONLY source of truth for PASS/FAIL —
                  the agent's self-reported 'done' is NOT trusted.
  delete_prompt : the "CLEANUP" instruction — deletes ONLY the exact marker.
                  Must include "Do NOT delete any other event/file/draft."
  grade_params  : dict of parameters the grader uses to verify the task.

Use {runid} and {tomorrow} as placeholders — runner.py substitutes them.
{tomorrow} is replaced with the date string YYYY-MM-DD for the day after today.
{day_after_tomorrow} is replaced with YYYY-MM-DD two days from now.

SAFETY:
  * NEVER send email. No Work IQ Mail send prompts here ever.
  * DELETE prompts must name the exact marker and say "Do NOT delete any other".
  * All markers use the pattern M365EVAL-{runid} so they are uniquely identifiable
    and trivially distinguished from real calendar/file data.
  * Email-draft tasks MUST leave To/CC/BCC completely empty. The DO prompt must
    explicitly forbid sending and leave the recipient field empty.
"""

TASKS = [
    # ------------------------------------------------------------------
    # Task C1: Calendar BASIC — create a 30-minute event
    # PROVEN end-to-end: CREATE→CREATED, READ→found marker line, DELETE→DELETED.
    # ------------------------------------------------------------------
    {
        "id": "cal_create_30min",
        "surface": "work_iq_calendar",
        "grade_type": "calendar",
        "description": "Calendar: create 30-min event at 10:00",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}-C1', date {day_after_tomorrow}, "
            "start 10:00, end 10:30, "
            "no attendees, do not send any invitation. "
            "Actually call the tool to create it. "
            "Then reply with the created event's id or 'CREATED'."
        ),

        "read_prompt": (
            "Using your Work IQ Calendar tool, list ALL my calendar events for {day_after_tomorrow}. "
            "For EACH output one line: TITLE | START | END "
            "(exact subject and times, format START as YYYY-MM-DD HH:MM). "
            "If there are none, say 'NO EVENTS'. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Calendar tool, DELETE the calendar event titled exactly "
            "'M365EVAL-{runid}-C1' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C1",
            "expected_hour": 10,
            "expected_minute": 0,
            "start_tolerance_minutes": 5,
            "expected_duration_minutes": 30,
            "duration_tolerance_minutes": 5,
        },
    },

    # ------------------------------------------------------------------
    # Task C2: Calendar PRECISION — create 60-min event at 15:00
    # Grade must check duration==60min AND start==15:00 exactly.
    # ------------------------------------------------------------------
    {
        "id": "cal_create_60min_precise",
        "surface": "work_iq_calendar",
        "grade_type": "calendar",
        "description": "Calendar: create 60-min event at 15:00 (precision check)",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}-C2', date {day_after_tomorrow}, "
            "start 15:00, end 16:00, "
            "no attendees, do not send any invitation. "
            "Actually call the tool to create it. "
            "Then reply 'CREATED'."
        ),

        "read_prompt": (
            "Using your Work IQ Calendar tool, list ALL my calendar events for {day_after_tomorrow}. "
            "For EACH output one line: TITLE | START | END "
            "(exact subject and times, format START as YYYY-MM-DD HH:MM). "
            "If there are none, say 'NO EVENTS'. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Calendar tool, DELETE the calendar event titled exactly "
            "'M365EVAL-{runid}-C2' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C2",
            # Precision: tighter tolerances
            "expected_hour": 15,
            "expected_minute": 0,
            "start_tolerance_minutes": 2,
            "expected_duration_minutes": 60,
            "duration_tolerance_minutes": 2,
        },
    },

    # ------------------------------------------------------------------
    # Task C3: Calendar BODY+LOCATION — create event with location field
    # Grade checks: title found, start correct, duration correct.
    # Body/location cannot be checked via the standard TITLE|START|END
    # read, so this is a precision start/duration test like C2 but on
    # a different time slot (09:00-09:45) to prove distinct slot handling.
    # ------------------------------------------------------------------
    {
        "id": "cal_create_with_location",
        "surface": "work_iq_calendar",
        "grade_type": "calendar",
        "description": "Calendar: create 45-min event at 09:00 with location (body+location fields)",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}-C3', date {day_after_tomorrow}, "
            "start 09:00, end 09:45, "
            "location 'M365EVAL-LOC-{runid}', "
            "body 'M365EVAL-BODY-{runid}: automated evaluation marker', "
            "no attendees, do not send any invitation. "
            "Actually call the tool to create it. "
            "Reply 'CREATED'."
        ),

        "read_prompt": (
            "Using your Work IQ Calendar tool, list ALL my calendar events for {day_after_tomorrow}. "
            "For EACH output one line: TITLE | START | END "
            "(exact subject and times, format START as YYYY-MM-DD HH:MM). "
            "If there are none, say 'NO EVENTS'. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Calendar tool, DELETE the calendar event titled exactly "
            "'M365EVAL-{runid}-C3' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C3",
            "expected_hour": 9,
            "expected_minute": 0,
            "start_tolerance_minutes": 2,
            "expected_duration_minutes": 45,
            "duration_tolerance_minutes": 2,
        },
    },

    # ------------------------------------------------------------------
    # Task M1: Mail BASIC DRAFT — save a draft (no recipient)
    # PROVEN end-to-end. Grade: independent Drafts search → FOUND.
    # ------------------------------------------------------------------
    {
        "id": "mail_draft_basic",
        "surface": "work_iq_mail",
        "grade_type": "email_draft",
        "description": "Mail: save basic draft (no recipient)",

        "prompt": (
            "Using your Work IQ Mail tool, create a DRAFT email and SAVE it as a draft. "
            "Do NOT send it. "
            "Leave the To/recipient field COMPLETELY EMPTY (no recipients at all). "
            "Subject: 'M365EVAL-{runid}-M1'. "
            "Body: 'eval draft body hello'. "
            "Just save the draft. "
            "Reply 'DRAFTED' and the draft id. "
            "Under no circumstances send any email."
        ),

        "read_prompt": (
            "Using your Work IQ Mail tool, search my DRAFTS folder for a draft whose "
            "subject is exactly 'M365EVAL-{runid}-M1'. "
            "If found, output exactly: FOUND | <subject> | To=<recipients or empty>. "
            "If not found, output exactly: NOT_FOUND. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Mail tool, DELETE the DRAFT whose subject is exactly "
            "'M365EVAL-{runid}-M1'. "
            "Delete ONLY that draft; do NOT delete or modify any other email or draft. "
            "Reply 'DELETED' when done."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-M1",
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },

    # ------------------------------------------------------------------
    # Task M2: Mail BODY PRECISION — draft with a specific body sentence
    # Grade: independent Drafts search, THEN a second read to check body
    # contains the exact sentence.
    # ------------------------------------------------------------------
    {
        "id": "mail_draft_body_check",
        "surface": "work_iq_mail",
        "grade_type": "email_draft_body",
        "description": "Mail: draft with exact body sentence (content precision)",

        "prompt": (
            "Using your Work IQ Mail tool, create a DRAFT email and SAVE it as a draft. "
            "Do NOT send it. "
            "Leave the To/recipient field COMPLETELY EMPTY (no recipients at all). "
            "Subject: 'M365EVAL-{runid}-M2'. "
            "Body must contain this exact sentence: "
            "'The quick brown fox jumps over the lazy dog M365EVAL-{runid}'. "
            "Just save the draft. "
            "Reply 'DRAFTED'. "
            "Under no circumstances send any email."
        ),

        "read_prompt": (
            "Using your Work IQ Mail tool, search my DRAFTS folder for a draft whose "
            "subject is exactly 'M365EVAL-{runid}-M2'. "
            "If found, output: FOUND | <subject> | BODY: <full body text of the draft>. "
            "If not found, output exactly: NOT_FOUND. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Mail tool, DELETE the DRAFT whose subject is exactly "
            "'M365EVAL-{runid}-M2'. "
            "Delete ONLY that draft; do NOT delete or modify any other email or draft. "
            "Reply 'DELETED' when done."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-M2",
            # The exact sentence that must appear in the draft body.
            "required_body_sentence": "The quick brown fox jumps over the lazy dog M365EVAL-{runid}",
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },

    # ------------------------------------------------------------------
    # Task OD1: OneDrive FILE CREATE+READ — probe whether Work IQ supports
    # OneDrive file create/read/delete via :8011. If any step fails, task
    # is marked 'unsupported' and excluded from the score denominator.
    # ------------------------------------------------------------------
    {
        "id": "onedrive_file_create",
        "surface": "work_iq_onedrive",
        "grade_type": "onedrive_file",
        "description": "OneDrive: create text file with specific content, verify content",

        "prompt": (
            "Using your Work IQ OneDrive tool, CREATE a text file named "
            "'M365EVAL-{runid}-OD1.txt' in my OneDrive root folder "
            "with the EXACT content: 'm365eval-onedrive-{runid}-OD1'. "
            "Actually call the Work IQ OneDrive tool to create the file. "
            "Reply 'CREATED' and the file path when done."
        ),

        "read_prompt": (
            "Using your Work IQ OneDrive tool, find the file named "
            "'M365EVAL-{runid}-OD1.txt' in my OneDrive root folder. "
            "If found, output one line: FOUND | M365EVAL-{runid}-OD1.txt | CONTENT: <full file content>. "
            "If not found, output: NOT_FOUND. "
            "Use the Work IQ OneDrive tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ OneDrive tool, DELETE the file named exactly "
            "'M365EVAL-{runid}-OD1.txt' from my OneDrive root folder. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other file."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-OD1.txt",
            "expected_file_content": "m365eval-onedrive-{runid}-OD1",
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },

    # ------------------------------------------------------------------
    # Task OD2: OneDrive SECOND FILE — different content, different name
    # Depends on OD1 working. If OD1 is unsupported, this is too.
    # ------------------------------------------------------------------
    {
        "id": "onedrive_file_create2",
        "surface": "work_iq_onedrive",
        "grade_type": "onedrive_file",
        "description": "OneDrive: create 2nd text file with different content",

        "prompt": (
            "Using your Work IQ OneDrive tool, CREATE a text file named "
            "'M365EVAL-{runid}-OD2.txt' in my OneDrive root folder "
            "with the EXACT content: 'onedrive-marker-{runid}-second'. "
            "Actually call the Work IQ OneDrive tool to create the file. "
            "Reply 'CREATED' and the file path when done."
        ),

        "read_prompt": (
            "Using your Work IQ OneDrive tool, find the file named "
            "'M365EVAL-{runid}-OD2.txt' in my OneDrive root folder. "
            "If found, output one line: FOUND | M365EVAL-{runid}-OD2.txt | CONTENT: <full file content>. "
            "If not found, output: NOT_FOUND. "
            "Use the Work IQ OneDrive tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ OneDrive tool, DELETE the file named exactly "
            "'M365EVAL-{runid}-OD2.txt' from my OneDrive root folder. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other file."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-OD2.txt",
            "expected_file_content": "onedrive-marker-{runid}-second",
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },

    # ------------------------------------------------------------------
    # Task MULTI: Multi-constraint — create calendar event with all three
    # constraints checked: exact start (11:00), exact duration (90 min),
    # AND a mail draft saved in the same "turn" (two actions in one prompt).
    # Grade: calendar event FOUND at 11:00 with 90-min duration.
    # The mail draft portion graded separately in the multi_constraint_mail task.
    # ------------------------------------------------------------------
    {
        "id": "cal_multi_constraint",
        "surface": "work_iq_calendar",
        "grade_type": "calendar",
        "description": "Calendar: multi-constraint 90-min event at 11:00 (harder precision)",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}-MULTI', date {day_after_tomorrow}, "
            "start 11:00, end 12:30, "
            "location 'Conference Room A', "
            "body 'Multi-constraint eval: M365EVAL-{runid}', "
            "no attendees, do not send any invitation. "
            "Actually call the tool to create it. "
            "Reply 'CREATED'."
        ),

        "read_prompt": (
            "Using your Work IQ Calendar tool, list ALL my calendar events for {day_after_tomorrow}. "
            "For EACH output one line: TITLE | START | END "
            "(exact subject and times, format START as YYYY-MM-DD HH:MM). "
            "If there are none, say 'NO EVENTS'. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Calendar tool, DELETE the calendar event titled exactly "
            "'M365EVAL-{runid}-MULTI' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-MULTI",
            "expected_hour": 11,
            "expected_minute": 0,
            "start_tolerance_minutes": 2,
            "expected_duration_minutes": 90,
            "duration_tolerance_minutes": 2,
        },
    },
]
