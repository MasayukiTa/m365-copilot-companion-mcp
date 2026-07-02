M365EVAL — Work IQ grader
==========================

What this measures
------------------
The companion's ability to execute REAL M365 tasks via its Work IQ MCP connectors
(Calendar, OneDrive, etc.), verified INDEPENDENTLY through a second Work IQ read
that is NOT the agent's self-reported reply.

Proven closed loop (calendar):
  Runner                          :8011 companion (Work IQ agent)
  ------                          ----------------------------------
  DO prompt          ---------->  calls Work IQ Calendar → creates event in M365 cloud
  wait 25s
  GRADE read prompt  ---------->  calls Work IQ Calendar → lists events for tomorrow
  parse TITLE|START|END lines     PASS iff marker found with correct time/duration
  DELETE prompt      ---------->  calls Work IQ Calendar → deletes the exact marker
  verify re-read     ---------->  confirms marker GONE

Key design decision: the agent's self-reported "CREATED"/"done" is NEVER used as
the PASS criterion. Only the independent GRADE read counts.  This was proven correct:
the read correctly reported an event as ABSENT when the agent had falsely claimed
'done' — so it is a trustworthy grader.

Graders
-------
  workiq  (DEFAULT) — grade via independent Work IQ LIST prompt sent to :8011.
                      No local Outlook required. Proven end-to-end.
  outlook (legacy)  -- grade via local Outlook COM (pywin32 + Outlook desktop
                       running and connected). Available via --grader outlook.

Endpoint
--------
  http://127.0.0.1:8011/v1/chat/completions
  Auth: Authorization: Bearer <MCP_API_KEY>  (read from .env at runtime)
  Model: m365-copilot-opus
  Each POST drives the Copilot Studio agent with Work IQ connectors.
  Calls take ~30-40s each.

Files
-----
  tasks.py   — task specs: {id, surface, prompt, read_prompt, delete_prompt, grade_params}
  runner.py  — the eval loop: preflight → DO → wait → GRADE → CLEANUP → report
  README.txt — this file

Prerequisites
-------------
1. The companion server must be running at http://127.0.0.1:8011.
2. MCP_API_KEY must be set in .env (never put in code or passed on command line).
3. No local Outlook or pywin32 required for the default --grader workiq.

How to run
----------
From the repo root:

  .venv\Scripts\python.exe bench\m365eval\runner.py --runid TEST20260625

Or as a module:

  .venv\Scripts\python.exe -m bench.m365eval.runner --runid TEST20260625

With a custom sync delay:

  .venv\Scripts\python.exe bench\m365eval\runner.py --runid RUN01 --sync-delay 35

Using the legacy Outlook COM grader:

  .venv\Scripts\python.exe bench\m365eval\runner.py --runid RUN01 --grader outlook

Options:
  --runid TEXT          Unique ID baked into every test artifact title (REQUIRED).
                        Also reads M365EVAL_RUNID environment variable.
  --task ID             Task id (default: cal_create_30min). Available: cal_create_30min
  --grader workiq|outlook  Grader backend (default: workiq).
  --sync-delay N        Seconds to wait for M365 propagation between DO and GRADE (default: 25).
  --dry-run             Skip the DO POST; grade whatever is in M365 now.
  --no-cleanup          Skip deletion of the test artifact (leaves it for inspection).

Exit codes
----------
  0  = PASS
  1  = Missing --runid argument
  2  = Preflight failed (endpoint unreachable or Outlook not connected)
  3  = MCP_API_KEY missing from environment
  4  = Companion DO POST failed
  5  = Companion GRADE read failed
  10 = FAIL (independent read did not find the expected artifact)

Safety rules (hard-coded)
--------------------------
* NEVER sends email. No Work IQ Mail send prompts in any task.
* Every test artifact uses the unique marker 'M365EVAL-<RUNID>'.
* DELETE prompts explicitly say "Do NOT delete any other event/file."
* After DELETE, a re-read verifies the marker is GONE before declaring cleanup OK.
* If cleanup verification fails, the runner prints "MANUAL CLEANUP NEEDED" with
  the exact marker name and date so the user can act.
* MCP_API_KEY is read from .env at runtime; never printed, logged, or committed.
* Events are scheduled for tomorrow (14:00-14:30) to avoid clutter on today's calendar.

Tasks
-----
  cal_create_30min             — Calendar: create 30min event → grade → delete. (ACTIVE, proven)
  todo_onedrive_create_read_delete — OneDrive: create file → read back → delete.
                                     (DISABLED — prefix "todo_" means runner skips by default;
                                      enable when Work IQ OneDrive path is proven.)

Adding tasks
------------
Add entries to tasks.py TASKS list. Each needs:
  id, surface, prompt, read_prompt, delete_prompt, grade_params.
The runner substitutes {runid} and {tomorrow} in all prompt fields and grade_params strings.
