# 2026-09-11 — An n=100 A/B captured nothing in 102 minutes

The watchdog hard-reset a **healthy** Edge four times, and each reset sent every unfinished goal
back to turn 1. Fixed in `d13dbd7`.

## What was observed

An n=100 SWE-bench A/B was started at 08:21. At 10:03, 102 minutes in:

| | |
|---|---|
| predictions captured by this run | **0 / 100** |
| fleet restarts | **3** (`run_id` `_a0` → `_a1` → `_a2` → `_a3`) |
| Edge hard resets | **4** |
| workers that had reached settle | 26 |
| disk | 12.1 → 9.5 GB |

Work *was* happening — 26 workers reached their acceptance-check green — and all of it was
being thrown away.

**The first status report on this run said it was "healthy."** It was based on 23 workers, turns
1–4, and a status.json updated 8 seconds earlier. Every one of those is a measure of *motion*.
The measure of *progress* — captures — was zero and was not looked at. That error is the reason
this document leads with the capture count.

## Root cause

The coordinator's own events, in order (`.fleet/coordinator_20260911_082928_p22152.log`):

```
[relay_fleet] w12: socket -> tab (ChatHubError: frame budget exhausted before completion)
[watchdog] fleet stalled 150s -> hard-resetting the Edge (no eval in flight -> wedged)
[recover] Edge context lost; resuming 16 goal(s) (attempt 1/3)
[relay_fleet] w6: socket -> tab (ChatHubError: frame budget exhausted before completion)
[watchdog] fleet stalled 150s -> hard-resetting the Edge (no eval in flight -> wedged)
[recover] Edge context lost; resuming 11 goal(s) (attempt 2/3)
[watchdog] fleet stalled 150s -> hard-resetting the Edge                          (x2)
[recover] Edge context lost; resuming 4 goal(s) (attempt 3/3)
```

Every stall is immediately preceded by a socket→tab fallback.

The mechanism comes from the code rather than the pattern. `_fall_back_to_tab` calls
`_open_fresh`, whose own comment allows **three navigation attempts of 45s plus a 25s composer
wait each — 210s — and "up to ~300s"** once a sign-in page has been surfaced, because a person
may be mid-MFA. It is synchronous on the single-threaded round-robin, so `status.json` cannot
advance while it runs.

`fleet_runner._watchdog_should_reset` calls a browser wedged when it has not advanced for
`stall_s` (150s) **and** no worker declares a bounded eval. Of every long blocking path in the
fleet, this one alone declared nothing. So a healthy Edge, doing exactly what it was told, read
as wedged; the reset raised `FleetContextLost`; and every unfinished goal restarted at turn 1,
straight back into the same condition.

### The amplification is the point

Fallbacks are rare: **86 across 3592 `worker_done` records — 2.4%** (`.fleet/socket_route.jsonl`).
That 2.4% was destroying 100% of a run's progress, repeatedly.

## The fix

`_declare_blocking(seconds)` / `_end_blocking()` on `RelayWorker`, and the fallback declares
`FALLBACK_OPEN_CEILING_S` (300s, taken from `_open_fresh`'s own documented bound) around the tab
open.

Three things it deliberately is **not**:

* **Not a bigger threshold.** 150s is the right question to ask of a browser doing nothing. The
  defect is that the fleet was doing something and did not say so, and `eval_busy_until` is
  exactly how that is said. A larger number would move the livelock and blind the watchdog to
  real wedges for longer.
* **Not `_mark_eval_busy`.** That helper also flips the card to `verifying` — a lie for a worker
  opening a tab — and declares the acceptance ceiling (≥1500s) rather than this operation's own
  bound.
* **Not silent on close.** `_end_blocking` flushes the snapshot, because a snapshot still
  claiming a finished blocking call is a worker vouching for a browser it stopped watching,
  which blinds the watchdog to a genuine wedge for the rest of the window.

The same helpers corrected a smaller instance of the same mistake made earlier the same day: the
settle-time tree hash declared `_eval_ceiling_s()` (≥1500s) for work measured at 7.3s idle and
29.8s under load. Fifty times the operation is not a declaration. It now declares
`TREE_HASH_CEILING_S`, taken from those measurements.

## Verification

`relay/test_tab_fallback_is_not_a_wedge.py` drives the watchdog's own pure decision function, so
the defect is pinned at the decision that made it rather than at a log line:

* the reproduction — an undeclared tab open at 210s returns `(True, "…no eval in flight…")`
* the fix — a declared window at 210s returns `(False, "…within eval deadline…")`
* the failsafe — an **expired** window past `EVAL_STALL_CEILING_S` still resets, so a
  declaration is not a blank cheque
* the honesty — declaring does not set `verifying`, and the window is flushed on open *and* close
* the wiring — comments stripped first, then `_open_fresh` must lie between declare and end

Removing the declaration turns it red again (revert–test–restore). `relay/` 2794 passed; CI green
on `d13dbd7`.

## Not fixed here

Whatever makes a socket turn give up is untouched, so fallbacks will keep happening at roughly
2.4%. What must no longer happen is a stall following one.

**An earlier draft of this section named `ChatHubError: frame budget exhausted before completion`
as "what triggers the fallback".** That was generalising from the two lines this run happened to
show. Counted across all 86 fallbacks in `.fleet/socket_route.jsonl`:

| count | reason |
|---|---|
| 33 | `could not open the socket: InvalidProxyStatus` |
| 19 | `ConnectionClosedError: no close frame received or sent` |
| 13 | unknown |
| 11 | `turn deadline exceeded before a completion frame` |
| 4 | `the backend declined the request: InternalError` |
| **2** | **`frame budget exhausted before completion`** |

The frame budget is 2 of 86. The dominant cause is the socket failing to open or dropping
(52 of 86, 60%). The fix in `d13dbd7` is unaffected either way — it is about what the fleet does
*during* a fallback, not about why one started — but the cause named here was wrong and a reader
would have gone looking in the wrong place.

**And the frame budget cannot currently be judged at all.** `chathub.py:506` sets
`max_frames=2000` and raises the moment a turn exceeds it, but the frame count of a turn is
recorded nowhere — not on the fallback record, not anywhere in the repository. There is no
denominator, so nobody can say whether 2000 is generous or tight. Adding `seen` to the record on
both the passing and failing paths is the prerequisite for touching that number; changing it
without one would be guessing.

## Two attribution errors made while diagnosing this

Both are the same shape — treating a correlation as a cause — and both are recorded because the
shape recurred three times in one day.

1. **The verification watcher reported `stalls=4` one minute after starting.** It selected the
   newest coordinator log by mtime, and the new fleet had not created one yet, so it was counting
   the *killed* run's four stalls. A verification that counts the incident it exists to disprove
   is worse than none. Fixed by scoping to logs created after the watcher started.

2. **A disk drop from 11.87 → 7.76 GB was attributed to the benchmark**, and the run was stopped
   for it. The actual writers, at exactly that time, were VS Code extension updates: `codex.exe`
   0.28 GB, `codex` (linux) 0.24 GB, the openai.chatgpt VSIX cache 0.38 GB, `claude.exe` 0.21 GB,
   `.codex/logs_2.sqlite` 0.17 GB — about 1.6 GB. The benchmark's own footprint is 0.92 GB, and
   its staging use is transient. Stopping was still the correct safety call; the attribution was
   not.
