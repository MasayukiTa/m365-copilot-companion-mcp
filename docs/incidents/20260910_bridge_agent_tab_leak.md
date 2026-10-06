# INCIDENT 2026-09-10 — 69 orphaned Copilot tabs on the bridge's headless Edge

**Status**: root-caused, reproduced, fixed, verified. Two defects, one incident.

## What was observed

CDP `:9223` — the bridge's Edge (profile `copilot-bridge-edge`, launched `--headless=new`, no
window anybody can click) — was holding **71 pages, 69 of them on one identical URL**:

```
https://m365.cloud.microsoft/chat/?titleId=T_02140b8c-f551-675b-516a-4c7d2b08867e
```

which is exactly `MCP_IMPL_AGENT_URL`. At 16:17 the same port held **1** page. By 18:54 it held
**71**. The bridge's Edge was using 1238 MB.

The owner's argument is what settled the attribution, before any code was read: a headless
browser has no window, so nobody opened 69 tabs at a specific URL by hand. Only this repository
drives that browser.

## Why it took so long to see

**Renderer count is not tab count.** All 70 Copilot pages were same-origin, so Chromium shared
about 7 renderer processes between them. Every instrument that watched *processes* — including
several run during this incident — reported the bridge Edge as flat at 17 processes while the
page count went from 1 to 71. The only instrument that sees this is CDP `/json/list` counting
`type == "page"`, and page counts were sampled only until 16:17.

Nothing in `.fleet` or `.setup/logs` had ever recorded a page or process count over time, so
there is no history against which to date an earlier onset. "This never happened before" cannot
be verified from records that exist; only "no record shows it before today".

## The two defects

### 1. The producer: a failed reopen orphaned the tab it had just opened

`bridge/copilot_bridge.py`, `_find_or_open_agent` opens the tab **before** it navigates:

```python
pg = ctx.new_page()
pg.goto(url, wait_until="domcontentloaded")     # can raise
for _ in range(40):                             # composer wait, can raise
    pg.wait_for_timeout(1000)
```

Its only caller, `ensure_page_alive`, catches everything:

```python
except Exception as exc:
    logger.warning("agent page had closed and could not be reopened", exc_info=True)
```

The caller keeps **no reference** to the page the callee created, so a failure anywhere after
`new_page()` left a tab on the agent URL that nothing could ever close. One orphan per failure.

**The arithmetic that identified it**: in the accumulation window (16:17–18:54) `bridge.log`
recorded `agent page had closed and could not be reopened` **76 times**, against 69 surviving
tabs. No other candidate came close — the tool probe's own borrow/return was balanced (37 opens
against 33 `gave back the page` plus 8 `resident page released`), and the token capture ran only
7 times.

### 2. The cleaner was disarmed by the predicate that fails

`_close_duplicate_agent_tabs` exists for exactly this: close every other tab already on the
agent surface. It was gated on `_agent_tab_matches`, which requires a **live composer element**:

```python
if pg.locator(COPILOT_SELECTORS["composer"]).count() <= 0:
    return False
```

That is the right question for REUSE — "can I hand this tab to a conversation" — and precisely
the wrong one for CLEANUP. A headless browser holding scores of tabs discards their renderers,
and a discarded page answers `0`. **The tabs most in need of closing were the only ones the
cleaner could not see**, and each surviving orphan made the next renderer likelier to be
discarded: a positive feedback loop. The same failing predicate also drove the reuse path, which
is why the log said `no reusable agent tab found -- opened a new one` 37 times while 69 tabs on
that exact URL sat in the same context.

## The fixes

| Defect | Fix |
|---|---|
| Producer | `_find_or_open_agent` now closes the tab **it** opened if the attempt fails, then re-raises. A REUSED tab is never closed on that path — it may be somebody's live conversation, and the failure says nothing about who holds it. |
| Cleaner | `_close_duplicate_agent_tabs` decides on the URL (`_agent_tab_url_matches`), not on usability. It skips pages claimed by a live owner (`relay.ownership`), because the light token capture claims its page before navigating, and an unreadable ledger counts as claimed — not closing is the safe direction. |

`_agent_tab_matches` keeps the composer check: choosing a tab to REUSE still requires a usable
one.

## Verification, in the order the owner required: analysis → reproduction → fix

1. **Analysis** — the code paths and log arithmetic above.
2. **Cleared the evidence deliberately.** 71 stable pages prove nothing: a flat count can mean
   "the leak stopped" or "the browser cannot fit another page", and those are indistinguishable
   while the tabs remain. 68 tabs were closed via CDP (keeping `about:blank`, because **Edge
   exits with its last page** — measured previously: closing it took the browser down and the
   bridge with it). Pages went to 2, the bridge stayed up on `transport=socket`, and for the
   next 10 minutes `titleId` pages stayed at **0** — the leak needs a seed, which is what the
   feedback loop in defect 2 predicts.
3. **Reproduction** — `bridge/test_agent_tab_reuse_and_selfheal.py`, hermetic (no browser, no
   CDP, no network). Both defects were first pinned in their BROKEN form and observed to fail
   the moment the fix landed; the tests were then inverted to pin the fixed behaviour, keeping
   the measured evidence in their docstrings. 7 tests, including two guards that must survive
   any future change: the keep-alive `about:blank` is never closed, and a non-agent tab is never
   closed.
4. **No recurrence, on the running system** — the bridge was restarted onto the fixed code at
   20:09:31 and watched for 30 minutes at 1-minute resolution: page count, `titleId` page count,
   and the counters for `could not be reopened` / `closed the tab this attempt had just opened` /
   the cleaner's own line.

## Corrections made during the investigation, kept on the record

- **"The parent is explorer.exe, so it is not ours" is not an argument.** Chromium's Windows
  process singleton hands a launch request to an already-running browser for that profile and
  the launcher exits, so a browser can service another process's work while keeping its original
  parent, command line and creation time. What actually settled the separate default-profile
  question was that no launch path in this repository omits `--user-data-dir` /
  `--remote-debugging-port`, and a browser with neither cannot be driven by CDP or Playwright.
  (That refutation came from an adversarial review by an external model, requested by the owner.)
- **`token_via_light_page` was accused and is innocent.** Its `finally` calls `_release(page)`,
  and `_release` does call `page.close()`. Naming it as the leak was wrong, and fixing it would
  have been a change to working code.
- **"Renderer count is not tab count", and neither is Task Manager's "Microsoft Edge (N)"** —
  that is a grouping by executable. Tabs are `type == "page"` in CDP `/json/list`.

## The instrument that was missing

Both the incident and the impossibility of dating its onset come back to the same gap: nothing
recorded page counts over time. Process counts were sampled repeatedly and were flat by
construction. Any future watch on this class needs `/json/list` page counts per CDP port,
persisted — a process-level instrument cannot see it.
