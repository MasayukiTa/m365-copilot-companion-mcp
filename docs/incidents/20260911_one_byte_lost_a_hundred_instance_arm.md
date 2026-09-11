# 2026-09-11 — One byte in one git diff threw away sixty solved instances

An n=100 SWE-bench A/B died three chunks in, having solved 60, because a patch contained a byte
that cp932 cannot decode. Fixed in `4ef0d31`; the same defect was then swept out of the grade
path in `1c83939` before the rerun reached it.

## What happened

```
[2026-09-11 20:27:54] fleet exited rc=0
Exception in thread Thread-183 (_readerthread):
  UnicodeDecodeError: 'cp932' codec can't decode byte 0x9c in position 370
Traceback (most recent call last):
  File "bench\swe_solve_decoupled.py", line 259, in main
    ne = capture(ch)
  File "bench\swe_solve_decoupled.py", line 152, in capture
    if diff.strip():
AttributeError: 'NoneType' object has no attribute 'strip'
[20:27:56] ON solve did not reach its done marker (rc=1); aborting
```

The offending line:

```python
diff = subprocess.run(["git", "-C", wt, "diff"], capture_output=True, text=True,
                      timeout=60).stdout
```

`text=True` with no `encoding=` decodes with `locale.getpreferredencoding()` — cp932 on this
machine. A patch is arbitrary bytes.

## The chain, and why the existing guard could not help

1. One byte (`0x9c`) is not valid cp932.
2. That killed subprocess's reader **thread**, not the call.
3. `run()` returned **normally**, with `.stdout` set to `None`.
4. The parent therefore saw **no exception at all** — the `except Exception` wrapped around the
   call never fired.
5. `diff.strip()` raised `AttributeError`.
6. The orchestrator exited `rc=1`; `loop.py` aborted the whole arm.

The failure mode is the worst available: not a mangled character, but the loss of every instance
in the run.

## It had already been solved here

`tools/code_exec._decode` tries UTF-8, falls back to the local codepage with `errors="replace"`,
and never raises. `tests/test_child_output_decoding.py` exists for exactly this class and states
the rule: 「出力の一部が化けるより、出力ごと消えるほうが困る」.

This caller was never swept. The fix uses that helper rather than adding a second implementation,
and fixes **both halves** — reading bytes stops the raise, and `(diff or "")` stops a `None` from
any other path reaching `.strip()`.

## The sweep found it waiting in the grade path

Fixing only the solve caller would have left the identical line where the rerun goes next.

* **`bench/swe_check_remote.py:147`** — the same call on the same data.
* **`bench/swe_check_remote.py:92` (`_ssh_ps`)** — worse, and the one the batch grade actually
  calls (`swe_grade_swebench.py:87` and `:152`). It already guarded with `(r.stdout or "")`, so a
  decode failure does **not** crash. It returns `""`, retries three times, returns `""` again,
  and the caller reports **zero real verdicts** — which `loop.py` logs as *"the eval host
  unreachable / scp failed"* and aborts the arm. A host answering perfectly would be recorded as
  down, and hours of solving discarded on a misdiagnosis. **A silent wrong answer is worse than
  the crash that started this.**

Verified against the real eval host after the change: `_ssh_ps` returned `grade-path-probe-ok` in
5.8s, and the decoder round-trips UTF-8 (U+691C U+67FB) while replacing the incident byte instead
of losing the output.

The rerun then graded the ON arm **100/100 with zero EVALERR** — 29 RESOLVED — through the code
path that would previously have reported an unreachable host.

## Not fixed here

A repo-wide sweep found **68 more** `subprocess.run(..., text=True)` calls with no `encoding=`
and no `errors=`, across `bench/`, `relay/`, `tools/` and `scripts/`. Most read ASCII (git SHAs,
version strings) and are harmless in practice; the two fixed here read a patch and a remote
host's output. The rest are deliberately untouched: a mechanical 68-site edit does not belong in
the same change as the fix for the ones that actually failed, and it deserves its own review.

## Nothing was burned

`loop.py`'s infra-abort guard returns before `burned.add`, so the slice of 100 stayed fresh and
the 60 captured predictions stayed on disk. The rerun resumed at `52/100 remaining (captured 48)`
rather than starting over.

## What this cost, and what found it

The run was dead for **an hour** before anyone noticed, and the check that should have caught it
made things worse: a PowerShell process query filtered `CommandLine -match 'selfimprove.loop'`,
and the checking command's own command line contained that string, so it **matched itself** and
reported two live processes for a run that had already exited.

The watcher could not see it either. It emitted only when a counter changed, and a dead run
produces no change at all — identical to a quiet one. It was rebuilt to carry liveness as its own
counter (firing `*** RUN IS GONE ***` on the transition), a 30-minute heartbeat, an abort counter
read from the solve log, and a process query that excludes its own PID and never spells the match
pattern literally.

Silence is not success.
