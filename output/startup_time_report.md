# Startup time report: launch -> listening on 127.0.0.1:8000

Branch: `deliver/mcp-startup-time`
Measured on this PC (Windows, Python 3.10), 2026-11-01.

## Goal

Cut the time between launching `main.py` and the server listening on
`127.0.0.1:8000`, without changing behaviour. The supervisor log
(`%TEMP%\m365-companion-supervisor.log`) reports "server not listening yet
(ConnectFailure); main.py alive N s after launch" for the whole of that gap.

## What dominated the gap

Almost all of the launch->listen gap is spent in `import main`: importing every
`tools.*` module and running all `@mcp.tool` registration, which finishes BEFORE
`uvicorn.run(...)` binds the port. `python -X importtime` (raw trace saved as
`importtime_after.txt`) showed the biggest costs were pandas (which pulls in numpy
and pyarrow), python-docx and python-pptx -- libraries used by only a few tools.

## The change (commit 7f864ec4)

Those three imports were moved out of module top-level and into the functions that
use them, in `tools/data_ops.py`, `tools/docx_ops.py`, `tools/pptx_ops.py`. The
tool list is unchanged: every tool still registers at import, so clients that read
the tool list see exactly the same set. The cost of pandas/docx/pptx now happens on
first USE of those tools, not at boot.

## Measured numbers: before vs after

`import main` wall-clock, fresh interpreter each time (sibling checkouts dropped
from sys.path, this worktree pinned), 3 runs each:

| run           | before (defer OFF) | after (defer ON) |
|---------------|--------------------|------------------|
| 1             | 22.221 s           | 15.273 s         |
| 2             | 17.944 s           | 14.366 s         |
| 3             | 25.670 s           | 13.249 s         |
| **median**    | **22.221 s**       | **14.366 s**     |

**Median improvement: 22.221 s -> 14.366 s, ~7.9 s faster (~35%).**

Heavy libraries present in `sys.modules` right after `import main`:

* before: `pandas, numpy, pyarrow, docx, pptx` (all loaded at import time)
* after:  none (all deferred to first use)

The "before" state is the parent of the defer commit (`7f864ec4~1`) for those three
files; it was reconstructed with `git show`, measured, and reverted -- the live
server on port 8000 was never started, stopped or restarted.

## Measurement variance

These runs share the PC with the live server, so absolute numbers vary run to run
(e.g. before 17.9-25.7 s). The direction is stable and large, and the mechanism is
unambiguous: after the change, pandas/numpy/pyarrow/docx/pptx are simply not
imported during startup.

## Registration stays before listening

All `@mcp.tool` registration runs during `import main`, which completes before
`uvicorn.run(...)` binds the socket. The order is preserved and is required: a
client may read the tool list immediately on connect, so registration MUST stay
before binding. The startup trace below confirms the order.

## Startup trace (commit 5d630461)

`main.py` records `_PROC_START = time.perf_counter()` at module top and, when the
env var `MCP_STARTUP_TRACE` is set, prints:

* `STARTUP_TRACE registration-complete <s>` -- at the start of `__main__`, after all
  imports and tool registration have run;
* `STARTUP_TRACE about-to-listen <s>` -- immediately before `uvicorn.run(...)` binds
  `127.0.0.1:8000`.

The gap between those two lines is the measured registration->listen delay on the
real boot path. With the env var unset there is zero output and one extra
`perf_counter()` read, so the default boot path is unchanged.

## Regression guards (in CI, hermetic)

* `tests/test_heavy_libraries_are_not_imported_at_startup.py` (commit 67750bc7):
  asserts each of the three tool modules, imported on its own, loads none of its
  heavy library.
* `tests/test_startup_imports_are_deferred.py` (commits 5d630461 / 6708fa4b):
  asserts `import main` (the whole startup image) loads none of
  pandas/numpy/pyarrow/docx/pptx/matplotlib. Verified red->green by re-adding a
  top-level `import docx` and watching it fail with `['docx']`.

Both are registered in `.github/workflows/ci.yml` and run on `ubuntu-latest`.
