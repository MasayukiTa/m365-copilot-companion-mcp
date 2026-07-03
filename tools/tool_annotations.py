"""MCP tool-annotation hints (MCP spec §21.5.3 / §21.6.2).

The spec defines four boolean hints a tool can attach so an MCP HOST can drive its
consent UI without having to understand the tool's implementation:

  readOnlyHint    - host MAY auto-approve (tool never modifies its environment)
  destructiveHint - host SHOULD require explicit confirmation (may be irreversible)
  idempotentHint  - safe to retry (repeating the call with the same args has no
                    additional effect beyond the first application)
  openWorldHint   - the tool talks to something outside this local process/host
                    (network, external service) as opposed to purely local state

This repo previously attached NONE of these to any of its ~138 tools, so every
connected host must always prompt for every tool -- including read-only ones like
read_file or sqlite_query. That defeats the purpose of the hints.

DERIVATION STRATEGY
--------------------
readOnlyHint is derived MECHANICALLY by static source inspection (see
`derive_read_only_hints` below): a tool is read-only iff its function body does not
call `require_unlocked(`. This is reliable because the repo's uniform convention
(confirmed by reading tools/security.py and every mutating module: file_ops,
code_exec, jobs, coding_ops, outlook_ops, odbc_ops, process_ops, schedule_ops,
watcher_ops, memory_ops, archive_ops writers, etc.) is an inline `require_unlocked()`
call at the top of every mutating/executing tool function. Gated => not read-only.
Ungated => read-only. This is the single biggest, highest-confidence win because
hosts commonly auto-approve tools flagged readOnlyHint=True.

destructiveHint / openWorldHint / idempotentHint CANNOT be reliably derived from
`require_unlocked()` alone -- that gate distinguishes "mutates local state" from
"pure read", not "irreversible" from "reversible", nor "local" from "external
service". Those three hints are therefore a SMALL, EXPLICIT, hand-curated table
below, built by reading each mutating/external tool's actual behavior. Coverage is
intentionally partial: when unsure, a hint is simply OMITTED (absent = unknown,
which is the safe default) rather than guessed. Do not blanket-set these hints.
"""
from __future__ import annotations

import inspect
from typing import Callable

# ---------------------------------------------------------------------------
# Step 1: mechanical readOnlyHint derivation
# ---------------------------------------------------------------------------


def _calls_require_unlocked(fn: Callable) -> bool | None:
    """Return True if `fn`'s source contains an inline require_unlocked( call.

    Returns None (unknown) if the source can't be inspected at all (e.g. a
    builtin, a C-extension-backed callable, or a dynamically generated
    function) -- callers must treat None as "don't set readOnlyHint" rather
    than guessing either way.
    """
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return None
    return "require_unlocked(" in src


def derive_read_only_hints(tools) -> dict[str, bool]:
    """For each tool in `tools`, return {name: readOnlyHint}.

    A tool with NO require_unlocked() call anywhere in its body is read-only
    (readOnlyHint=True). A tool that calls it is not (readOnlyHint=False).
    Tools whose source cannot be inspected are OMITTED from the returned dict
    (conservative default: no hint set at all, rather than assuming either
    value) -- see GETSOURCE_FAILURES for which ones, populated as a side
    effect of the most recent call.
    """
    hints: dict[str, bool] = {}
    GETSOURCE_FAILURES.clear()
    for fn in tools:
        name = getattr(fn, "__name__", None)
        if not name:
            continue
        gated = _calls_require_unlocked(fn)
        if gated is None:
            GETSOURCE_FAILURES.append(name)
            continue
        hints[name] = not gated
    return hints


# Populated by the most recent derive_read_only_hints() call: tool names whose
# source could not be inspected (getsource failed), so readOnlyHint was left
# unset for them rather than guessed. Reported by callers (e.g. main.py /
# the verification script) for visibility.
GETSOURCE_FAILURES: list[str] = []


# ---------------------------------------------------------------------------
# Step 2: small, explicit override table for destructive / openWorld / idempotent
# ---------------------------------------------------------------------------
# Each entry documents its own reasoning with a short comment. Only tools where
# these hints are confidently TRUE are listed -- everything else is left absent
# (unknown) rather than defaulted to False, per the book's guidance that absent
# is the safe/honest state when unsure.

TOOL_ANNOTATION_OVERRIDES: dict[str, dict[str, bool]] = {
    # --- filesystem: irreversible or overwrite-capable local mutation ---
    "delete_path": {"destructiveHint": True, "idempotentHint": True},  # unlink/rmtree, not recoverable
    "write_file": {"destructiveHint": True, "idempotentHint": True},  # silently overwrites existing content
    "move_path": {"destructiveHint": True},  # can overwrite destination (overwrite=True); not idempotent (source gone after first call)
    "copy_path": {"idempotentHint": True},  # overwrite=True re-copy converges to same result; not marked destructive (dest overwrite is opt-in and non-fatal, source untouched)
    # trash_path is explicitly recoverable (send2trash / Recycle Bin) -- NOT destructive.

    # --- code / shell execution: irreversible side effects, talks to the OS/world ---
    "run_python": {"destructiveHint": True, "openWorldHint": True},  # arbitrary code: can touch network, filesystem, processes
    "shell_exec": {"destructiveHint": True, "openWorldHint": True},  # arbitrary shell command, same reasoning
    "pwsh_exec": {"destructiveHint": True, "openWorldHint": True},  # arbitrary PowerShell
    "pwsh_exec_file": {"destructiveHint": True, "openWorldHint": True},  # arbitrary PowerShell script file
    "run_in_background": {"destructiveHint": True, "openWorldHint": True},  # fire-and-forget shell, same class as shell_exec
    "run_python_in_background": {"destructiveHint": True, "openWorldHint": True},  # fire-and-forget Python, same class as run_python
    "job_kill": {"destructiveHint": True},  # terminates a running background job; now require_unlocked-gated (matches process_kill)

    # --- process control ---
    "process_kill": {"destructiveHint": True},  # kills an arbitrary OS process, local only

    # --- git: local repo mutation ---
    "git_commit": {"idempotentHint": False},  # each call creates a new commit object -- explicitly NOT idempotent (avoid a wrong default elsewhere)
    "git_checkout": {},  # mutates working tree / can create a branch; behavior is reversible (git history intact) so destructiveHint omitted -- not confident enough to mark

    # --- Outlook: local COM automation, but sending mail/invites is an external, irreversible act ---
    "outlook_send_mail": {"destructiveHint": True, "openWorldHint": True},  # send_immediately=True actually delivers mail off-host (gated by its own confirm= param); draft-only calls are the common case, but the hint reflects the tool's most consequential mode
    "outlook_create_event": {"openWorldHint": True},  # send_invite=True notifies external attendees; not marked destructive (event/invite can be cancelled)

    # --- ODBC: local write helper vs. remote database query ---
    # odbc_query is read-only (no require_unlocked call) AND open-world (it queries a
    # remote/corporate DB). readOnlyHint + openWorldHint is a valid MCP combination
    # (reads, no side effects, but reaches an external service), so both are set.
    "odbc_query": {"openWorldHint": True},  # read-only query against a remote/corporate DB
    "odbc_to_excel": {"openWorldHint": True},  # queries a remote database AND writes a local file (gated, not read-only)

    # --- web: read-only BUT open-world (they reach external hosts over the network) ---
    # readOnlyHint + openWorldHint is valid and correct here -- no side effects, but the
    # host should know these leave the machine. Set both (readOnly is derived; openWorld here).
    "web_fetch": {"openWorldHint": True},  # fetches an arbitrary URL over the network
    "github_file": {"openWorldHint": True},  # fetches a file from GitHub over the network
    "web_search": {"openWorldHint": True},  # queries a web search engine
    "web_search_news": {"openWorldHint": True},  # queries a web news search engine

    # --- scheduling: mutates the Windows Task Scheduler, a system outside this process ---
    "schedule_create": {"openWorldHint": True},  # registers a task with the OS Task Scheduler service
    "schedule_delete": {"destructiveHint": True, "openWorldHint": True},  # removes a scheduled task; irreversible (task definition is gone)
    "schedule_run_now": {"openWorldHint": True},  # triggers the OS Task Scheduler to run a task immediately

    # --- notifications: fires a desktop toast via the OS, not reversible / not idempotent-meaningful ---
    "notify_desktop": {"openWorldHint": False},  # local OS notification only; explicit False to record it was considered, not an external service
}


def _validate_overrides(read_only: dict[str, bool]) -> list[str]:
    """Consistency checks (book's requirement): a tool cannot be BOTH
    readOnlyHint=True and destructiveHint=True (destructive implies side effects),
    and every destructiveHint=True override MUST correspond to a
    require_unlocked()-gated tool (readOnlyHint False).

    Note: readOnlyHint + openWorldHint is a VALID, non-contradictory combination in
    the MCP spec -- a tool can read data (no side effects) from an EXTERNAL service
    (e.g. web_fetch, a remote DB query). The two hints are independent, so the check
    below only forbids readOnly+destructive, never readOnly+openWorld.

    Returns a list of human-readable problem descriptions; empty list = all good.
    Raises AssertionError if any problem is found (fail loud, this is a correctness
    gate over hand-maintained data).
    """
    problems: list[str] = []
    for name, overrides in TOOL_ANNOTATION_OVERRIDES.items():
        ro = read_only.get(name)
        if ro is True and overrides.get("destructiveHint"):
            problems.append(
                f"{name}: marked readOnlyHint=True by derivation but override table "
                f"sets destructiveHint True -- contradiction (destructive implies side effects)"
            )
        if overrides.get("destructiveHint") is True and ro is not False:
            problems.append(
                f"{name}: destructiveHint=True in override table but tool is not "
                f"require_unlocked()-gated (readOnlyHint derived as {ro!r}, expected False)"
            )
    if problems:
        raise AssertionError(
            "tool_annotations consistency check failed:\n" + "\n".join(problems)
        )
    return problems


def build_annotations(tools) -> dict[str, dict]:
    """Compute the final per-tool annotations dict: {name: {hint: bool, ...}}.

    Merge rule: start from the mechanically-derived readOnlyHint, then layer in
    any hints from TOOL_ANNOTATION_OVERRIDES for that tool name. Runs the
    consistency check before returning (raises on contradiction -- this is
    static, hand-maintained data, so a contradiction is a bug to fix, not a
    runtime condition to swallow).
    """
    read_only = derive_read_only_hints(tools)
    _validate_overrides(read_only)

    result: dict[str, dict] = {}
    names = {getattr(fn, "__name__", None) for fn in tools} | set(
        TOOL_ANNOTATION_OVERRIDES
    )
    for name in names:
        if not name:
            continue
        ann: dict = {}
        if name in read_only:
            ann["readOnlyHint"] = read_only[name]
        ann.update(TOOL_ANNOTATION_OVERRIDES.get(name, {}))
        if ann:
            result[name] = ann
    return result
