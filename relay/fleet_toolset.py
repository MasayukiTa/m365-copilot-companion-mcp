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

    # -- drive the desktop ---------------------------------------------------------------
    #
    # THESE DO NOT BELONG TO A SWE-BENCH WORKER AND ARE HERE ANYWAY, WHICH IS WORTH SAYING
    # PLAINLY. Everything above serves the five things a bench worker does; nothing below
    # does. They are here because this list is consulted for EVERY fleet run, not only a
    # bench one, and a fleet worker is the only caller that can exercise computer use at
    # all -- the operator's own calls do not go through an unattended run, and the library
    # can be driven directly from a script, which proves the executor and proves nothing
    # about the agent. Listing them elsewhere and refusing them here made the capability
    # untestable rather than making a bench run safer.
    #
    # The distinction that is actually wanted is by RUN KIND, which this file cannot
    # express today: a bench run should not reach these and an office-work run should.
    # Recorded here rather than solved here, because inventing a second list is how the
    # first one stopped being a decision.
    #
    # What still gates them is the unlock: all six call require_unlocked(), so a worker
    # that has not been given the password moves no mouse.
    "screen_look":      "see the screen, with the coordinate frame needed to act on what it sees",
    "screen_click":     "click what it saw, in that frame",
    "screen_scroll":    "reach what is below the fold",
    "screen_type":      "type into the focused field, as characters rather than keystrokes",
    "screen_press":     "the named keys ordinary office work needs, refusing the destructive combinations",
    "screen_windows":   "what is open, which is often enough to decide without a picture",
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
    "process_kill":      "reaches ANY process on the machine, including the server hosting this gate; ending a child of one's own build stays available through shell_exec",
    "job_kill":          "no benchmark task requires killing work the worker did not start",
    "run_in_background": "an unsupervised process outliving the turn is the orphan problem again",
    "outlook_send_mail": "a benchmark worker has no business sending mail as the operator",
    "outlook_create_event": "a benchmark worker has no business writing the operator's calendar",
    "clipboard_get":     "reads whatever the operator last copied, which may be anything",
    "clipboard_set":     "writes the operator's clipboard, which nothing here needs",
    "screenshot":        "captures the operator's screen, including work unrelated to the run",
    # NOTE: the six screen_* computer-use tools were listed here for a day and that was
    # wrong -- see the block above FLEET_TOOLS. The reason ("a benchmark worker has no
    # business driving the operator's desktop") is correct and this list cannot express it,
    # because it is consulted for EVERY fleet run and not only for a benchmark one. Refusing
    # them here did not protect a bench run; it made computer use unreachable by the only
    # caller that can exercise it, which is a fleet worker.
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

    # FOUND 2026-09-14, by running the undecided-tools guard against the real registry for the
    # first time. The guard had been reading a gitignored dump, so it skipped in CI and, here,
    # compared against a snapshot taken by hand on 2026-08-30. Eleven tools had appeared since
    # and were neither allowed nor refused. Recording them as excluded does not change what the
    # gate does -- unlisted was already refused -- it changes whether anybody looked.
    "restore_point":     "establishes a way back before editing; the capture step reads `git diff HEAD`, so a worker able to roll the tree back can erase the artefact the run exists to produce -- same reason as git_checkout",
    "roll_back":         "returns the tree to a restore_point, which is exactly the diff the run is being graded on",
    "fleet_submit":      "queues work for this machine's fleet: a worker that can enqueue runs is not bounded by the run it is in -- same family as stop_request and schedule_create",
    "fleet_queue":       "reads what else the operator has running, which is not part of fixing the instance",
    "loop_until_verified": "drives an edit/verify loop of its own inside one turn; a worker that can start an unbounded agent loop is not bounded by a list of tools -- same reason as forge_tool",
    "recurrent_begin":   "opens a self-generating loop, for the same reason",
    "recurrent_step":    "advances that loop",
    "recurrent_state":   "reads that loop",
    "loop_trajectory":   "reports on those iterations; nothing to report when the loop is not available",
    "render_page":       "fetches a page after its JavaScript has run -- web_fetch's reason applies unchanged, and the container's package manager is where downloads belong",
    # NOT WEIGHED, RECORDED. This is the one of the eleven a worker could plausibly want: it is
    # replace_in_file with a verification and an automatic undo, which is strictly safer than
    # the multi_edit already allowed. It is listed here because that is what the gate already
    # did with it, NOT because a case against it was made. Promoting it is an operator's call.
    "edit_and_verify":   "not weighed -- recorded as excluded because unlisted already meant refused; the one of the eleven with a real case for promotion",
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


# ---------------------------------------------------------------------------------------
# ENFORCEMENT, AND WHY IT STARTS IN SHADOW
#
# The gateway cannot tell a fleet worker from the operator. Authentication carries an API key
# and no user identity -- that was established separately and is structural, not an oversight.
# So the only signal available is WHEN: an unattended fleet run is in flight.
#
# That is coarse. It would also restrict the operator if they used the gateway during a run.
# Which is exactly why this does not begin by refusing anything: the last time a gate was
# switched from permissive to closed without measuring first, the review that caught it said
# to shadow for an hour and confirm zero. Same discipline here.
#
# THE GATEWAY CONSULTS `check` AGAIN, from 2026-09-14. It had not since 2026-08-31: main.py
# removed the call site deliberately ("the benchmark's tool-population policy ... is a fact
# about that benchmark, not about this server") and left the list here "for the runner that owns
# it" -- and the runner never consulted it either, so for two weeks the modes below described a
# gate wired to nothing while a heading claimed "the gateway actually consults it".
#
# THE REMOVAL'S ARGUMENT IS STILL RIGHT AND THE REMOVAL WAS STILL WRONG. The policy does not
# live in dispatch; it lives here, and main.py asks it one question. What went out with the call
# site was `_fleet_run_active()` -- the mechanism built for the gateway's one real problem, that
# it cannot tell a worker from the operator. That is what makes a check in general dispatch
# harmless to the operator: outside an unattended run it allows everything. Operator decision,
# recorded in docs/unreached_burndown.md.
#
#   off      the policy is not consulted at all
#   shadow   every call that WOULD be refused is recorded; nothing is blocked
#   enforce  calls outside the allowed set are refused while a run is active   <- default
#
# Promote to enforce only after a shadow window shows the refusals are the ones intended --
# a shadow log full of read_file means the set is wrong, not that the workers are hostile.
#
# THE SHADOW WINDOW HAS NOW RUN. Measured 2026-08-30, 09:01 to 11:11, across a graded
# 40-instance run and a three-arm A/B: FOUR entries, all of them `process_kill`. Nothing a
# worker legitimately needs appeared -- no read_file, no write_file, no shell_exec -- so the
# set is not costing the benchmark anything, and the one tool it caught is the one that
# reaches any process on the machine including the server that is hosting the gate. A worker
# that needs to end something it started can do so through shell_exec, inside its own
# process tree.
#
# AND THE LOG THAT SENTENCE CITES WAS BEING WRITTEN BY THE TESTS ABOUT IT. Found 2026-09-14:
# SHADOW_LOG was a RELATIVE path and was not in conftest's redirect table, so every local run of
# relay/test_fleet_toolset.py appended a row to the operator's file -- measured 219 -> 220 on
# one run. The file held 220 rows spanning 2026-08-30 to 2026-09-14, not the four the paragraph
# above describes, and every row said `process_kill`, which is precisely the tool those tests
# pass. So the log could not distinguish a refusal that happened from one a test simulated.
#
# THE FOUR ARE STILL THE FIRST FOUR ROWS and still fall inside the stated window, so the window
# itself is not withdrawn -- but it is the only part of that file anyone may read as evidence,
# and even it cannot be proven free of a test write. The default stays `enforce` because the
# argument for it does not rest on the count: `process_kill` reaches any process on this
# machine, including the server hosting the gate, and shell_exec covers a worker's own tree.
# The log is isolated from 2026-09-14, so the NEXT window will measure what it claims to.
#
# That was the evidence for enforce, AND IT HAS SINCE BEEN SWITCHED -- `mode()` defaults to
# enforce and test_the_default_is_enforce_now_that_the_shadow_window_has_run pins it. This
# paragraph said "deliberately NOT switched here ... set FLEET_TOOLSET_MODE=enforce when no run
# is in progress" for as long as the opposite was true. Corrected 2026-09-14, in the same batch
# that found the test section below claiming the gateway consults a gate nothing consults:
# prose contradicting a passing test is the failure this module keeps producing.
#
# Set FLEET_TOOLSET_MODE=shadow to measure again; shadow remains reachable and is tested.
import json as _json
import os as _os
import time as _time

MODE_ENV = "FLEET_TOOLSET_MODE"
SHADOW_LOG = ".fleet/toolset_shadow.jsonl"


def mode():
    """off / shadow / enforce. The DEFAULT IS ENFORCE, and the shadow window is why.

    It defaulted to shadow while nobody knew what enforcing would refuse. That window has now
    run: 09:01 to 11:11 on 2026-08-30, across a graded 40-instance run and a three-arm A/B,
    producing FOUR entries -- all of them process_kill. Nothing a worker legitimately needs
    appeared. No read_file, no write_file, no shell_exec.

    A gate that has been measured and left permissive is not caution, it is a gate that does
    nothing. The env var still moves it back for a run that needs to, and the reason for doing
    so should be written down somewhere at the time.
    """
    m = (_os.environ.get(MODE_ENV) or "enforce").strip().lower()
    return m if m in ("off", "shadow", "enforce") else "enforce"


def _fleet_run_active():
    """Is an unattended fleet run in flight right now.

    Read through contract_state so that a policy file which is missing or corrupt is not
    silently read as 'no run' -- that fail-open is the one this repository just closed.
    """
    try:
        from tools.contract_gate import contract_state
        state, _ = contract_state()
        return state == "active"
    except Exception:
        return False


def check(name):
    """(allowed, note). Never raises: a policy that can crash the gateway is worse than none."""
    try:
        m = mode()
        if m == "off" or is_allowed(name):
            return True, ""
        if not _fleet_run_active():
            return True, ""
        note = ("tool %r is outside the fleet's allowed set (%d tools); "
                "reason it is not there: %s"
                % (name, len(FLEET_TOOLS),
                   DELIBERATELY_EXCLUDED.get(name, "never decided about")))
        try:
            with open(SHADOW_LOG, "a", encoding="utf-8") as f:
                f.write(_json.dumps({"t": _time.time(), "tool": name, "mode": m,
                                     "would_refuse": True}) + "\n")
        except Exception:
            pass
        if m == "shadow":
            return True, note
        return False, note
    except Exception:
        return True, ""
