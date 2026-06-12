import os

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import PlainTextResponse

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
from tools.memory_ops import memory_delete, memory_list, memory_load, memory_save
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
        "unlock(password) per IP. Call list_my_tools to see the full catalog."
    ),
)

@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> PlainTextResponse:
    """Liveness probe that NEVER touches a blocking tool.

    This async handler runs directly on the event loop and returns immediately, so the
    supervisor can distinguish "the loop is briefly busy running a heavy tool in a worker
    thread" (this still answers fast, because tool bodies are now offloaded) from "the
    loop is actually dead". It does no auth and no I/O on purpose."""
    return PlainTextResponse("ok")


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
    # memory (cross-session)
    memory_save, memory_load, memory_list, memory_delete,
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

for tool in TOOLS:
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
    mcp.run(
        transport="streamable-http",
        port=8000,
        path="/mcp",
        uvicorn_config={"timeout_graceful_shutdown": 30},
    )
