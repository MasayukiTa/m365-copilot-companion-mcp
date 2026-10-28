"""One admission journal every intake route writes to -- the single receipt of a submission.

WHY THIS EXISTS. A goal reaches this machine's fleet by three routes and, before this file,
they left three different trails -- or none:

  * fleet_submit (tools/fleet_intake.py) wrote a job under .fleet/tasks/pending/<id>.json with
    an ``origin`` block, so its provenance survived; but there was no one place that said "a
    submission arrived at HH:MM:SS by THIS route from THIS caller" across routes.
  * The cockpit's "追加" button (ui/FleetCockpit.Submit.cs -> TryAddGoalsToLiveFleet) wrote an
    add_goal straight into <state_dir>/commands.d/ with NO origin at all, so who added it and
    by which route was lost the moment it left the window.
  * The cockpit's "開始"/"続き" (SpawnFleet) wrote a goals-file and never touched the queue,
    so a started run had no admission record distinct from the run itself.

The owner's instruction (2026-10-26) is that every route pass through ONE admission entry that
records the route and the submitter. This module is that entry. It is deliberately tiny and
never raises into a submission: a journal line is an after-the-fact record, exactly like
tools/fleet_intake._note_possible_duplicate, and a failed append is logged beside the journal,
not re-raised onto the caller that was only trying to submit work.

ONE WRITER SHAPE, APPEND-ONLY. Each call appends exactly one JSON object, newline-terminated,
to .fleet/intake_journal.jsonl. The fields are fixed so a reader (a report, the cockpit, an
audit) can join a journal line to .fleet/tasks/<...>/<jid>.json by ``jid`` and to history.json
by the same ``jid``:

  ts        float  -- wall clock at admission (time.time())
  at        str    -- the same instant, ISO-8601 local, for a human reading the file
  jid       str    -- the admission id the rest of the system already joins on
  route     str    -- which door: "mcp" | "cli" | "ui_add" | "ui_start" | "ui_resume" | ...
  submitter str    -- who, as short text: the origin.source / caller identity
  session   str    -- the caller's session id when it carried one (else "")
  goal      str    -- the goal text, trimmed to a bounded preview so the file stays readable

The journal NEVER decides anything. It does not admit, refuse, dedupe, or route. It only records
that an admission happened, so that a submission can be confirmed to have been received no matter
which route carried it -- the standing project rule that what cannot be confirmed does not count
as working.
"""

import json
import os
import time

#: Journal file name under the fleet state dir. One line per admission, append-only.
JOURNAL_NAME = "intake_journal.jsonl"

#: Where a failed append records why, rather than raising onto the submitter. Mirrors
#: tools/fleet_intake's dup_notes_errors.log: the exception is kept, never propagated.
JOURNAL_ERRORS_NAME = "intake_journal_errors.log"

#: The goal preview is bounded so one pasted document cannot bloat a line every reader scans.
#: Generous -- real goals run to a few hundred characters -- and it is a PREVIEW, not the record
#: of the goal: the full text lives in the job file this line's ``jid`` points at.
MAX_GOAL_PREVIEW = 2000

#: The routes this project knows today. NOT enforced -- an unknown route is still recorded, so a
#: new door added later leaves a trail before anyone updates this list. Documented so a reader
#: knows what the established values mean.
KNOWN_ROUTES = (
    "mcp",         # tools/fleet_intake.fleet_submit (an agent over the tunnel)
    "cli",         # relay/fleet_runner.py invoked from a command line
    "ui_add",      # cockpit "追加": add_goal into a live run
    "ui_start",    # cockpit "開始": a fresh SpawnFleet
    "ui_resume",   # cockpit "続き": SpawnFleetResume
    "adopt",       # a fresh runner adopting a pending add_goal command
)


def _state_dir(state_dir=None) -> str:
    """The fleet state directory to write under.

    Resolved lazily (not at import) so a test that points FLEET_STATE_DIR at a temp directory is
    honoured on every call, the same discipline relay/task_router.py keeps for FLEET_STATE_DIR.
    """
    if state_dir:
        return state_dir
    env = os.environ.get("FLEET_STATE_DIR", "").strip()
    if env:
        return env
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, ".fleet")


def journal_path(state_dir=None) -> str:
    """Full path of the admission journal under the resolved state dir."""
    return os.path.join(_state_dir(state_dir), JOURNAL_NAME)


def _goal_preview(goal) -> str:
    text = " ".join(str(goal or "").split())
    if len(text) > MAX_GOAL_PREVIEW:
        return text[:MAX_GOAL_PREVIEW] + "\u2026"
    return text


def _record_error(state_dir, exc) -> None:
    """Keep a failed append beside the journal. NEVER raises -- a journal that cannot be written
    must not become a second failure on top of the first."""
    try:
        path = os.path.join(_state_dir(state_dir), JOURNAL_ERRORS_NAME)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("%s\t%s: %s\n" % (
                time.strftime("%Y-%m-%dT%H:%M:%S"), type(exc).__name__, exc))
    except Exception:
        pass


def record(jid, route, submitter="", goal="", session="", state_dir=None, ts=None):
    """Append one admission line to the journal and return the record dict (or None on failure).

    NEVER RAISES. A submission is not failed by a journal that could not be written: the goal is
    already (or is about to be) on disk in the queue, and this line only witnesses that it
    arrived. A failed append is recorded in intake_journal_errors.log and swallowed.

    The arguments mirror what every route already has in hand:
      jid        the admission id (fleet_submit's uuid, the CLI's, or the command-derived one)
      route      a KNOWN_ROUTES value (unknown is still recorded, by design)
      submitter  origin.source / caller identity, as short text
      goal       the goal text (stored as a bounded preview)
      session    the caller's session id, when one was carried
    """
    now = time.time() if ts is None else float(ts)
    rec = {
        "ts": now,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "jid": str(jid or ""),
        "route": str(route or ""),
        "submitter": " ".join(str(submitter or "").split())[:200],
        "session": str(session or ""),
        "goal": _goal_preview(goal),
    }
    try:
        d = _state_dir(state_dir)
        os.makedirs(d, exist_ok=True)
        # Append-only, one object per line, utf-8 with no BOM and "\n" newlines so the file reads
        # the same on every platform -- the shape socket_route.jsonl and dup_notes.jsonl keep.
        with open(os.path.join(d, JOURNAL_NAME), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:
        _record_error(state_dir, exc)
        return None
    return rec


def read_all(state_dir=None):
    """Every admission line as a list of dicts, skipping anything unreadable. Never raises.

    A convenience for a reader (report/audit/test): the journal is small and append-only, so
    reading it whole is cheap. A torn or non-JSON line is skipped rather than failing the read,
    the same tolerance SubmittedTasks.ReadFiles keeps on the cockpit side.
    """
    out = []
    try:
        path = journal_path(state_dir)
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return out
    return out
