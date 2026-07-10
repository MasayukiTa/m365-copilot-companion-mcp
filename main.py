import os

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
    gate_answer,
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
        "m365-copilot-companion-mcp toolkit. Use these tools to read, search, edit, "
        "and inspect local project files; run bounded Python or shell commands; work "
        "with CSV/Excel/JSON; generate PowerPoint decks and diagrams; manage "
        "long-running background jobs; verify your own outputs via read_image and "
        "pptx_export_png; and install Python dependencies when needed. Read-only tools "
        "are available after token authentication; mutating or execution tools require "
        "unlock(password) per IP. Call list_my_tools to see the full catalog. "
        "Relative user-folder names (Desktop, Documents, Downloads, ...) resolve to the "
        "user's home profile, not the server's working directory. If a file or folder "
        "seems missing, use find_files (recursive name search) before concluding it is "
        "absent, and never claim a path is outside the allowed base unless a tool call "
        "actually returned that error."
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
    gate_ask, gate_poll, gate_answer, gate_list,
    stop_request, stop_check, stop_clear,
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
    def call_tool(name: str = "", arguments: dict = None):
        """Gateway to EVERY tool on this server. This client caps the tool list, so rarely-used
        tools are reached through this one. Call call_tool(name="") to LIST all tools (name,
        signature, one-line summary); then call_tool(name="<tool>", arguments={...}) to invoke
        one. The target tool's own auth/unlock still applies.

        Args:
            name: the tool's name (e.g. "odbc_query"). Empty or "?" lists every tool.
            arguments: dict of the target tool's keyword arguments.
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
        try:
            return fn(**(arguments or {}))
        except Exception as _e:
            return "[call_tool %s error: %s: %s]" % (name, type(_e).__name__, _e)

    # critical tools FIRST (survive a front-biased truncation), then fill from the existing order
    _PRIORITY = (unlock, list_unlocked, call_tool, list_my_tools, env_info)
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
    _pn = {getattr(f, "__name__", "") for f in _PRIORITY}
    _want = [n.strip() for n in os.environ.get("MCP_TOOL_MAP_INCLUDE", "").split(",") if n.strip()]
    _pinned = [_ALL_TOOLS[n] for n in _want if n in _ALL_TOOLS and n not in _pn]
    _pinned_n = _pn | {getattr(f, "__name__", "") for f in _pinned}
    _rest = [t for t in TOOLS if getattr(t, "__name__", "") not in _pinned_n]
    _head = list(_PRIORITY) + _pinned
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


if __name__ == "__main__":
    _install_faulthandler()
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
        try/except so a bookkeeping bug can never break the real request/response."""

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
                        record_auth_failure()
                except Exception:
                    pass
                await send(message)

            await self.app(scope, receive, _send_and_observe)

    app = mcp.http_app(path="/mcp", transport="streamable-http",
                       json_response=True, middleware=[Middleware(_AcceptBoth)])

    # OpenAI-compatible chat-completions surface (additive, FLAG-GATED).
    # Mounts POST /v1/chat/completions + GET /v1/models on this SAME uvicorn app
    # ONLY when OPENAI_COMPAT=1; otherwise it is a no-op and /mcp is unchanged.
    # Lets any OpenAI-API harness use the Copilot-routed Opus 4.8 as its backend.
    # NOTE: must run on the raw Starlette `app` (needs app.router.routes) BEFORE the
    # _BearerPrefix wrap below turns `app` into a bare ASGI callable.
    from relay.openai_adapter import register_openai_routes
    if register_openai_routes(app):
        print("[main] OpenAI-compat routes mounted: POST /v1/chat/completions, GET /v1/models")

    # Wrap OUTERMOST so the raw-key -> "Bearer <key>" rewrite happens before FastMCP's
    # auth middleware sees the request (see _BearerPrefix docstring). Lifespan events pass
    # straight through, so the inner Starlette startup/shutdown still runs under uvicorn.
    app = _BearerPrefix(app)

    uvicorn.run(app, host="127.0.0.1", port=8000, timeout_graceful_shutdown=30)
