# FreshSubmitAmbiguous Audit — Production Code Review

- Date (UTC): 2026-10-01
- Scope: READ-ONLY audit of production code (this file is the only artifact written).
- Job: r6abe301b_a0_w0
- Question audited: Can `FreshSubmitAmbiguous` ever be converted back into an
  *automatic* resend of the same logical fresh user turn by any caller, recovery
  loop, rotation, timeout, or resume path?

## Verdict

No automatic path converts `FreshSubmitAmbiguous` into a resend. All **four**
production catch sites fail closed without auto-retry, auto-rotation, or
transient-retry. A resend of the same logical first turn is possible **only**
after an explicit human `--resume-runtime` action, which is exactly the "new
observable decision boundary" the exception's contract requires. One residual,
human-gated duplicate window is documented below and should be tracked.

## Exception contract

`relay/send_errors.py:8` — `FreshSubmitAmbiguous(RuntimeError)`:
a fresh submit may already have landed but its USER-turn receipt is ambiguous.
Callers MUST NOT automatically resend the same payload (retrying can duplicate a
user turn); stop/pause and require a new observable decision boundary instead.

## Complete reference map (case-sensitive grep over relay/*.py)

- Definition: `send_errors.py:8`
- Imports: `copilot_autopilot_relay.py:105`, `local_loop_controller.py:22`,
  `relay_fleet.py:40`
- Raised (1 production site): `copilot_autopilot_relay.py:1949`
  (also test doubles: `test_local_loop_controller.py:96`,
  `test_fresh_submit_ambiguity_is_not_retried.py:43`).
- Caught (3 production sites):
  - `copilot_autopilot_relay.py:2405` — `run_relay` single-conversation loop.
  - `relay_fleet.py:4426` — RelayWorker.
  - `local_loop_controller.py:1148` — LOCAL_LOOP controller.
- Tests pinning the properties: `test_fresh_submit_ambiguity_is_not_retried.py`,
  `test_fresh_composer_submission.py`, `test_local_loop_controller.py`.

(Correction to prior revision: the earlier draft listed only two catch sites and
miscounted "three call sites." There are three production catch sites — the
missing one was the `run_relay` loop at `copilot_autopilot_relay.py:2405`,
audited below. Its behaviour is fail-closed and does not change the verdict.)

## Raise site — copilot_autopilot_relay.py:1949 (CopilotWebDriver.send)

- Raised only when `fresh_conversation` is true, after a single submit action,
  when the response-independent USER-turn receipt is missing/mismatched.
- Structural property: the `raise` sits inside `for attempt in range(3):` but,
  being an exception, exits the loop immediately — send() never performs a second
  attempt/resend of the same fresh submit.
- Fresh turns deliberately bypass the continuation re-click path and wait only
  for the USER-turn receipt; a missing/mismatched receipt is treated as ambiguous
  and fail-closed.

## Catch site A — copilot_autopilot_relay.py:2405 (run_relay single-conversation loop)

- This is the loop that repeatedly calls `driver.send(job)` (line 2404) inside
  `while not max_turns or turn < max_turns:` (2397).
- Ordering is correct and load-bearing: `except FreshSubmitAmbiguous` (2405)
  precedes the generic transient-retry `except Exception` branch further down.
  The comment (2408-2409) states this loop previously resent the same job via
  that generic branch; the specific handler now intercepts it first.
- Behaviour (2414-2434): if normalized acceptance `checks_norm` exist, run them
  via `run_all_blocking`; if they pass -> outcome DONE (no resend needed); else
  -> outcome STUCK with reason "fresh submit delivery ambiguous; automatic resend
  forbidden". Both arms `break` out of the loop — no `continue`, no rotation, no
  retry. If `checks_norm` is empty, `_amb_passed` stays False -> STUCK.
- Pinned by test_fresh_submit_ambiguity_is_not_retried.py:12 (`amb` index found
  before the generic/retry markers; `assert amb < generic < retry`).

## Catch site B — relay_fleet.py:4426 (RelayWorker)

- Ordering is correct and load-bearing: `except FreshSubmitAmbiguous` (4426)
  precedes `except Exception` (4473). Since `FreshSubmitAmbiguous` subclasses
  `Exception`, reversing the order would route it into the generic handler, which
  is the one that calls `_retry_transient()`. The ambiguous branch does NOT call
  `_retry_transient()`.
- Behaviour: try `_salvage_via_checks()`; on success return (terminal DONE), else
  set status/outcome = stuck/STUCK with reason
  "fresh submit delivery ambiguous; not retried". No resend.
- `_salvage_via_checks()` (relay_fleet.py:5034) is read-only: it runs the same
  acceptance checks the DONE gate uses against the current workspace and either
  settles DONE (returns True) or returns False. It never resends `self.job`.

## Catch site C — local_loop_controller.py:1148 (LOCAL_LOOP)

- Ambiguous branch (1148-1158): records `UI_TRIGGER_AMBIGUOUS`, calls
  `mark_waiting_runtime(scope="campaign")`, returns WAITING_RUNTIME. It does NOT
  call `retry_uncommitted_turn` and does NOT call `_rotate` — distinct from the
  generic branch (1159) which does both and then `continue` (resend).
- Pinned by test_ambiguous_fresh_send_pauses_without_rotate_or_retry (357):
  result WAITING_RUNTIME and `rotations == []`.

## Recovery / rotation / timeout / resume paths

- No auto-resume: "WAITING_RUNTIME is an explicit pause and never auto-resumes"
  (local_loop_controller.py:1086); "WAITING_RUNTIME resumes only through explicit
  --resume-runtime" (:1439).
- Controller start does not implicitly resume: pinned by
  test_waiting_runtime_is_not_implicitly_resumed_by_controller_start, whose
  driver raises AssertionError("WAITING_RUNTIME must not send another browser
  RUN").
- `mark_waiting_runtime` (local_job_store.py:847) only flips the jobs row to
  WAITING_RUNTIME; it does NOT advance `current_seq` and does NOT mark the turn
  committed.
- `resume_runtime` (local_job_store.py:911) flips WAITING_RUNTIME -> READY at the
  SAME `current_seq`, expiring the stale lease so the next claim gets a higher
  fencing token "and makes any late commit harmless". It does NOT advance seq.
- Timeout/other transient paths in RelayWorker and run_relay are in the generic
  `except` branches, unreachable for FreshSubmitAmbiguous because the specific
  handler precedes each of them.

## Residual, human-gated duplicate window (track, do not block)

After an operator runs `--resume-runtime`, the controller reads the unchanged
`current_seq` and re-sends `RUN <job> seq=<same>`. The lease-expiry on resume
protects against a duplicate COMMIT (fencing), but NOT against a duplicate
BROWSER user turn: if the first turn actually landed in M365 without producing a
SQLite commit, resuming re-injects the same RUN into the same conversation,
producing a duplicate user turn. This is consistent with the exception contract
(resend only after a new human boundary) and is bounded, because
`UI_TRIGGER_ATTEMPT` is recorded before send and counted into `sent_attempts`,
which is restored from `ui_trigger_attempt_count` on resume and capped by
`max_attempts`.

- Severity: low-to-medium (requires the exact first-turn-landed-without-commit
  race AND an operator resume).
- Reproducibility: not automatically reproducible; needs the receipt-observation
  failure to coincide with an actual landed turn, then a manual resume.

### Recommendation

1. Keep current behaviour: no automatic path resends an ambiguous fresh submit;
   the four handler orderings and non-retry branches are correct.
2. Document the resume-time risk operationally: before `--resume-runtime` on a
   job paused by FreshSubmitAmbiguous, confirm the first user turn did not land
   in the M365 conversation; or add a per-seq "require_manual_ack" gate so resume
   of an ambiguous seq requires explicit confirmation rather than re-dispatching
   the same seq.
3. Preserve the handler ordering in copilot_autopilot_relay.py, relay_fleet.py
   and local_loop_controller.py; it is already fixed by
   test_fresh_submit_ambiguity_is_not_retried.py (`amb < generic < retry`).
   Do not reorder.

## Audit integrity

This audit was read-only over production code. No production file was modified.
The only write performed is this report file.

## Post-audit coordinator review (2026-10-01)

The independent agent audit above missed one production caller because its reference map searched the explicit `FreshSubmitAmbiguous` handlers that existed at the audited HEAD, not every `CopilotWebDriver.send()` caller that could catch the subclass through `except Exception`.

`relay/copilot_autopilot_relay.py::run_relay()` was such a caller. Before the follow-up fix, a fresh delivery ambiguity fell through its generic send-exception branch, incremented the transient budget, decremented the logical turn, and automatically re-sent the same `job`. A behavior regression reproduced the defect directly: with `max_transient=1`, the ambiguous driver received the same logical send twice.

The single-conversation relay now catches `FreshSubmitAmbiguous` before its generic transient branch. It never retries or rotates on that exception. If and only if explicit independent acceptance checks already pass, it salvages the run as DONE without another send; otherwise it stops as STUCK and records `fresh_submit_ambiguous` in the run log. Behavioral regressions assert both the one-send STUCK path and the one-send acceptance-salvage path. Fleet and LOCAL_LOOP retain their earlier fail-closed handlers.

This post-audit correction means the original `No automatic path converts FreshSubmitAmbiguous into a resend` verdict was not true for the audited HEAD, but is the intended invariant after the follow-up fix.
