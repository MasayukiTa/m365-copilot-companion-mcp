# trace_view: a read-only trace viewer for tool-call sessions

When an agent conversation goes wrong, the question is always the same: what did this session
call, in what order, where was it idle, what was refused, and did it ever hand the work over?
The answers already exist in the server's own append-only records, but they sit in four files
and have to be joined by hand. `trace_view` joins them and renders one timeline per session.

It never writes to the state directory, never contacts the server, and uses only the standard
library (plus `openpyxl`, already a requirement, for the Excel output).

## What it reads

All files live in the server's state directory (the directory of the tool ledger; override with
`--state-dir` or the `TRACE_VIEW_STATE_DIR` environment variable). A one-generation rotated copy
(`<name>.1`) is read as well when it exists.

| File | Written by | Used for |
| --- | --- | --- |
| `tool_events.jsonl` | `tools/tool_ledger.py` | one `call` row and one `outcome` row per tool call, joined by `id` |
| `mcp_inbound.jsonl` | `tools/obs_log.py` | JSON-RPC arrivals (`initialize`, `tools/list`, `tools/call`, ...) |
| `lock_refusals.jsonl` | the unlock gate | lock refusals and unlock grants, by session |
| `fleet_submit_refusals.jsonl` | `tools/obs_log.py` | why a `fleet_submit` was refused |
| `send_stage.jsonl` | the bridge (optional) | shown as records not tied to a session |

Files are streamed, never loaded whole: the loader bisects on byte offset to the start of the
window (the files are time ordered) and skips rows with a cheap timestamp check before parsing.
A missing file, a damaged line or a row of the wrong shape is counted in the "load status"
table and otherwise ignored.

## What it shows

* **Per session**: every step (time, tool, redacted arguments, outcome, duration, result head),
  in order, with the interval since the previous step.
* **Gaps**: an idle stretch is measured from the end of the latest-ending earlier call to the
  start of the next one (default threshold 120 s). Each gap names the calls on both sides and
  how many calls other sessions made inside it. The printed length is the difference of the two
  clock times as displayed, so the label always agrees with the times beside it.
* **Refusal reasons**: `locked`, `env_protected` (a `.env` read), `protected_path`,
  `unknown_tool`, `missing_args`, `unexpected_args`, `duplicate`, `limit_exceeded`,
  `control_message`.
* **fleet_submit**: queued or refused (with reason, from the result text and the refusal log).
* **Findings**: gaps; a lock refusal in a session that never called `fleet_submit`; a session
  that connected (`initialize` / `tools/list`) and never called a tool; calls with no outcome;
  the same refusal three or more times in a row; `tools/call` arrivals with no ledger rows.
* **Statistics** by tool, by outcome and by refusal reason; serial or parallel execution.

## Redaction rules

Every string passes through `redact.py` before it reaches any output, whatever the source field:

* An argument whose name looks secret (`password`, `token`, `unlock_token`, `api_key`,
  `authorization`, `secret`, `credential`, `cookie`, ...) is printed as `[redacted]`: no head,
  no length, no hash.
* Any other argument is printed as `head (len N, #hash8)`. The head is at most 8 characters and
  at most one third of the value, so a short value is never shown whole. Booleans and small
  integers are shown. Path-like arguments (`path`, `pattern`, `working_dir`, ...) are shown up
  to 48 characters.
* A result is shown as an 80-character head. When any argument mentions `.env`, or the tool's
  result is a secret by nature (`unlock`, `office_password_recovery`, `clipboard_get`, ...), the
  result is `[withheld]`.
* `key=value` pairs with a secret-looking key, upper-case `NAME=value` assignments (the shape
  of an environment file), bearer tokens, well-known token prefixes, JWTs, long mixed
  letter/digit runs and e-mail addresses are replaced everywhere they occur.
* Client addresses are never printed.

Redaction is conservative on purpose: when in doubt it shows less. It is a second line of
defence; the ledger already refuses to store secret values for arguments it recognises.

## Usage

```
python -m tools.trace_view list   --since "2026-10-09 18:00" --until "2026-10-09 19:30"
python -m tools.trace_view --since "2026-10-09 18:00" --until "2026-10-09 19:30" \
       --out ./trace_out --format md,html,xlsx
python -m tools.trace_view --since ... --until ... --session 6a7c --gap 60 --tz UTC --lang en
```

Times are JST by default (`--tz` accepts `JST`, `UTC`, `+09:00` or an IANA name). Without
`--out`, Markdown goes to standard output. `--lang ja|en` selects the labels.

The HTML file is self-contained (CSS only, no scripts, no external resources): a time axis per
session with one bar per call (length = duration, colour = outcome), hatched gap blocks, and the
step table. The Excel file has sheets for sessions, steps, gaps, findings and per-tool
statistics, with conditional formatting on outcome, gap length and failure counts.

### MCP tool

`trace_session(since, until, session="")` returns a Markdown summary (JST, capped in size). It
returns a history of what callers did, so it is gated by `require_unlocked`. It is excluded from
the benchmark worker tool set.

## Limits

* **It only sees what the server recorded.** A request that never reached the server, a client
  that failed before or between calls, a connection dropped during `initialize`, or a client
  that simply did not call anything leaves no row. Such a failure shows up only as an absence
  (a gap, or a session that appears and disappears), never as an event. `mcp_inbound.jsonl`
  narrows this for connections made after it was introduced; older windows have no connection
  data at all, and the load-status table says so (zero rows).
* Session ids are hashes of the transport session. A client that opens a new transport session
  per turn appears as many short sessions.
* A call that was in flight when the window ended has no outcome yet and is listed as such.
* Redacted heads are short on purpose, so a trace identifies which file or command was involved
  but not its full content; use the ledger itself, with its own access rules, for that.
