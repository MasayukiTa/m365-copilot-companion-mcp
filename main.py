import os
import time

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from tools.archive_ops import zip_create, zip_extract, zip_list
from tools.clipboard_ops import clipboard_get, clipboard_set
from tools.docx_ops import create_docx, docx_from_markdown, docx_info, read_docx
from tools.code_exec import run_python, shell_exec
from tools.coding_ops import (
    diff_files,
    git_add,
    git_blame,
    git_branch,
    git_checkout,
    git_commit,
    git_diff,
    git_log,
    git_status,
    grep,
    multi_edit,
    python_check,
    replace_in_file,
)
from tools.data_ops import read_excel, read_json, summarize_table, write_excel, write_json
from tools.diagram_ops import render_diagram, render_mermaid_png
from tools.env_ops import env_info, pip_install, which
from tools.file_ops import (
    append_file,
    copy_path,
    create_directory,
    delete_path,
    dir_size,
    file_metadata,
    find_duplicates,
    hash_file,
    list_directory,
    move_path,
    read_file,
    trash_path,
    write_file,
)
from tools.image_ops import image_info, read_image
from tools.memory_ops import (
    memory_delete,
    memory_list,
    memory_load,
    memory_save,
    semantic_memory_delete,
    semantic_memory_list,
    semantic_memory_load,
    semantic_memory_save,
)
from tools.procedural_memory import (
    procedural_memory_delete,
    procedural_memory_import_markdown,
    procedural_memory_save,
    procedural_memory_search,
)
from tools.agent_memory_ops import (
    agent_memory_list,
    agent_memory_read,
    agent_memory_save,
    agent_memory_search,
)
from tools.data_discovery import find_db_objects
from tools.data_memory_hook import data_memory_status
from tools.data_aliases import data_aliases_add, data_aliases_list
from tools.data_report import data_report_plan, data_report_run, data_report_explain
from tools.notify_ops import notify_desktop
from tools.outlook_ops import (
    outlook_calendar,
    outlook_create_event,
    outlook_inbox,
    outlook_send_mail,
)
from tools.gate_ops import (
    gate_ask,
    gate_list,
    gate_poll,
    stop_check,
    stop_clear,
    stop_request,
)
from tools.process_ops import process_info, process_kill, process_list
from tools.runlog_ops import runlog_append, runlog_list, runlog_read, runlog_summarize
from tools.trace_ops import toolcalls_tail
from tools.verify_ops import (
    verify_python,
    verify_numeric_close,
    verify_file_contains,
    verify_json_schema,
    verify_table_stat,
)
from tools.foundry import forge_tool, forge_list, forge_read, forge_delete
from tools.registry_ops import registry_read, service_status
from tools.shell_extra import pwsh_exec, pwsh_exec_file, shell_which
from tools.screenshot_ops import screenshot
from tools.odbc_ops import (
    odbc_columns,
    odbc_connections,
    odbc_drivers,
    odbc_query,
    odbc_tables,
    odbc_to_excel,
)
from tools.ocr_ops import ocr_image, ocr_pdf
from tools.render_ops import render_math
from tools.schedule_ops import (
    schedule_create,
    schedule_delete,
    schedule_info,
    schedule_list,
    schedule_run_now,
)
from tools.search_web import web_search, web_search_news
from tools.sql_ops import sqlite_query, sqlite_schema, sqlite_tables, sqlite_to_excel
from tools.watcher_ops import watcher_events, watcher_start, watcher_stop
from tools.auth_stats import get_summary as _auth_stats_summary
from tools.tool_probe import get_summary as _tool_probe_summary
from tools.jobs import (
    job_kill,
    job_list,
    job_output,
    job_status,
    job_wait,
    run_in_background,
    run_python_in_background,
)
from tools.local_loop_ops import (
    abort_turn,
    claim_turn,
    commit_turn,
    get_job_status,
    heartbeat,
    read_job_context,
)
from tools.skill_ops import (skill_list, skill_load, skill_match, skill_read_resource,
                             skill_request_approval)
from tools.pdf_ops import pdf_info, read_pdf
from tools.pptx_ops import (
    create_pptx,
    pptx_add_image,
    pptx_add_slide,
    pptx_add_table,
    pptx_export_png,
    pptx_from_markdown,
    pptx_info,
    pptx_replace_image,
)
from tools.registry import list_my_tools, register
from tools.search_ops import find_files, glob
from tools.security import list_unlocked, unlock
from tools.task_ops import todo_clear, todo_list, todo_write
from tools.web_ops import github_file, web_fetch

load_dotenv()

EXECUTION_PROFILE_TOOLS = (
    claim_turn, heartbeat, commit_turn, abort_turn, read_job_context, get_job_status,
) if os.environ.get("MCP_EXECUTION_PROFILES", "0") == "1" else ()

API_KEY = os.environ["MCP_API_KEY"]

auth = StaticTokenVerifier(
    tokens={
        API_KEY: {
            "client_id": "copilot-studio",
            "scopes": ["mcp.use"],
        },
    },
)

mcp = FastMCP(
    "m365-copilot-companion-mcp",
    auth=auth,
    instructions=(
        "m365-copilot-companion-mcp toolkit. "
        # The catalogue is reachable ONLY through the call_tool gateway. Naming tools as
        # though they were directly callable made agents look for read_file/list_directory
        # in their own tool list, not find them, and conclude the tools did not exist --
        # then answer 'この環境には存在しません' while holding 166 usable tools. State the gateway
        # first, before anything else, and never imply a bare tool name is callable.
        # 実測: 同じ問いを3回投げると、一覧を取った回は成功し、取らなかった回は
        # 「この環境にファイルシステムのツールは無い」と断定して拒否した。差はそこだけ
        # だったので、一覧取得を「推奨」ではなく「最初の行動」として書き切る。
        "RULE 1 -- DO THIS FIRST, ALWAYS: call call_tool(name='') and read the returned "
        "catalogue (about 160 entries) BEFORE answering anything about what you can or "
        "cannot do. This is not optional and not a fallback. "
        # 実測: 「MT18EX5 RM の期限切れを調べて」の一言を投げたら、承認済みの手順が
        # あるのに skill_match を呼ばず、DB を自分で叩いて別解を作文した。もっともらしいが
        # 問い自体がすり替わっていた（材料の期限切れの話になり、本来の「期限を過ぎてから
        # 使われたロット」を出していない）。手順の記述は末尾に置いていて読まれなかったので、
        # 一覧取得の直後に上げる。
        "RULE 2 -- DO THIS SECOND: call skill_match with the user's request, before "
        "doing any domain work. If it returns a confident trusted match, call "
        "skill_load and FOLLOW that procedure as written. Do not re-derive it, do not "
        "write your own query, and do not substitute a similar-sounding question: a "
        "matched Skill encodes decisions that were verified against the real data, and "
        "improvising past it has produced confident wrong answers. Skill trust never "
        "grants extra execution rights; unlock and contract gates still apply. "
        "RULE 3: every tool lives BEHIND the call_tool gateway. Names like read_file / "
        "list_directory / run_python / glob are NOT in your own tool list and you will "
        "not find them there. Their absence from your tool list is EXPECTED and proves "
        "nothing. "
        "RULE 4: you may never state that a capability is unavailable, that this "
        "environment lacks filesystem access, or that only Microsoft 365 / GitHub tools "
        "are connected, unless you have already run call_tool(name='') in THIS "
        "conversation and the catalogue actually lacks what you need. Saying it without "
        "having looked is a factual error. "
        "Usage: call_tool(name='X') for a tool's signature, then "
        "call_tool(name='X', arguments={...}) to run it. "
        # 実測: 一覧取得は10/10通るのに、答えは 14〜18 とばらけた(正解16)。
        # 最初は「run_python で計算しろ」と書いたが、run_python は unlock 必須で、
        # 解錠に当たった回がまるごと拒否に戻り 5/10 -> 4/10 と悪化した。
        # そこで glob / list_directory 自身に件数を返させ、ここではそれを読めと言う。
        "RULE 5: read-only listing tools (glob, list_directory, find_files) return "
        "the count on their FIRST line, e.g. '16 matches' or '16 files, 3 "
        "directories'. When asked how many, report THAT number verbatim. Never "
        "tally the listing yourself -- hand-counting is where answers drift. To count files of a kind, call glob('*.md', path) or list_directory(path, pattern='*.md') -- both filter first and hand you the number. Never enumerate an UNFILTERED list_directory and pick out the rows you want: every wrong answer measured came from doing that, and each one was low by one. If you want to cross-check, run both and compare the two first lines. Do not re-derive the number by picking rows out of an unfiltered listing: the run that did that had the correct count in hand, discarded it, and answered 15 instead of 16. "
        "RULE 6: read-only tools (glob, list_directory, find_files, read_file, "
        "search) need no unlock. Only mutating or executing tools (run_python, "
        "shell, write_file) do. Never refuse a read-only request on the grounds "
        "that you are not unlocked, and never reach for run_python when a "
        "read-only tool already answers the question. "
        "With that gateway you can read, search, edit and inspect local files; run "
        "bounded Python or shell commands; work with CSV/Excel/JSON; generate PowerPoint "
        "decks and diagrams; manage long-running jobs; verify your own output via "
        "read_image and pptx_export_png; and install Python dependencies. Read-only "
        "tools work after token authentication; mutating or execution tools additionally "
        "require unlock(password), per client IP. "
        # THE AGENT CANNOT COMPLY WITH A RULE IT WAS NEVER TOLD. The token was added to the
        # gate, to unlock()'s reply and to call_tool's signature, and none of those is a place
        # an agent reliably reads BEFORE its first refusal. It goes here, next to the unlock
        # sentence it modifies, because "carry a value between calls" is only free if the
        # instruction is in front of the model the whole time.
        "RULE 7: unlock(password) replies with a line `unlock_token: <value>`. KEEP that "
        "value for the rest of the conversation and pass it on every mutating or executing "
        "call: call_tool(name='run_python', arguments={...}, unlock_token='<value>'). It is "
        "shown once; if you lose it, call unlock again. A refusal mentioning a missing token "
        "means only that -- add the token, do not retry the identical call. "
        "Relative user-folder names (Desktop, Documents, Downloads, ...) resolve to the "
        "user's home profile, not the server's working directory. If a file or folder "
        "seems missing, use find_files (recursive name search) before concluding it is "
        "absent, and never claim a path is outside the allowed base unless a tool call "
        "actually returned that error. "
        "Persistent memory: when durable knowledge emerges (procedures, data sources, "
        "format templates, decisions, or facts) or the user says to remember it (e.g., "
        "\"刻んで\"), call agent_memory_save via call_tool to persist it; before "
        "re-deriving, search prior entries with agent_memory_search or agent_memory_list. "
        "Reusable Skills: call skill_match using the user's request, then skill_load only "
        "for a confident trusted match. Skill trust never grants extra execution rights; "
        "every mutating, shell, or outbound tool keeps its normal unlock/contract gate."
    ),
)

@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness probe that NEVER touches a blocking tool.

    This async handler runs directly on the event loop and returns immediately, so the
    supervisor can distinguish "the loop is briefly busy running a heavy tool in a worker
    thread" (this still answers fast, because tool bodies are now offloaded) from "the
    loop is actually dead". It does no auth and does no blocking I/O on purpose (the
    auth-failure summary below is an in-memory read of tools.auth_stats' module
    singleton, not a file read).

    Also surfaces auth_fail_10m / auth_fail_last_ts (see tools/auth_stats.py) so a
    burst of 401s -- e.g. Copilot Studio's stored key desyncing from MCP_API_KEY --
    is visible to the supervisor/cockpit without grepping logs. get_summary() never
    raises, so this can't turn a healthy-loop probe into a 500."""
    payload = {"status": "ok"}
    payload.update(_auth_stats_summary())
    payload.update(_tool_probe_summary())
    return JSONResponse(payload)


TOOLS = (
    # code execution
    run_python, shell_exec,
    pwsh_exec, pwsh_exec_file, shell_which,
    run_in_background, run_python_in_background,
    job_status, job_wait, job_output, job_list, job_kill,
    # processes / services / registry (Windows host introspection)
    process_list, process_info, process_kill,
    service_status, registry_read,
    # files (read)
    read_file, list_directory, glob, find_files,
    # files (write / mutate)
    write_file, append_file, copy_path, move_path,
    create_directory, delete_path, trash_path,
    # file metadata / disk forensics
    hash_file, find_duplicates, dir_size, file_metadata,
    # archives
    zip_list, zip_extract, zip_create,
    # tabular / json
    read_excel, write_excel, summarize_table, read_json, write_json,
    # text search / editing
    grep, replace_in_file, multi_edit, diff_files, python_check,
    # git (read)
    git_status, git_diff, git_log, git_branch, git_blame,
    # git (write)
    git_add, git_commit, git_checkout,
    # web
    web_fetch, github_file,
    web_search, web_search_news,
    # images / pdf (self-verification)
    read_image, image_info,
    read_pdf, pdf_info,
    ocr_image, ocr_pdf,
    # diagrams / math
    render_diagram, render_mermaid_png,
    render_math,
    # memory (cross-session) -- semantic (facts/preferences; book SS17/SS28.16 taxonomy)
    memory_save, memory_load, memory_list, memory_delete,
    # memory: semantic_* back-compat aliases (same store as memory_* above)
    semantic_memory_save, semantic_memory_load, semantic_memory_list, semantic_memory_delete,
    # memory: procedural (reusable how-to success snippets; distinct store)
    procedural_memory_save, procedural_memory_search, procedural_memory_delete,
    # memory: procedural bulk-import (gateway-only: appended after the priority/include
    # sets are fixed above, so MCP_TOOL_MAP truncation never registers it directly --
    # reachable only via call_tool("procedural_memory_import_markdown", {...})).
    procedural_memory_import_markdown,
    # memory: agent_memory/ WRITE engine -- structured per-topic notebooks (summary,
    # data_sources, method, key_facts, decisions, artifacts, next_actions, ...), distinct
    # from the flat KV store above and from procedural_memory's slug-keyed snippets.
    # Named agent_memory_* (not memory_*) because tools.memory_ops already exports
    # module-level memory_save/memory_list -- reusing those names would collide in
    # main.py's _ALL_TOOLS dict, which is keyed by function __name__. Gateway-only, same
    # reasoning as procedural_memory_import_markdown above -- reachable only via
    # call_tool("agent_memory_save"/"agent_memory_search"/"agent_memory_read"/
    # "agent_memory_list", {...}).
    agent_memory_save, agent_memory_search, agent_memory_read, agent_memory_list,
    # DB discovery: NL entry point over procedural memory + live ODBC fallback
    # (gateway-only, same reasoning as procedural_memory_import_markdown above).
    find_db_objects,
    # data-memory hook status (read-only, no unlock needed; gateway-only, same
    # reasoning as find_db_objects above -- reachable only via
    # call_tool("data_memory_status", {})).
    data_memory_status,
    # DB aliases: synonym/abbreviation store widening find_db_objects queries
    # (gateway-only; add is unlock-gated, list is read-only).
    data_aliases_add, data_aliases_list,
    # NL report pipeline: plan (memory-first, pre-fills SQL) -> run (deterministic
    # execute + provenance + report_manifest.json) -> explain (always-present
    # provenance block). Gateway-only, reachable only via call_tool.
    data_report_plan, data_report_run, data_report_explain,
    # notifications
    notify_desktop,
    # scheduling (Windows Task Scheduler)
    schedule_create, schedule_list, schedule_info, schedule_run_now, schedule_delete,
    # filesystem watcher
    watcher_start, watcher_events, watcher_stop,
    # SQLite (local read-only DB)
    sqlite_tables, sqlite_schema, sqlite_query, sqlite_to_excel,
    # ODBC (corporate DBs via Windows / Entra auth)
    odbc_drivers, odbc_connections, odbc_tables, odbc_columns, odbc_query, odbc_to_excel,
    # pptx
    create_pptx, pptx_from_markdown, pptx_info,
    pptx_add_slide, pptx_add_image, pptx_add_table, pptx_replace_image,
    pptx_export_png,
    # docx (Word documents)
    create_docx, docx_from_markdown, docx_info, read_docx,
    # outlook (mail + calendar via local COM, no Graph API)
    outlook_inbox, outlook_send_mail, outlook_calendar, outlook_create_event,
    # clipboard / screen capture
    clipboard_get, clipboard_set, screenshot,
    # task management
    todo_write, todo_list, todo_clear,
    # orchestration: audit/replay run-log (operator D)
    runlog_append, runlog_read, runlog_list, runlog_summarize,
    # orchestration: tool-call trace (observability; only records when MCP_TRACE_TOOLCALLS set)
    toolcalls_tail,
    # orchestration: human-in-the-loop gate + kill-switch (operator E)
    # gate_answer is intentionally local-operator-only; a model must not approve its own gate.
    gate_ask, gate_poll, gate_list,
    stop_request, stop_check, stop_clear,
    # Model-facing Skill operations are read-only and accept only exact trusted digests.
    skill_list, skill_match, skill_load, skill_read_resource, skill_request_approval,
    # orchestration: response-content-independent LOCAL_LOOP control plane
    *EXECUTION_PROFILE_TOOLS,
    # orchestration: verification-loop helpers (operator C)
    verify_python, verify_numeric_close, verify_file_contains,
    verify_json_schema, verify_table_stat,
    # orchestration: tool foundry (operator A) - forged tools live after restart
    forge_tool, forge_list, forge_read, forge_delete,
    # env / introspection
    list_my_tools, env_info, pip_install, which,
    # security
    unlock, list_unlocked,
)

# --- Tool-gateway mode (MCP_TOOL_MAP=1) --------------------------------------------------
# Some clients cap an agent at a fixed number of tools (Copilot Studio = 70). With 138 tools
# the tail -- including unlock/list_unlocked -- is silently dropped, so a connected agent can
# neither self-unlock nor reach the long-tail tools. In map mode we register a high-value
# subset FIRST (critical + SWE/coding + common), with `call_tool` and `unlock` at the very
# front so they survive truncation, plus one `call_tool(name, arguments)` gateway that invokes
# ANY of the full tool set by name. Discovery: call_tool(name="") lists every tool with its
# signature + summary. Default (env unset) registers all tools unchanged -- no behavior change
# for full-capability clients (e.g. Claude Code).
import inspect as _inspect

_ALL_TOOLS = {getattr(t, "__name__", repr(t)): t for t in TOOLS}

if os.environ.get("MCP_TOOL_MAP") == "1":
    def call_tool(name: str = "", arguments: dict = None, unlock_token: str = ""):
        """Gateway to EVERY tool on this server -- START HERE by calling call_tool(name="").

        This client caps the tool list, so most tools are NOT visible to you and are reached
        only through this one. File, shell, Excel, database and image tools all exist here.
        Their absence from your own tool list is expected and proves nothing about what this
        server can do -- so never answer "there is no tool for that", "this environment has no
        filesystem access", or "only Microsoft 365 tools are connected" without having called
        call_tool(name="") in this conversation and read the catalogue. Measured: the runs that
        listed first answered correctly; the runs that declared a capability missing without
        listing were simply wrong.

        Then call_tool(name="<tool>") shows one tool's signature, and
        call_tool(name="<tool>", arguments={...}) runs it. Read-only tools (glob, read_file,
        list_directory, find_files) run as-is; only mutating or executing tools need unlock.

        UNLOCKING, AND KEEPING THE TOKEN. `unlock(password="...")` replies with a line reading
        `unlock_token: <value>`. KEEP THAT VALUE for the rest of the conversation and pass it
        on every mutating or executing call:

            call_tool(name="run_python", arguments={"code": "..."}, unlock_token="<value>")

        It is shown once and cannot be retrieved afterwards; if it is lost, call unlock again.
        A refusal that mentions a missing token means exactly this and nothing else -- the
        remedy is to include it, not to retry the same call.

        Args:
            name: the tool's name (e.g. "odbc_query"). Empty or "?" lists every tool.
            arguments: dict of the target tool's keyword arguments.
            unlock_token: the value from unlock()'s reply. Required for mutating and executing
                tools once MCP_REQUIRE_UNLOCK_TOKEN is on; harmless to pass at any time.
        """
        if not name or name in ("?", "list", "*"):
            # COMPACT catalog: name -- one-line summary only (no signatures), so an agent
            # with a small context window can scan every tool without overflowing. Get one
            # tool's full signature/usage with call_tool(name="<tool>") (no arguments).
            rows = []
            for n in sorted(_ALL_TOOLS):
                doc = (getattr(_ALL_TOOLS[n], "__doc__", "") or "").strip().splitlines()
                rows.append("%s -- %s" % (n, doc[0] if doc else ""))
            return ("%d tools available. Pick what THIS task needs, then: "
                    "call_tool(name='X') shows X's signature; "
                    "call_tool(name='X', arguments={...}) runs X.\n%s" % (
                        len(rows), "\n".join(rows)))
        fn = _ALL_TOOLS.get(name)
        if fn is None:
            return "[call_tool: unknown tool '%s'. Use call_tool(name='') to list all.]" % name
        if arguments is None:
            # HELP for ONE tool: signature + doc. To actually run a no-arg tool, pass arguments={}.
            try:
                sig = str(_inspect.signature(fn))
            except Exception:
                sig = "(...)"
            return "%s%s\n%s" % (name, sig, (getattr(fn, "__doc__", "") or "").strip())
        # REMOVED: the benchmark's tool-population policy, consulted here.
        #
        # Which sixteen tools a benchmark worker may reach is a fact about that benchmark,
        # not about this server, and general dispatch is the wrong place to hold it. The
        # list is still in relay/fleet_toolset.py for the runner that owns it.
        # EVIDENCE TRACE. Off unless a runner asked for one, and a no-op in ordinary
        # operation. This is the only point that sees every dispatched call with its real
        # name and arguments -- recording at the adapter would name `call_tool` and nothing
        # useful, and reconstructing afterwards from the filesystem cannot see a file that
        # was created, read and deleted. See tools/evidence_trace.py for what a trace can
        # and cannot support.
        try:
            from tools import evidence_trace as _trace
        except Exception:
            _trace = None
        # THE UNLOCK TOKEN, WHICH ONLY HAS ONE PLACE TO ARRIVE.
        #
        # The gate's identity comes from a forwarding header, which a caller states rather
        # than proves, so possession of the API key was enough to be believed. A second factor
        # has to be presented per call; the client cannot vary headers per call; so the only
        # channel is an argument. This is the single point every gated tool passes through --
        # one declared parameter here rather than one added to 116 signatures.
        #
        # IT HAS TO BE A DECLARED PARAMETER, and finding that out took a live request. The
        # first version accepted it as an extra key inside `arguments`, which every unit test
        # was happy with because they call the function directly -- but the MCP layer
        # validates the call against this signature and rejects an unexpected keyword before
        # any of this code runs. The tests were testing the function; the client talks to the
        # schema.
        #
        # Also accepted inside `arguments` for a client that finds that shape easier, and
        # popped there so it never reaches the tool: the tool has no such parameter and would
        # raise TypeError, and a credential should not be handed to arbitrary tool code by
        # accident.
        _args = dict(arguments or {})
        _token = (unlock_token or _args.pop("unlock_token", "") or "")
        _args.pop("unlock_token", None)
        _sec, _tok_handle = None, None
        try:
            from tools import security as _sec
            _tok_handle = _sec.set_presented_token(str(_token))
        except Exception:
            _sec = None
        # THE PROBE'S OWN CALL ARRIVING HERE IS THE SIGNAL, AND ITS ABSENCE IS THE ALARM.
        #
        # The incident the self-probe exists for -- a lapsed connector consent -- kills the call
        # INSIDE the Copilot web UI, upstream of this process, so nothing here ever moves and
        # every health dot stays green. The success is fully visible here though: the probe asks
        # the agent to list one directory whose name only this server knows. Stamping the arrival
        # gives the bridge a signal that does not depend on parsing reply text, and that reads
        # the same whether the turn went over a page or a socket.
        #
        # On the hot path, so the cheap test comes first and nothing is written unless the call
        # is ours; it can never raise, because a tool call must not fail over bookkeeping.
        try:
            from tools import tool_probe as _probe
            _probe.note_inbound(name, _args)
        except Exception:
            pass
        # PLACED AFTER `_args` EXISTS. It was written above the argument parsing and read
        # `_args` sixty-five lines before that name is bound, so EVERY call through this
        # gateway raised UnboundLocalError the moment routing shipped. Nothing routed,
        # nothing ran locally, and the fleet's workers reported STUCK -- which reads like
        # models failing at the task. The tests all passed: they assert on this file's
        # SOURCE, and source assertions cannot catch a name that is not bound yet.
        # REMOVED: routing every tool call to a container on a remote SSH host.
        #
        # It was written to close three findings whose common cause was 'the worker can
        # write where the harness lives'. Relocating the writes is not a fix: it closes
        # nothing for anyone without a second machine, and this repository is not for one
        # machine. It also put ssh and docker assumptions into the shipped dispatch path,
        # and cost 2.5-25 seconds per call, which is not a slow benchmark but a broken one.
        # The broker now lives under bench/remote/ where the benchmark's own infrastructure
        # belongs. See docs/SECURITY.md for what actually contains a worker here.
        # THE TOOL LEDGER, WRITTEN AROUND THE CALL RATHER THAN AFTER IT.
        #
        # Measured: the session store holds 122 MB and 15,605 turns and records NOT ONE tool
        # call -- only what the two sides wrote about the work. So the refuter that exists to
        # catch a wrong DONE reads the worker's own account of what it did, and DONE precision
        # is 0.718. Every "did it actually do that" was unanswerable.
        #
        # The CALL record goes down before fn runs, so a call that never returns leaves an
        # orphan -- which is a finding, not a gap. A ledger written only on completion records
        # exactly the runs that did not need recording.
        _cid = ""
        _t0 = time.time()
        try:
            from tools import tool_ledger as _ledger
            _cid = _ledger.record_call(name, _args, task=_args.get("_task", ""),
                                       worker=_args.get("_worker", ""))
        except Exception:
            _ledger = None
        try:
            _out = fn(**_args)
            if _trace is not None:
                _trace.record(name, _args, True, _out, fn)
            if _ledger is not None and _cid:
                try:
                    _ledger.record_outcome(_cid, ok=True, result=_out,
                                           duration_s=time.time() - _t0)
                except Exception:
                    pass
            return _out
        except Exception as _e:
            if _trace is not None:
                _trace.record(name, _args, False,
                              "%s: %s" % (type(_e).__name__, _e), fn)
            if _ledger is not None and _cid:
                try:
                    _ledger.record_outcome(_cid, ok=False,
                                           error="%s: %s" % (type(_e).__name__, _e),
                                           duration_s=time.time() - _t0)
                except Exception:
                    pass
            return "[call_tool %s error: %s: %s]" % (name, type(_e).__name__, _e)
        finally:
            # SCOPED TO THIS CALL. A token left behind would authorise the next one, which is
            # the same class of defect as the identity it replaces.
            # RESET TO WHAT THIS CONTEXT HELD BEFORE, rather than blanking. Blanking erases a
            # caller's token if a gated tool ever reaches the gateway again; reset restores it.
            if _sec is not None and _tok_handle is not None:
                try:
                    _sec.reset_presented_token(_tok_handle)
                except Exception:
                    pass

    # critical tools FIRST (survive a front-biased truncation), then fill from the existing order
    _LOCAL_LOOP_PRIORITY = (
        claim_turn, heartbeat, commit_turn, abort_turn, read_job_context, get_job_status,
    ) if EXECUTION_PROFILE_TOOLS else ()
    # With the default cap=8, LOCAL_LOOP needs all six protocol tools plus unlock and
    # call_tool. Keep call_tool inside that hard boundary so claimed jobs can still use
    # the hidden local work tools; list_unlocked is diagnostic and may safely spill out.
    # Keep the gateway immediately after unlock. Some hosts truncate or cache a
    # front-biased schema subset; losing diagnostic get_job_status is survivable, while
    # losing call_tool makes claimed work impossible.
    _PRIORITY = (unlock, call_tool, *_LOCAL_LOOP_PRIORITY, list_unlocked, list_my_tools, env_info)
    # A SMALL registered set is not just about the 70 cap: each tool's schema costs input
    # tokens, and a Copilot Studio agent's model has a limited budget -- with all ~70 schemas
    # loaded, even a short task prompt overflows (OpenAIModelTokenLimit) before any work. So
    # the registered count is configurable: MCP_TOOL_MAP_MAX (default 70). MCP_TOOL_MAP_INCLUDE
    # is a comma-separated list of tool names pinned right after the priority set, so a focused
    # agent can carry exactly the few tools its task needs (e.g. file + OCR + Excel) and reach
    # everything else via the call_tool gateway.
    try:
        _MAX = int(os.environ.get("MCP_TOOL_MAP_MAX", "70"))
    except ValueError:
        _MAX = 70
    # EXECUTION GOES THROUGH THE GATEWAY, NOT AROUND IT.
    #
    # Every tool that runs code or a shell is removed from the DIRECT registration and stays
    # reachable only via call_tool. Two reasons, and the second is the one that made this
    # necessary rather than tidy:
    #
    #   The server's own instructions already tell an agent that every tool lives behind
    #   call_tool, so this makes the registration match the documentation instead of
    #   contradicting it in the eight most dangerous cases.
    #
    #   And it gives the execution family a single entry point. Without one there is nowhere
    #   to require a second factor: an unlock token has to be presented somewhere, the client
    #   cannot vary headers per call, and adding an argument to 116 gated tools is not a
    #   change anyone can review. One gateway is one place.
    #
    # Nothing becomes unreachable. Measured: the direct set stays at the same size, the eight
    # execution tools leave it, and eight others (render_math, the memory tools) move up into
    # the space -- so the schema budget is unchanged too.
    #
    # MCP_TOOL_MAP_EXEC_DIRECT=1 restores the old behaviour, because a claim that "usage is
    # unchanged" should be falsifiable by the operator rather than only by me.
    _EXEC_ONLY_VIA_GATEWAY = frozenset({
        "run_python", "shell_exec", "pwsh_exec", "pwsh_exec_file", "shell_which",
        "run_in_background", "run_python_in_background", "job_kill",
    })
    _exec_direct = os.environ.get("MCP_TOOL_MAP_EXEC_DIRECT") == "1"
    _pn = {getattr(f, "__name__", "") for f in _PRIORITY}
    _want = [n.strip() for n in os.environ.get("MCP_TOOL_MAP_INCLUDE", "").split(",") if n.strip()]
    _pinned = [_ALL_TOOLS[n] for n in _want if n in _ALL_TOOLS and n not in _pn]
    _pinned_n = _pn | {getattr(f, "__name__", "") for f in _pinned}
    _rest = [t for t in TOOLS if getattr(t, "__name__", "") not in _pinned_n
             and (_exec_direct or getattr(t, "__name__", "") not in _EXEC_ONLY_VIA_GATEWAY)]
    # The priority set itself can grow beyond the configured schema budget. Enforce
    # the cap here too; ordering above defines which critical tools survive.
    _head = (list(_PRIORITY) + _pinned)[:max(0, _MAX)]
    TOOLS = tuple(_head + _rest[: max(0, _MAX - len(_head))])
# ----------------------------------------------------------------------------------------

# --- Tool annotations (MCP spec §21.5.3 / §21.6.2) --------------------------------------
# readOnlyHint / destructiveHint / idempotentHint / openWorldHint let a connected HOST
# drive its consent UI (e.g. auto-approve read-only tools) instead of prompting uniformly
# for every tool. See tools/tool_annotations.py for the derivation: readOnlyHint is
# derived mechanically from static source inspection (no require_unlocked() call = read
# only); the other three hints come from a small, hand-curated override table. Best-effort
# per-tool: a failure computing or attaching annotations for one tool must never stop the
# others from registering.
try:
    from tools.tool_annotations import build_annotations

    # Compute annotations over the FULL tool set (_ALL_TOOLS), not the possibly-
    # truncated TOOLS: in MCP_TOOL_MAP gateway mode TOOLS is a small subset, and the
    # override-table consistency check (every destructiveHint tool must be gated) would
    # otherwise see mutating tools as "absent" and fail -> crash the server on startup.
    # Annotations are keyed by name and applied to whichever subset registers below.
    _TOOL_ANNOTATIONS = build_annotations(list(_ALL_TOOLS.values()))
except Exception:
    _TOOL_ANNOTATIONS = {}

# The pre-execution review (tools/command_judge.py) asks the CLIENT for a judgement, and a
# synchronous tool needs a way back to the event loop to do it. register() below puts every
# tool in an anyio worker thread, which is the route that normally carries it; this middleware
# records the loop as well, so a tool that somehow runs elsewhere still has one. Best effort:
# a server that could not install it still works, and availability() reports "loop": false so
# a judge that cannot be reached says why instead of timing out.
try:
    from tools import judge_backend as _judge_backend

    _judge_backend.install(mcp)
except Exception:
    pass

for tool in TOOLS:
    _name = getattr(tool, "__name__", "")
    _ann = _TOOL_ANNOTATIONS.get(_name)
    try:
        if _ann:
            mcp.tool(annotations=_ann)(register(tool))
        else:
            mcp.tool()(register(tool))
    except Exception:
        # Never let one tool's annotation attachment block the rest of registration.
        mcp.tool()(register(tool))

# Auto-register forged tools (operator A). Safe when tools/auto is empty:
# load_auto_tools() returns [] and any failing forged module is skipped.
try:
    from tools.auto_loader import load_auto_tools

    for _mod_name, _forged in load_auto_tools():
        try:
            mcp.tool()(register(_forged))
        except Exception:
            pass
except Exception:
    pass

# --- Resource pilot (MCP spec 21.3.2 / Quick Reference 28.17) ---------------------------
# ADDITIVE ONLY: model side-effect-free READ data as MCP Resources, a primitive distinct
# from Tools (separate manager/cap -- see tools/resource_ops.py docstring). This does not
# remove, wrap, or alter any existing tool; it only adds a few read-only URIs alongside
# them. Each registration is individually best-effort so a failure here can never stop the
# server (mirrors the forged-tools try/except above).
try:
    from tools.resource_ops import file_resource, jobs_resource, server_info_resource

    try:
        mcp.resource("companion://server/info")(server_info_resource)
    except Exception:
        pass
    try:
        # Templated resource: companion://file/{path*} -- the RFC 6570 wildcard form
        # ({path*}, not {path}) is required because a plain {path} segment cannot
        # contain "/", and a real filesystem path needs multiple segments. Same
        # _validate_path guardrail as the read_file tool (see
        # tools/resource_ops.file_resource docstring).
        mcp.resource("companion://file/{path*}")(file_resource)
    except Exception:
        pass
    try:
        mcp.resource("companion://jobs/list")(jobs_resource)
    except Exception:
        pass
except Exception:
    pass
# ----------------------------------------------------------------------------------------


def _install_faulthandler() -> None:
    """Dump every thread's stack to a file periodically so a wedged event loop leaves a
    forensic trail. dump_traceback_later(repeat=True) re-arms itself; if the loop is
    frozen the dump still fires because faulthandler uses a separate watchdog thread.
    Best-effort: any failure here must not stop the server from starting."""
    try:
        import faulthandler
        from pathlib import Path

        dump_dir = Path(__file__).resolve().parent / ".fleet"
        dump_dir.mkdir(parents=True, exist_ok=True)
        dump_path = dump_dir / "faulthandler.log"

        # ROTATE. Every thread's stack, every five minutes, appended for ever: the file was
        # found at 51.8 MB, and it had no end. On a machine whose disk floor is measured in
        # single-digit gigabytes -- and which spent today deferring fleet admission because
        # the disk was tight -- a forensic log with no bound is a slow leak that eventually
        # costs the thing it exists to diagnose.
        #
        # One generation is kept. The value of these dumps is the recent ones: a wedge is
        # diagnosed from the last few heartbeats, not from a fortnight of healthy ones.
        try:
            cap = int(os.environ.get("MCP_FAULTHANDLER_MAX_BYTES", str(8 * 1024 * 1024)))
            if dump_path.exists() and dump_path.stat().st_size > cap:
                previous = dump_path.with_suffix(".log.1")
                if previous.exists():
                    previous.unlink()
                dump_path.rename(previous)
        except Exception:
            pass

        # Keep a handle open for the lifetime of the process (faulthandler writes to it).
        _fh = open(dump_path, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_fh)
        # Every 5 min, dump all thread stacks. On a healthy server this is just a periodic
        # heartbeat; when the loop is stuck the dump shows exactly which tool is blocking.
        faulthandler.dump_traceback_later(
            300, repeat=True, file=_fh, exit=False
        )
        # Keep a module-global ref so the file object isn't garbage-collected.
        global _FAULTHANDLER_FILE
        _FAULTHANDLER_FILE = _fh
    except Exception:
        pass


_FAULTHANDLER_FILE = None


def _start_approval_watcher(period: float = 3.0) -> None:
    """押された承認を、押した直後に信頼状態へ取り込む。

    取り込みは Skill を一覧したときにしか走らなかったので、承認を押しても誰も
    一覧しない限り何も起きず、確認画面が消えないままになった（押した側からは
    「承認したのに反応しない」としか見えない）。ここで定期的に拾う。

    失敗しても黙って次の周回へ行く。承認の取り込みが、サーバが立たない理由に
    なってはいけない。
    """
    import threading

    def loop():
        while True:
            try:
                from relay.skills import SkillStore
                SkillStore(os.path.dirname(os.path.abspath(__file__))).sync_approvals()
            except Exception:
                pass
            time.sleep(period)

    t = threading.Thread(target=loop, name="approval-watcher", daemon=True)
    t.start()


if __name__ == "__main__":
    _install_faulthandler()
    _start_approval_watcher()
    # timeout_graceful_shutdown gives in-flight requests up to 30s to finish on SIGTERM
    # instead of an immediate hard kill (uvicorn default 0 = no grace). Passed through
    # FastMCP.run_http_async -> uvicorn.Config(**uvicorn_config).
    # Copilot Studio's MCP connector does NOT send the dual Accept header
    # ("application/json, text/event-stream") that FastMCP 2.14's streamable-http strictly
    # requires -> the server 406'd ("Client must accept both...") and the connector got no tools
    # (surfaced as a malformed [{"jsonrpc":"2.0"}]). Two fixes, both safe for existing clients
    # (the relay already accepts JSON): (1) json_response=True -> reply in plain JSON the connector
    # parses directly instead of SSE; (2) an Accept-normalising ASGI middleware that forces both
    # media types so the 406 can never fire regardless of what the client sends.
    import uvicorn
    from starlette.middleware import Middleware

    class _AcceptBoth:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                hdrs = [(k, v) for (k, v) in (scope.get("headers") or []) if k.lower() != b"accept"]
                hdrs.append((b"accept", b"application/json, text/event-stream"))
                scope = dict(scope)
                scope["headers"] = hdrs
            await self.app(scope, receive, send)

    class _BearerPrefix:
        """Tolerate an Authorization header that is the RAW API key (no scheme).

        Copilot Studio's MCP connector labels the credential field "API key" (auth
        type = API key, header name = Authorization), so a novice pastes the raw
        MCP_API_KEY with NO "Bearer " prefix. StaticTokenVerifier then never sees a
        valid bearer token and returns 401 with no clue. This middleware normalises
        the header to "Bearer <value>" when the value does not already start
        (case-insensitively) with "bearer " -- so both the correct "Bearer <key>"
        form and the raw "<key>" form authenticate. Safe because this server only
        uses static tokens: the rewrite just supplies the scheme the verifier wants
        and the wrong key still fails downstream (401).

        This MUST wrap the finished app as the OUTERMOST ASGI layer: FastMCP inserts
        the auth (RequireAuth) middleware BEFORE anything passed via http_app(middleware=),
        so a header rewrite handed to that param would run too late (after auth already
        401'd). Wrapping the returned app puts the rewrite ahead of auth.

        Being the outermost layer also makes this the one place that sees BOTH the
        request (already normalised) and the final response status for /mcp -- so it
        doubles as the observation point for tools.auth_stats: today's incident was
        Copilot Studio's stored key desyncing from MCP_API_KEY, causing every /mcp
        call to 401 with zero surfaced signal. record_response_start() below inspects
        the outgoing "http.response.start" ASGI event for status 401 on the /mcp path
        and calls tools.auth_stats.record_auth_failure(); everything is wrapped in
        try/except so a bookkeeping bug can never break the real request/response.

        The recorded IP is derived from the raw ASGI `scope` (peer address plus any
        X-Forwarded-For header) via tools.security.derive_identity() -- the SAME pure
        helper _parse_request() uses for unlock decisions. This module only has a
        scope dict, not a Starlette Request, but the derivation itself must not be
        reimplemented here: if this and the unlock gate ever computed the IP
        differently, the recorded origin would not match the IP the unlock gate
        actually saw, making the data useless for anything an operator wants to do
        with it."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            is_mcp_path = False
            if scope.get("type") == "http":
                new_hdrs = []
                changed = False
                for (k, v) in (scope.get("headers") or []):
                    if k.lower() == b"authorization":
                        val = v.strip()
                        # Only rewrite when a value is present and it doesn't already
                        # carry a bearer scheme (any casing: "Bearer", "bearer", ...).
                        if val and not val.lower().startswith(b"bearer "):
                            v = b"Bearer " + val
                            changed = True
                    new_hdrs.append((k, v))
                if changed:
                    scope = dict(scope)
                    scope["headers"] = new_hdrs
                is_mcp_path = (scope.get("path") or "").startswith("/mcp")

            if not is_mcp_path:
                await self.app(scope, receive, send)
                return

            async def _send_and_observe(message):
                # Observe the response status BEFORE forwarding it -- never delay or
                # alter the real response. Any bookkeeping failure here must not
                # prevent `send` from being called.
                try:
                    if message.get("type") == "http.response.start" and message.get("status") == 401:
                        from tools.auth_stats import record_auth_failure
                        from tools.security import derive_identity

                        # Raw ASGI scope, not a Starlette Request: pull the peer
                        # host and the raw X-Forwarded-For header value by hand,
                        # then hand both to the SAME derivation _parse_request()
                        # uses, rather than guessing at the IP independently here.
                        client = scope.get("client")
                        peer_host = client[0] if client else ""
                        xff_value = ""
                        for (hk, hv) in (scope.get("headers") or []):
                            if hk.lower() == b"x-forwarded-for":
                                xff_value = hv.decode("latin-1")
                                break
                        _, identity_ip = derive_identity(peer_host, xff_value)
                        record_auth_failure(ip=identity_ip)
                except Exception:
                    pass
                await send(message)

            await self.app(scope, receive, _send_and_observe)

    app = mcp.http_app(path="/mcp", transport="streamable-http",
                       json_response=True, middleware=[Middleware(_AcceptBoth)])

    # Wrap OUTERMOST so the raw-key -> "Bearer <key>" rewrite happens before FastMCP's
    # auth middleware sees the request (see _BearerPrefix docstring). Lifespan events pass
    # straight through, so the inner Starlette startup/shutdown still runs under uvicorn.
    app = _BearerPrefix(app)

    uvicorn.run(app, host="127.0.0.1", port=8000, timeout_graceful_shutdown=30)
