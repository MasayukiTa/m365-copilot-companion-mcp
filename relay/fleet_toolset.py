"""What a fleet worker may reach, enumerated from zero.

WHY THIS IS A LIST AND NOT A FILTER.

The gateway carries 167 tools. The first plan for containment was "move the execution tools
to the remote host" -- and an external review named that as the thing most likely to be
wrong, because it is subtraction: you move the few you thought of and the rest stay where
they were. Measured, the review was right. A classification of all 167 by name put these in
the harmless bucket:

    replace_in_file      mutates a file
    process_kill         kills any process on the machine
    run_in_background    spawns one
    verify_python        runs Python
    outlook_send_mail    sends mail as the operator
    clipboard_set        writes the operator's clipboard
    screenshot           captures their screen
    trash_path           deletes
    zip_extract          writes files anywhere it is pointed
    schedule_run_now     runs a scheduled task

None of those has "exec" or "shell" in its name. A denylist built by looking at names would
have shipped every one of them, and this repository already carries the rule that a
hand-written allowlist fails open when it is built by removing rather than by listing.

So: this is the whole set. A tool that is not written here is not reachable by a worker, and
adding one is a decision somebody makes on purpose, with a reason next to it.

WHAT A SWE-BENCH WORKER ACTUALLY DOES: read the repository, edit files in it, build it, run
its tests, and look at its own diff. Everything below serves one of those five. Nothing below
reaches outside the checkout, sends anything anywhere, or touches another process.
"""

# Each entry: tool -> why a worker needs it. The reason is not decoration; a tool nobody can
# justify in one line is a tool that should not be here.
FLEET_TOOLS = {
    # -- look at the checkout ------------------------------------------------------------
    "read_file":        "read a source file it is about to change",
    "list_directory":   "see what is in a directory",
    "glob":             "find files by pattern",
    "grep":             "find the code that matters in a repository it has never seen",
    "find_files":       "locate a file by name when grep is the wrong instrument",
    "read_json":        "many projects keep configuration it must respect in JSON",

    # -- change the checkout -------------------------------------------------------------
    "write_file":       "write the fix, which is the artefact the whole run exists to produce",
    "append_file":      "add to a file without rewriting it",
    "replace_in_file":  "the ordinary shape of a small fix",
    "multi_edit":       "several edits to one file without three round trips",
    "create_directory": "a fix that adds a module needs somewhere to put it",
    "write_json":       "update a manifest the fix requires",

    # -- build it and run its tests ------------------------------------------------------
    "shell_exec":       "build and test commands are the task; this is the point of the run",
    "run_python":       "many of these projects are Python and their tests are run from it",

    # -- see its own work ----------------------------------------------------------------
    "git_status":       "what have I changed",
    "git_diff":         "the answer it is being asked for IS this diff",
}

# Named so a reader can see what was considered and refused, rather than guessing that it was
# forgotten. Absence is a decision; this records which decision.
DELIBERATELY_EXCLUDED = {
    "pip_install":       "installs into an environment; the checkout's own venv is the worker's business, and this tool reached the harness's",
    "pwsh_exec":         "a second execution path is a second thing to contain for no gain; shell_exec is enough",
    "pwsh_exec_file":    "a second execution path is a second thing to contain, for no gain",
    "run_python_in_background": "an unsupervised process outliving the turn is how seventeen test processes ran for fourteen hours",
    "delete_path":       "a fix does not need to delete; removing a file can be done through the shell inside the container, where the blast radius is the container",
    "trash_path":        "deleting is done through the shell inside the container, where the blast radius is the container",
    "process_kill":      "no benchmark task requires killing a process the worker did not start",
    "job_kill":          "no benchmark task requires killing work the worker did not start",
    "run_in_background": "an unsupervised process outliving the turn is the orphan problem again",
    "outlook_send_mail": "a benchmark worker has no business sending mail as the operator",
    "outlook_create_event": "a benchmark worker has no business writing the operator's calendar",
    "clipboard_get":     "reads whatever the operator last copied, which may be anything",
    "clipboard_set":     "writes the operator's clipboard, which nothing here needs",
    "screenshot":        "captures the operator's screen, including work unrelated to the run",
    "web_fetch":         "dependency downloads belong to the package manager inside the container, not to the worker",
    "web_search":        "the task ships with its own issue text; searching is how a worker finds someone else's answer",
    "web_search_news":   "the task ships with its own issue text; searching finds someone else's answer",
    "unlock":            "a worker must never be able to widen its own permissions",
    "gate_ask":          "a worker must not create the approval it would then be answering",
    "gate_poll":         "reading the operator's pending decisions is not part of fixing a bug",
    "stop_request":      "a worker must not be able to stop the run it is part of",
    "stop_clear":        "and least of all clear a stop that somebody else set",
    "schedule_create":   "persistence beyond the run is not part of solving a bug",
    "schedule_run_now":  "running a scheduled task reaches work that has nothing to do with the instance",
    "git_commit":        "the capture step reads the working tree; a commit hides the change from it",
    "git_checkout":      "switching away from the base commit invalidates the whole instance",
    "git_add":           "staging is not needed: capture uses `git diff HEAD`, which sees staged and unstaged alike",
    "odbc_query":        "the operator's databases are not part of any instance",
    "sqlite_query":      "the operator's databases are not part of any benchmark instance",
    "forge_tool":        "a worker that can create tools is not bounded by a list of tools",
    "skill_request_approval": "creates an approval the worker would then be answering",
    "zip_extract":       "writes wherever it is pointed; the container's own shell is the bounded way to unpack",
    "notify_desktop":    "the operator's attention is not a resource the worker allocates",
    "verify_python":     "executes Python under a name that reads like a check",
    "python_check":      "executes Python under a name that reads like a static check",
}

# ---------------------------------------------------------------------------------------
# THE REST OF THE CATALOGUE, decided in groups rather than left unlisted.
#
# The test that demanded this is the point of the module: a tool that is neither allowed nor
# refused is safe (it is not allowed) and dishonest (nobody looked). Grouping is fine; silence
# is not.
# ---------------------------------------------------------------------------------------
_OFFICE = ("create_docx create_pptx docx_from_markdown docx_info pdf_info read_docx read_excel "
           "read_pdf write_excel pptx_add_image pptx_add_slide pptx_add_table pptx_export_png "
           "pptx_from_markdown pptx_info pptx_replace_image ocr_image ocr_pdf read_image "
           "image_info render_diagram render_math render_mermaid_png summarize_table "
           "sqlite_to_excel odbc_to_excel").split()
_DATA = ("odbc_columns odbc_connections odbc_drivers odbc_tables sqlite_schema sqlite_tables "
         "find_db_objects data_aliases_add data_aliases_list data_memory_status "
         "data_report_explain data_report_plan data_report_run verify_table_stat").split()
_MEMORY = ("agent_memory_list agent_memory_read agent_memory_save agent_memory_search "
           "memory_delete memory_list memory_load memory_save procedural_memory_delete "
           "procedural_memory_import_markdown procedural_memory_save procedural_memory_search "
           "semantic_memory_delete semantic_memory_list semantic_memory_load semantic_memory_save "
           "skill_list skill_load skill_match skill_read_resource").split()
_TURN = ("abort_turn claim_turn commit_turn heartbeat get_job_status read_job_context watcher_stop "
         "job_list job_output job_status job_wait watcher_events watcher_start "
         "runlog_append runlog_list runlog_read runlog_summarize toolcalls_tail "
         "todo_clear todo_list todo_write").split()
_HOST = ("env_info process_info process_list service_status registry_read dir_size "
         "file_metadata hash_file which shell_which list_my_tools list_unlocked stop_check "
         "gate_list schedule_delete schedule_info schedule_list forge_delete forge_list "
         "forge_read outlook_calendar outlook_inbox github_file").split()
_FS_EXTRA = ("copy_path move_path zip_create zip_list diff_files find_duplicates "
             "verify_file_contains verify_json_schema verify_numeric_close").split()
_GIT_EXTRA = "git_blame git_branch git_log".split()

for _n in _OFFICE:
    DELIBERATELY_EXCLUDED.setdefault(_n, "document, image and rendering work has nothing to do with fixing a bug in a checkout")
for _n in _DATA:
    DELIBERATELY_EXCLUDED.setdefault(_n, "the operator's databases and reporting stack are outside every benchmark instance")
for _n in _MEMORY:
    DELIBERATELY_EXCLUDED.setdefault(_n, "shared memory and skills are cross-run state; a worker writing there reaches other runs")
for _n in _TURN:
    DELIBERATELY_EXCLUDED.setdefault(_n, "the run's own bookkeeping belongs to the runner, not to the code being run")
for _n in _HOST:
    DELIBERATELY_EXCLUDED.setdefault(_n, "reads the host rather than the checkout, which is the boundary this set exists to draw")
for _n in _FS_EXTRA:
    DELIBERATELY_EXCLUDED.setdefault(_n, "filesystem work beyond the checkout can be done inside the container, where it is bounded")
for _n in _GIT_EXTRA:
    DELIBERATELY_EXCLUDED.setdefault(_n, "history is not needed to fix the bug at the base commit and invites reading other branches")


def is_allowed(name: str) -> bool:
    """The only question this module answers."""
    return name in FLEET_TOOLS


def unknown_tools(catalogue):
    """Tools in the catalogue that this file has neither allowed nor refused.

    THE POINT OF THE WHOLE MODULE. A new tool appears in the gateway and is, by default,
    invisible here -- neither allowed nor considered. That is fine for safety (it is not
    allowed) and bad for honesty (nobody decided). This is what a test asserts on, so the
    set stays a decision rather than a leftover.
    """
    known = set(FLEET_TOOLS) | set(DELIBERATELY_EXCLUDED)
    return sorted(set(catalogue) - known)
