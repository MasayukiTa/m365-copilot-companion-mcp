# Doctor explains an out-of-credits environment; quickstart.bat asks before the desktop shortcut

Two independent requirements on the new-PC doctor path and the first-run installer.

## (1) An out-of-credits environment is explained, and never counted as a product failure

### Hypothesis (verified)

When the Copilot Studio **environment** has no credits, the platform does not
return the structured error code `platform_errors.detect()` keys on; it returns
**plain prose** in place of an answer ("...out of credits..."). Left unclassified
that prose was scored like any other non-answer, i.e. as a product failure of the
agent, which is wrong: it is a capacity limit of the environment, not a defect of
the agent or this PC, and no retry can make credits appear.

### Fix (already on the branch, extended here)

- `relay/credits_exhausted.py` -- detects the plain out-of-credits reply and holds
  the one plain line (`MESSAGE`) the doctor path shows: *"The Copilot Studio
  environment is out of credits; ask the environment administrator ..."*.
- `relay/relay_fleet.py` -- on detect, settles the worker as
  `status="stuck", outcome="CREDITS_EXHAUSTED"`, carries `credits_exhausted.MESSAGE`
  as the worker `reason`, and notifies ("クレジット不足"). Non-retryable.
- `relay/turn_outcome.py` -- adds the `CREDITS` turn outcome.
- `relay/outcomes.py` -- `CREDITS_EXHAUSTED` is a closed-set member mapped to
  `stuck`, `excluded` from the miss totals at zero turns, fail-closed otherwise,
  and non-retryable.

The remaining gap this commit closes is the **cockpit**: a `CREDITS_EXHAUSTED`
worker is `status="stuck"`, and `Theme.CountsAsNeedsAttention` excluded only
`INFRA_STUCK`, so the out-of-credits lane was still tallied as needs-attention /
a product failure on both the header and the chat band.

- `ui/Theme.cs` -- `CountsAsNeedsAttention` excludes `CREDITS_EXHAUSTED` the same
  way it excludes `INFRA_STUCK`; this is the ONE shared definition both screens
  count through.
- `ui/FleetCockpit.WorkerState.cs` -- adds `IsCreditsExhausted`, and keeps it out
  of `IsOperatorAttention`.
- `ui/FleetCockpit.Card.cs` -- computes `isCredits`, gives it its own non-red
  pill ("クレジット不足" / "Out of credits") and shows the worker `reason`
  (= `credits_exhausted.MESSAGE`, the same plain line the doctor path prints)
  **verbatim**, with only an Open-conversation shortcut (no re-queue: retrying
  cannot assign capacity).

## (2) quickstart.bat asks once (default no) before the desktop shortcut, and remembers it

`quickstart.bat` sets `PROV_SHORTCUT=no` as the default, asks once with a
`[y/N]` prompt (default no), records the decision to
`.setup\convenience_provisioned` BEFORE the first `start_all` call, and only runs
`scripts/make_desktop_shortcut.ps1` when the answer is `yes`. The absence of a
decision is no longer read as consent. The existing `-Remove` switch of
`scripts/make_desktop_shortcut.ps1` is untouched (it still deletes the launcher
and records `shortcut=no`).

## Verification

```
python -m pytest -q \
  ui/test_credits_exhausted_is_not_a_product_failure.py \
  ui/test_status_labels_needs_human_only.py \
  tests/test_outcome_enum_closed.py tests/test_retry_sets_agree.py \
  relay/test_platform_error_replies.py relay/test_upstream_throttle.py \
  relay/test_canned_nonanswer_cause.py relay/test_stuck_cause_classifier.py
=> 108 passed, 2 skipped
```

## Tests added

`ui/test_credits_exhausted_is_not_a_product_failure.py` pins, reading the C# as
source text (WPF, not unit-compilable), in the style of the sibling
`ui/test_status_labels_needs_human_only.py`:

1. `CountsAsNeedsAttention` excludes `CREDITS_EXHAUSTED` beside `INFRA_STUCK`.
2. The cockpit gives it its own non-failure lane and shows the reason verbatim
   (`IsCreditsExhausted`, `isCredits`, `OneLine(reason)`).
3. It is carved out of `IsOperatorAttention`.
4. It is not in the C# retryable set.
5. The Python contract it mirrors holds (`scoring_of`
   `excluded` at zero turns, non-retryable).
