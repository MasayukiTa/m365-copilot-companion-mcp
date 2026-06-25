"""
bench/m365eval/tasks.py
-----------------------
Task specs for the M365-native evaluation suite.

Each task dict contains:
  id            : unique string identifier
  surface       : M365 surface being tested
  grade_type    : "calendar" | "email_draft" | "email_draft_body" | "onedrive_file"
                  | "calendar_allday" | "email_draft_special_subject"
                  | "email_draft_long_body" | "multi_step_cal_draft"
                  Selects the grading function in runner.py / suite_runner.py.
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

    # ==================== NEW HARDER TASKS START HERE ====================

    # ------------------------------------------------------------------
    # Task C4: Calendar EXACT-MINUTE START — 09:07 (non-round time)
    # Tests whether the companion passes non-round minute values correctly.
    # Tolerance: 1 minute (tight).
    # ------------------------------------------------------------------
    {
        "id": "cal_exact_minute_start",
        "surface": "work_iq_calendar",
        "grade_type": "calendar",
        "description": "Calendar: create 20-min event at exact 09:07 (non-round minute precision)",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}-C4', date {day_after_tomorrow}, "
            "start 09:07, end 09:27, "
            "no attendees, do not send any invitation. "
            "The start time must be exactly 09:07 (not 09:00 or 09:10). "
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
            "'M365EVAL-{runid}-C4' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C4",
            "expected_hour": 9,
            "expected_minute": 7,
            "start_tolerance_minutes": 1,
            "expected_duration_minutes": 20,
            "duration_tolerance_minutes": 2,
        },
    },

    # ------------------------------------------------------------------
    # Task C5: Calendar ALL-DAY EVENT
    # Creates an all-day event. Grade: title found; start tolerance is large
    # (all-day events may report midnight or no time).
    # Uses grade_type "calendar_allday" — suite_runner checks only title present.
    # ------------------------------------------------------------------
    {
        "id": "cal_allday_event",
        "surface": "work_iq_calendar",
        "grade_type": "calendar_allday",
        "description": "Calendar: create an all-day event (no start/end time)",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new all-day calendar event: "
            "subject 'M365EVAL-{runid}-C5', date {day_after_tomorrow}, "
            "mark it as an all-day event (no specific start/end time, just the date). "
            "No attendees, do not send any invitation. "
            "Actually call the tool to create it. "
            "Reply 'CREATED'."
        ),

        "read_prompt": (
            "Using your Work IQ Calendar tool, list ALL my calendar events for {day_after_tomorrow}, "
            "including all-day events. "
            "For EACH output one line: TITLE | START | END "
            "(format START as YYYY-MM-DD HH:MM or YYYY-MM-DD for all-day). "
            "If there are none, say 'NO EVENTS'. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Calendar tool, DELETE the calendar event titled exactly "
            "'M365EVAL-{runid}-C5' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C5",
            # For all-day events, we only check the title is present (graded by calendar_allday handler)
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 1440,  # 24h tolerance: all-day can land at any hour
            "expected_duration_minutes": 1440,
            "duration_tolerance_minutes": 1440,
        },
    },

    # ------------------------------------------------------------------
    # Task C6: Calendar LATE-NIGHT SPANNING EVENT (23:30 - 00:30 next day)
    # Tests whether the companion correctly handles an event that crosses midnight.
    # Grade: title present on {day_after_tomorrow}, start at 23:30 (+/-2min).
    # ------------------------------------------------------------------
    {
        "id": "cal_midnight_span",
        "surface": "work_iq_calendar",
        "grade_type": "calendar",
        "description": "Calendar: 60-min event at 23:30 spanning midnight (23:30-00:30)",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}-C6', date {day_after_tomorrow}, "
            "start 23:30, end 00:30 the next day (i.e. the event goes from 23:30 on "
            "{day_after_tomorrow} to 00:30 on the following day), "
            "no attendees, do not send any invitation. "
            "The event spans midnight. "
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
            "'M365EVAL-{runid}-C6' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C6",
            "expected_hour": 23,
            "expected_minute": 30,
            "start_tolerance_minutes": 2,
            "expected_duration_minutes": 60,
            "duration_tolerance_minutes": 5,
        },
    },

    # ------------------------------------------------------------------
    # Task C7: Calendar SHORT EVENT (15 minutes) — tests minimum granularity
    # ------------------------------------------------------------------
    {
        "id": "cal_short_15min",
        "surface": "work_iq_calendar",
        "grade_type": "calendar",
        "description": "Calendar: create 15-min event at 14:00 (short duration edge case)",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}-C7', date {day_after_tomorrow}, "
            "start 14:00, end 14:15, "
            "no attendees, do not send any invitation. "
            "The event is only 15 minutes long. "
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
            "'M365EVAL-{runid}-C7' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C7",
            "expected_hour": 14,
            "expected_minute": 0,
            "start_tolerance_minutes": 2,
            "expected_duration_minutes": 15,
            "duration_tolerance_minutes": 2,
        },
    },

    # ------------------------------------------------------------------
    # Task M3: Mail SPECIAL SUBJECT — subject contains special characters
    # Grade: independent Drafts search; exact subject must round-trip.
    # Special chars: colons, slashes, ampersand, parens, hash.
    # ------------------------------------------------------------------
    {
        "id": "mail_draft_special_subject",
        "surface": "work_iq_mail",
        "grade_type": "email_draft_special_subject",
        "description": "Mail: draft with special-character subject (A/B & C (test) #1)",

        "prompt": (
            "Using your Work IQ Mail tool, create a DRAFT email and SAVE it as a draft. "
            "Do NOT send it. "
            "Leave the To/recipient field COMPLETELY EMPTY (no recipients at all). "
            "Subject must be EXACTLY: 'M365EVAL-{runid}: A/B & C (test) #1'. "
            "Body: 'special subject test'. "
            "Just save the draft. "
            "Reply 'DRAFTED'. "
            "Under no circumstances send any email."
        ),

        "read_prompt": (
            "Using your Work IQ Mail tool, search my DRAFTS folder for a draft whose "
            "subject contains 'M365EVAL-{runid}'. "
            "If found, output one line: FOUND | <exact subject> | To=<recipients or empty>. "
            "If not found, output exactly: NOT_FOUND. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Mail tool, DELETE the DRAFT whose subject contains "
            "'M365EVAL-{runid}: A/B & C (test) #1'. "
            "Delete ONLY that draft; do NOT delete or modify any other email or draft. "
            "Reply 'DELETED' when done."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}: A/B & C (test) #1",
            # grade_type=email_draft_special_subject checks that the exact special-char subject
            # appears verbatim in the FOUND line
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },

    # ------------------------------------------------------------------
    # Task M4: Mail LONG BODY (200+ chars verbatim)
    # Grade: body must contain the exact 220-char string verbatim.
    # Tests whether the companion truncates or corrupts long body content.
    # ------------------------------------------------------------------
    {
        "id": "mail_draft_long_body",
        "surface": "work_iq_mail",
        "grade_type": "email_draft_body",
        "description": "Mail: draft with 220-char verbatim body string (truncation test)",

        "prompt": (
            "Using your Work IQ Mail tool, create a DRAFT email and SAVE it as a draft. "
            "Do NOT send it. "
            "Leave the To/recipient field COMPLETELY EMPTY (no recipients at all). "
            "Subject: 'M365EVAL-{runid}-M4'. "
            "The body must contain this EXACT string verbatim (do not shorten or paraphrase): "
            "'M365EVAL-LONGBODY-{runid}: Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel "
            "India Juliet Kilo Lima Mike November Oscar Papa Quebec Romeo Sierra Tango Uniform "
            "Victor Whiskey Xray Yankee Zulu 0123456789 END'. "
            "Just save the draft. "
            "Reply 'DRAFTED'. "
            "Under no circumstances send any email."
        ),

        "read_prompt": (
            "Using your Work IQ Mail tool, search my DRAFTS folder for a draft whose "
            "subject is exactly 'M365EVAL-{runid}-M4'. "
            "If found, output: FOUND | <subject> | BODY: <full body text of the draft>. "
            "If not found, output exactly: NOT_FOUND. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Mail tool, DELETE the DRAFT whose subject is exactly "
            "'M365EVAL-{runid}-M4'. "
            "Delete ONLY that draft; do NOT delete or modify any other email or draft. "
            "Reply 'DELETED' when done."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-M4",
            "required_body_sentence": (
                "M365EVAL-LONGBODY-{runid}: Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel "
                "India Juliet Kilo Lima Mike November Oscar Papa Quebec Romeo Sierra Tango Uniform "
                "Victor Whiskey Xray Yankee Zulu 0123456789 END"
            ),
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },

    # ------------------------------------------------------------------
    # Task M5: Mail MULTI-LINE BODY — body with explicit line breaks
    # Grade: body must contain two specific lines verbatim (both checked).
    # Tests multi-line preservation.
    # ------------------------------------------------------------------
    {
        "id": "mail_draft_multiline_body",
        "surface": "work_iq_mail",
        "grade_type": "email_draft_body",
        "description": "Mail: draft with multi-line body (line 1 and line 2 both checked)",

        "prompt": (
            "Using your Work IQ Mail tool, create a DRAFT email and SAVE it as a draft. "
            "Do NOT send it. "
            "Leave the To/recipient field COMPLETELY EMPTY (no recipients at all). "
            "Subject: 'M365EVAL-{runid}-M5'. "
            "The body must contain these two lines exactly as shown:\n"
            "Line 1: 'M365EVAL-LINE1-{runid}: first line of multiline test'\n"
            "Line 2: 'M365EVAL-LINE2-{runid}: second line of multiline test'\n"
            "Include both lines in the body. "
            "Just save the draft. "
            "Reply 'DRAFTED'. "
            "Under no circumstances send any email."
        ),

        "read_prompt": (
            "Using your Work IQ Mail tool, search my DRAFTS folder for a draft whose "
            "subject is exactly 'M365EVAL-{runid}-M5'. "
            "If found, output: FOUND | <subject> | BODY: <full body text of the draft>. "
            "If not found, output exactly: NOT_FOUND. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Mail tool, DELETE the DRAFT whose subject is exactly "
            "'M365EVAL-{runid}-M5'. "
            "Delete ONLY that draft; do NOT delete or modify any other email or draft. "
            "Reply 'DELETED' when done."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-M5",
            # Check for line 1 (line 2 checked separately in grade logic via required_body_sentence)
            "required_body_sentence": "M365EVAL-LINE1-{runid}: first line of multiline test",
            "required_body_sentence_2": "M365EVAL-LINE2-{runid}: second line of multiline test",
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },

    # ------------------------------------------------------------------
    # Task XARTIFACT1: Multi-step cross-artifact — CREATE calendar event C8
    # AND a draft whose body references the event's exact title.
    # This is task XARTIFACT1 (Calendar side): grade the calendar event.
    # ------------------------------------------------------------------
    {
        "id": "cross_artifact_cal",
        "surface": "work_iq_calendar",
        "grade_type": "calendar",
        "description": "Cross-artifact: create calendar event C8 at 16:00 (paired with cross_artifact_mail)",

        "prompt": (
            "I need you to do TWO things in sequence using your Work IQ tools:\n"
            "1. Using your Work IQ Calendar tool, CREATE a calendar event: "
            "subject 'M365EVAL-{runid}-C8', date {day_after_tomorrow}, "
            "start 16:00, end 17:00, no attendees, do not send any invitation. "
            "Actually call the Calendar tool to create it.\n"
            "2. Using your Work IQ Mail tool, CREATE a DRAFT email (do NOT send it, "
            "leave To field EMPTY): "
            "subject 'M365EVAL-{runid}-XMAIL', "
            "body must contain the exact text: "
            "'This draft references calendar event M365EVAL-{runid}-C8 scheduled for {day_after_tomorrow}'. "
            "Actually call the Mail tool to save the draft.\n"
            "Reply 'DONE: CREATED event M365EVAL-{runid}-C8 and saved draft M365EVAL-{runid}-XMAIL'."
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
            "'M365EVAL-{runid}-C8' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C8",
            "expected_hour": 16,
            "expected_minute": 0,
            "start_tolerance_minutes": 2,
            "expected_duration_minutes": 60,
            "duration_tolerance_minutes": 2,
        },
    },

    # ------------------------------------------------------------------
    # Task XARTIFACT2: Multi-step cross-artifact — DRAFT side
    # Grade: the draft saved in the cross_artifact_cal DO step.
    # The draft body must reference the calendar event title exactly.
    # This uses a separate DO step that re-saves the draft if not already present.
    # ------------------------------------------------------------------
    {
        "id": "cross_artifact_mail",
        "surface": "work_iq_mail",
        "grade_type": "email_draft_body",
        "description": "Cross-artifact: grade the draft whose body references event title M365EVAL-{runid}-C8",

        "prompt": (
            "Using your Work IQ Mail tool, create a DRAFT email and SAVE it as a draft. "
            "Do NOT send it. "
            "Leave the To/recipient field COMPLETELY EMPTY (no recipients at all). "
            "Subject: 'M365EVAL-{runid}-XMAIL'. "
            "Body must contain the exact text: "
            "'This draft references calendar event M365EVAL-{runid}-C8 scheduled for {day_after_tomorrow}'. "
            "Just save the draft. Reply 'DRAFTED'. "
            "Under no circumstances send any email."
        ),

        "read_prompt": (
            "Using your Work IQ Mail tool, search my DRAFTS folder for a draft whose "
            "subject is exactly 'M365EVAL-{runid}-XMAIL'. "
            "If found, output: FOUND | <subject> | BODY: <full body text of the draft>. "
            "If not found, output exactly: NOT_FOUND. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Mail tool, DELETE the DRAFT whose subject is exactly "
            "'M365EVAL-{runid}-XMAIL'. "
            "Delete ONLY that draft; do NOT delete or modify any other email or draft. "
            "Reply 'DELETED' when done."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-XMAIL",
            "required_body_sentence": (
                "This draft references calendar event M365EVAL-{runid}-C8 "
                "scheduled for {day_after_tomorrow}"
            ),
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },

    # ------------------------------------------------------------------
    # Task C9: Calendar READ PROBE — find the event planted by C1 task.
    # This tests the companion's ability to identify a specific event
    # among potentially many events on the same day.
    # NOTE: This task requires C1 to have been cleaned up, so we plant
    # a fresh reference event in its DO step, then grade that we can
    # list the correct event at the correct time.
    # ------------------------------------------------------------------
    {
        "id": "cal_read_probe",
        "surface": "work_iq_calendar",
        "grade_type": "calendar",
        "description": "Calendar: plant event C9 then confirm it appears at 13:45 in a list query",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}-C9', date {day_after_tomorrow}, "
            "start 13:45, end 14:45, "
            "no attendees, do not send any invitation. "
            "Actually call the tool to create it. "
            "Reply 'CREATED'."
        ),

        "read_prompt": (
            "Using your Work IQ Calendar tool, list ALL my calendar events for {day_after_tomorrow}. "
            "For EACH output one line: TITLE | START | END "
            "(exact subject and times, format START as YYYY-MM-DD HH:MM). "
            "If there are none, say 'NO EVENTS'. "
            "Use the tool; do not guess. "
            "I need to confirm that event 'M365EVAL-{runid}-C9' is listed with start time 13:45."
        ),

        "delete_prompt": (
            "Using your Work IQ Calendar tool, DELETE the calendar event titled exactly "
            "'M365EVAL-{runid}-C9' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C9",
            "expected_hour": 13,
            "expected_minute": 45,
            "start_tolerance_minutes": 2,
            "expected_duration_minutes": 60,
            "duration_tolerance_minutes": 2,
        },
    },

    # ------------------------------------------------------------------
    # Task OD3: OneDrive MULTI-LINE CONTENT — file with multi-line content
    # Grade: read back the file and check specific line is present verbatim.
    # ------------------------------------------------------------------
    {
        "id": "onedrive_multiline_content",
        "surface": "work_iq_onedrive",
        "grade_type": "onedrive_file",
        "description": "OneDrive: create file with multi-line content, verify specific line",

        "prompt": (
            "Using your Work IQ OneDrive tool, CREATE a text file named "
            "'M365EVAL-{runid}-OD3.txt' in my OneDrive root folder "
            "with the following EXACT multi-line content:\n"
            "Line1: M365EVAL-{runid}-multiline-start\n"
            "Line2: alpha beta gamma delta\n"
            "Line3: M365EVAL-{runid}-multiline-end\n"
            "Actually call the Work IQ OneDrive tool to create the file. "
            "Reply 'CREATED' when done."
        ),

        "read_prompt": (
            "Using your Work IQ OneDrive tool, find the file named "
            "'M365EVAL-{runid}-OD3.txt' in my OneDrive root folder. "
            "If found, output one line: FOUND | M365EVAL-{runid}-OD3.txt | CONTENT: <full file content>. "
            "If not found, output: NOT_FOUND. "
            "Use the Work IQ OneDrive tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ OneDrive tool, DELETE the file named exactly "
            "'M365EVAL-{runid}-OD3.txt' from my OneDrive root folder. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other file."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-OD3.txt",
            # Check for start marker — if content round-trips, this marker will be present
            "expected_file_content": "M365EVAL-{runid}-multiline-start",
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },

    # ------------------------------------------------------------------
    # Task C10: Calendar TOMORROW (not day_after_tomorrow) — uses {tomorrow}
    # Tests that the companion can handle different date references.
    # ------------------------------------------------------------------
    {
        "id": "cal_tomorrow_slot",
        "surface": "work_iq_calendar",
        "grade_type": "calendar_tomorrow",
        "description": "Calendar: create event on TOMORROW (not day_after_tomorrow) at 08:30",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}-C10', date {tomorrow}, "
            "start 08:30, end 09:00, "
            "no attendees, do not send any invitation. "
            "Actually call the tool to create it. "
            "Reply 'CREATED'."
        ),

        "read_prompt": (
            "Using your Work IQ Calendar tool, list ALL my calendar events for {tomorrow}. "
            "For EACH output one line: TITLE | START | END "
            "(exact subject and times, format START as YYYY-MM-DD HH:MM). "
            "If there are none, say 'NO EVENTS'. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Calendar tool, DELETE the calendar event titled exactly "
            "'M365EVAL-{runid}-C10' on {tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C10",
            "expected_hour": 8,
            "expected_minute": 30,
            "start_tolerance_minutes": 2,
            "expected_duration_minutes": 30,
            "duration_tolerance_minutes": 2,
        },
    },

    # ------------------------------------------------------------------
    # Task M6: Mail DRAFT — save a second draft with different content
    # Tests idempotency / multiple draft creation in one session.
    # ------------------------------------------------------------------
    {
        "id": "mail_draft_second",
        "surface": "work_iq_mail",
        "grade_type": "email_draft",
        "description": "Mail: save a second distinct draft (idempotency / multiple drafts)",

        "prompt": (
            "Using your Work IQ Mail tool, create a DRAFT email and SAVE it as a draft. "
            "Do NOT send it. "
            "Leave the To/recipient field COMPLETELY EMPTY (no recipients at all). "
            "Subject: 'M365EVAL-{runid}-M6'. "
            "Body: 'second eval draft M365EVAL-{runid} distinct content'. "
            "Just save the draft. "
            "Reply 'DRAFTED'. "
            "Under no circumstances send any email."
        ),

        "read_prompt": (
            "Using your Work IQ Mail tool, search my DRAFTS folder for a draft whose "
            "subject is exactly 'M365EVAL-{runid}-M6'. "
            "If found, output exactly: FOUND | <subject> | To=<recipients or empty>. "
            "If not found, output exactly: NOT_FOUND. "
            "Use the tool; do not guess."
        ),

        "delete_prompt": (
            "Using your Work IQ Mail tool, DELETE the DRAFT whose subject is exactly "
            "'M365EVAL-{runid}-M6'. "
            "Delete ONLY that draft; do NOT delete or modify any other email or draft. "
            "Reply 'DELETED' when done."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-M6",
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },

    # ------------------------------------------------------------------
    # Task C11: Calendar 2-HOUR EVENT at 17:30 (evening slot precision)
    # ------------------------------------------------------------------
    {
        "id": "cal_evening_2hr",
        "surface": "work_iq_calendar",
        "grade_type": "calendar",
        "description": "Calendar: create 2-hour event at 17:30 (evening precision)",

        "prompt": (
            "Using your Work IQ Calendar tool, CREATE a new calendar event: "
            "subject 'M365EVAL-{runid}-C11', date {day_after_tomorrow}, "
            "start 17:30, end 19:30, "
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
            "'M365EVAL-{runid}-C11' on {day_after_tomorrow}. "
            "Actually call the tool to delete it. "
            "Reply 'DELETED' when done. "
            "Do NOT delete any other event."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-C11",
            "expected_hour": 17,
            "expected_minute": 30,
            "start_tolerance_minutes": 2,
            "expected_duration_minutes": 120,
            "duration_tolerance_minutes": 2,
        },
    },

    # ------------------------------------------------------------------
    # Task SP1: SharePoint READ PROBE — attempt to list a SharePoint document library.
    # If SharePoint Work IQ is not available, mark unsupported.
    # grade_type: "sharepoint_probe" — suite_runner checks for any useful response.
    # ------------------------------------------------------------------
    {
        "id": "sharepoint_probe",
        "surface": "work_iq_sharepoint",
        "grade_type": "sharepoint_probe",
        "description": "SharePoint: probe Work IQ SharePoint capability (read-only list)",

        "prompt": (
            "Using your Work IQ SharePoint tool (if available), list the files or documents "
            "in my SharePoint default document library. "
            "Output the result as: SHAREPOINT_FILES: <list of file names, or 'none found'>. "
            "If you do not have a Work IQ SharePoint tool, output: "
            "SHAREPOINT_UNSUPPORTED: no SharePoint tool available. "
            "Do not create, modify, or delete any file. "
            "Use the tool; do not guess."
        ),

        "read_prompt": (
            "Using your Work IQ SharePoint tool (if available), list the documents "
            "in my SharePoint default document library. "
            "If available, output: SHAREPOINT_FILES: <file list or 'none'>. "
            "If the tool is not available, output: SHAREPOINT_UNSUPPORTED. "
            "Do not create, modify, or delete any file."
        ),

        "delete_prompt": (
            "No deletion needed for the SharePoint probe task. Reply 'NO_CLEANUP_NEEDED'."
        ),

        "grade_params": {
            "expected_subject": "M365EVAL-{runid}-SP1-PROBE",
            # No actual artifact - this is a read-only probe
            "expected_hour": 0,
            "expected_minute": 0,
            "start_tolerance_minutes": 0,
            "expected_duration_minutes": 0,
            "duration_tolerance_minutes": 0,
        },
    },
]
