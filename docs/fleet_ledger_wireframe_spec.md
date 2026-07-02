# Fleet Ledger Wireframe Spec
## Quiet Operations Ledger — Fleet & Main Screen Redesign

**Created:** 2026-06-28  
**Source direction:** `Desktop/agent_visual_direction.md`  
**Scope:** Three canonical states × two windows (Fleet Option A + Main Option B). No .cs edits. No commit.

---

## 0. Grounding Principles

Before any wireframe: this spec is _data-honest first_. Every displayed element is tagged:

- `[REAL]` — field exists in status.json or transcript jsonl today; can ship now.
- `[COMPUTED]` — derivable client-side from REAL fields; can ship now.
- `[FUTURE]` — requires a relay/backend contract change; **must not be faked** at ship time. Honest fallback is specified for each.

The direction doc's "4-level info architecture" maps to **always-visible / main area / one-click / hidden**. This spec enforces that boundary.

---

## 1. Palette Mapping

### New "Quiet Operations Ledger" tokens vs. current Theme.cs

| Role | Ledger Name | Ledger Hex | Current Theme.cs (light) | Delta | Recommendation |
|------|-------------|------------|--------------------------|-------|----------------|
| App background | Paper | `#F7F6F2` | `Bg()` → `#FAFAF8` | Very close (2pt warm shift) | **Reuse with tweak** — change `Bg(false)` to `#F7F6F2` |
| Content surface | Ledger | `#FFFFFF` | `Surface()` → `#FFFFFF` | Identical | Reuse as-is |
| Primary text | Ink | `#171717` | `Text()` → `#18181B` | 1pt off | **Reuse as-is** (imperceptible) |
| Secondary text | Graphite | `#3F3F46` | `Muted()` → `#71717A` | Significantly darker | **New token needed** — add `SecondaryText()` → `#3F3F46` |
| Dividers | Rule | `#D8D6CF` | `Border()` → `#E5E5E1` | Warmer / darker | **Reuse with tweak** — change `Border(false)` to `#D8D6CF` |
| Primary signal | Signal | `#D9480F` | `Accent()` → `#EA580C` | Close; Signal is cooler red-orange | **Replace** `Accent(false)` with `#D9480F`. Signal is ONE use: primary action only. |
| Active evidence | Live | `#2563EB` | `Info()` → `#2563EB` | **Identical** | Reuse as-is |
| Verified | Verified | `#15803D` | `Success()` → `#16A34A` | 1pt | **Reuse as-is** |
| Attention | Attention | `#B45309` | `Warning()` → `#D97706` | Darker/muted | **Prefer Ledger value** — darker reads better on Paper bg |
| Broken / stale | Dead | `#B91C1C` | `Danger()` → `#DC2626` | Darker | **Prefer Ledger value** |

### Recommended Theme.cs changes (minimal, non-destructive)

```
// In Theme.cs — light-mode token updates only; dark mode unchanged
Bg(false)      #F7F6F2    (was #FAFAF8)
Border(false)  #D8D6CF    (was #E5E5E1)
Accent(false)  #D9480F    (was #EA580C)
Warning(false) #B45309    (was #D97706)
Danger(false)  #B91C1C    (was #DC2626)

// New token — add to Theme.cs
public static string Secondary(bool d) { return d ? "#A1A1AA" : "#3F3F46"; }
```

**Do not** add new color tokens for states that already have them. The key constraint is: state lives in spine marks, small text chips, and the 3px left rail — NOT in broad fills. The existing `RailW = 3` constant is already correct for this.

---

## 2. Evidence Spine: Now vs. Proper

### Now (can ship immediately): `[COMPUTED]` Execution Timeline

The transcript jsonl gives us per-turn timestamps. The relay already writes turn `ts` (epoch) on every assistant and user turn. We can derive a coarse timeline without any backend change.

**What we render (honest label):**

```
EXECUTION TIMELINE  (from transcript turns)

  09:14  directive received         [COMPUTED from first user turn ts]
  09:14  first agent response       [COMPUTED from turn 1 assistant ts]
  09:27  still running              [COMPUTED from latest turn ts]
  09:31  status → verifying         [COMPUTED from status field change, polled]
  09:34  done                       [COMPUTED from status == "done"]
```

**Derivation:**
- Line 1 of transcript jsonl: `{meta:true, key, name, goal, ts}` → "directive received" timestamp `[REAL]`
- Each `{turn, role, text, ts}` line gives a per-turn timestamp `[REAL]`
- Status changes (`status` field in status.json) are polled every 700ms and can be logged client-side with a local timestamp `[COMPUTED]`
- Phase label is a client-side remap (see Phase Vocabulary below)

**Honest label rule:** Always render the spine section header as "Execution timeline" or "Turn activity" — never as "Phase log" or "Evidence trail" (those imply structured phase-event data that doesn't exist yet).

### Proper future: `[FUTURE]` Structured Phase Events

**Minimal backend addition required:**

Add a `phase_events` list to each worker entry in status.json, emitted by the relay's own state-machine transitions:

```json
"phase_events": [
  {"ts": 1751020440, "event": "queued",     "label": "Queued by fleet runner"},
  {"ts": 1751020441, "event": "started",    "label": "First turn dispatched"},
  {"ts": 1751020798, "event": "verifying",  "label": "Entered verification"},
  {"ts": 1751020831, "event": "done",       "label": "Verified and closed"}
]
```

**Critical property:** The relay already _knows_ these transition moments — it drives the status state machine. This requires **no agent cooperation** and **no inference**. The relay emits `{ts, event, label}` whenever it updates `status`. The label is a fixed string per transition, not generated text.

**Implementation location:** `relay/fleet_runner.py` — wherever `worker["status"]` is set, also append to `worker["phase_events"]`. Persist to status.json on next write.

Until `phase_events` is added to the backend, display the `[COMPUTED]` turn-timestamp timeline labeled honestly. The component renders either: if `phase_events` present → proper spine; if absent → "Execution timeline (from conversation turns)".

### Phase Vocabulary (status → display phase)

| Raw `status` `[REAL]` | Display phase `[COMPUTED]` | Spine marker color |
|-----------------------|----------------------------|--------------------|
| `pending` | Queued | Graphite (neutral) |
| `ready` | Starting | Live (#2563EB) |
| `waiting` | Running | Live (#2563EB) |
| `researching` | Researching | Live (#2563EB) |
| `refuting` | Reviewing | Live (#2563EB) |
| `verifying` | Verifying | Attention (#B45309) |
| `done` | Done | Verified (#15803D) |
| `stuck` | Needs attention | Attention (#B45309) |
| `maxturns` | Needs attention | Attention (#B45309) |
| `error` | Stopped (error) | Dead (#B91C1C) |
| `cancelled` | Stopped | Graphite |

---

## 3. Autonomy Contract Panel

Used as a pre-flight confirmation before a long-running delegation (e.g., overnight task via `--auto-mode`).

### Wireframe

```
╔══════════════════════════════════════════════════════╗
║  AUTONOMY CONTRACT                                   ║
╠══════════════════════════════════════════════════════╣
║                                                      ║
║  Directive                                           ║
║  ┌──────────────────────────────────────────────┐   ║
║  │ SWE-bench失敗85件を分類し修正案を明日朝まで  │   ║
║  └──────────────────────────────────────────────┘   ║
║                                                      ║
║  Scope                                               ║
║  repo: m365-companion  (from --folder arg)  [REAL]   ║
║                                                      ║
║  Allowed                                             ║
║  edit files · run tests · read docs      [REAL]      ║
║  (from relay permission policy)                      ║
║                                                      ║
║  Ask before                                          ║
║  publishing · deleting files · external  [FUTURE]    ║
║  ⚠ Not enforced at runtime — configured intent only  ║
║                                                      ║
║  Stop when                                           ║
║  tests pass / budget exceeded            [FUTURE]    ║
║  ⚠ Not enforced — fleet runs until done/stuck        ║
║                                                      ║
║  Acceptance checks                                   ║
║  (none specified in goal)                [REAL/partial]
║  If checks[] present: listed here verbatim           ║
║                                                      ║
║  Report                                              ║
║  Evidence summary on completion          [REAL]      ║
║  (outcome + last text from transcript)               ║
║                                                      ║
║  Effort:  [ auto ▾ ]   Approval: [ run ▾ ]          ║
║  (effort/approval dropdowns KEPT HERE — user pref)  ║
║                                                      ║
║  ┌────────────────┐   ┌──────────────────────────┐  ║
║  │   Cancel       │   │   Delegate overnight  →  │  ║
║  └────────────────┘   └──────────────────────────┘  ║
╚══════════════════════════════════════════════════════╝
```

### Contract Element Table

| Element | Tag | Source | Fallback |
|---------|-----|--------|----------|
| Directive text | `[REAL]` | `goal` field (worker) or composer input | — |
| Scope / folder | `[REAL]` | `cwd` field or `--folder` arg | "not specified" |
| Allowed actions | `[REAL]` | relay permission policy (hardcoded in fleet_runner) | Show relay defaults |
| Ask before | `[FUTURE]` | Not implemented — show as "configured intent, not enforced" | Grayed out with ⚠ |
| Stop when | `[FUTURE]` | Not implemented — show as "configured intent, not enforced" | Grayed out with ⚠ |
| Acceptance checks | `[REAL/partial]` | `checks[]` array on worker; often empty | "None specified" |
| Report format | `[REAL]` | `outcome` + last transcript text | "summary on completion" |
| Effort dropdown | `[REAL]` | `effort` from settings.txt | Default "auto" |
| Approval dropdown | `[REAL]` | `approval` from settings.txt | Default "run" |

**Critical note:** The effort/approval dropdowns stay in this panel exactly as they are in the current header. The direction doc says "this is the right place for approval design" — but the implementation constraint says KEEP the existing dropdowns and do not reorganize them in this spec iteration. The contract panel surfaces the same values in a more legible context; the header dropdowns remain authoritative.

---

## 4. State 1 — Idle / Ready for a Directive

### 4a. Fleet Window (Option A: Ledger-First)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Fleet                                   [Effort: auto▾] [⚙] [☰] [◑]│
│ No active delegation                                                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  DIRECTIVE                                                           │
│  ─────────────────────────────────────────────────────────────────  │
│  No directive set.                                                   │
│  Give a directive and the agent will split it into lanes,            │
│  run checks, and report evidence here.                               │
│                                                                      │
│  Suggested:                                                          │
│  [Analyze failures overnight]  [Implement and verify]  [Review diff] │
│                                                                      │
│                                                                      │
│  EVIDENCE SPINE          LANES                                       │
│  ──────────────          ────────────────────────────────────────── │
│  (empty — no run)        No lanes. Start a delegation above.         │
│                                                                      │
│                                                                      │
│                                                                      │
│                                                                      │
│                                                                      │
├─────────────────────────────────────────────────────────────────────┤
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Add tasks...                                                  │  │
│  │                                                               │  │
│  │  One goal per line  ·  "/" for commands          [Start →]   │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 4b. What is hidden (Idle state)

- Filter tabs (All / Active / Needs input / Done) — no data, no noise
- Evidence pane — nothing to show
- Lane count chips
- ETA / elapsed
- Pause / Stop fleet controls (no run active)
- Debug drawer

### 4c. Where intervention happens

Composer at bottom. Suggested directive chips are the only affordance. No buttons in the header beyond gear/theme/lang.

### 4d. Trust evidence displayed

None — honestly empty. No fake "ready" indicators.

### 4e. Element Table — Idle

| Element | Tag | Source field | Fallback |
|---------|-----|-------------|----------|
| "No active delegation" subtitle | `[COMPUTED]` | `running == false OR status.json absent` | Always shown |
| Suggested directives | `[FUTURE]` | Hardcoded quick-starts (not personalized) | Static chips — acceptable |
| Effort/Approval dropdowns | `[REAL]` | `effort`, `approval` from settings.txt | Defaults (auto / run) |

---

## 5. State 2 — Long-Running Delegation Active

Example: 4 lanes, mixed phases, W2 needs attention.

### 5a. Fleet Window (Option A: Ledger-First)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Fleet                 4 lanes · 1 needs attention · evidence 8s ago  │
│                       [Pause] [Stop all]  [Effort:auto▾] [⚙] [◑]   │
├─────────────────────────────────────────────────────────────────────┤
│  DIRECTIVE                                                           │
│  ─────────────────────────────────────────────────────────────────  │
│  SWE-bench 失敗 85 件を分類し、修正案を明日朝 08:00 までにまとめる  │
│  started 09:14 · 1h 43m elapsed · 3 / 4 lanes active               │
├─────────────────────────────────────────────────────────────────────┤
│  EVIDENCE SPINE               LANE BOARD                            │
│  │                            ──────────────────────────────────── │
│  ● 09:14  directive recv'd    W0  cause-clustering                  │
│  │                                Running  ·  turn 14/40           │
│  ● 09:14  lanes dispatched         last: "grouped 32 cases by       │
│  │                                 fixture type"  ·  19s ago       │
│  ● 09:27  W0 researching      ──────────────────────────────────── │
│  │                            W1  scaffold-patch                    │
│  ● 09:41  W1 verifying             Verifying  ·  turn 28/40        │
│  │                                 last: "running pytest on 12      │
│  ● 10:44  W2 needs attention        modified files"  ·  8s ago     │
│  │        browser unresponsive ──────────────────────────────────── │
│  ● 10:57  now                 W2  harness-verification         ⚠   │
│                                    Needs attention  ·  turn 19/40  │
│  ─ execution timeline ─           stopped: browser session lost     │
│  (from conversation turns)         last confirmed evidence 14m ago  │
│  [COMPUTED]                        [Resume] [Open evidence] [Stop]  │
│                               ──────────────────────────────────── │
│                               W3  readme-update                     │
│                                    Queued  ·  waiting for capacity  │
│                                    (disk floor: 6.0 GB)             │
│                               ──────────────────────────────────── │
├─────────────────────────────────────────────────────────────────────┤
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Steer or intervene...  (W2: resume after browser reset)      │  │
│  │                                                               │  │
│  │  "/" for commands · affects active run         [Send →]      │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 5b. Main Window (Option B: Chat-First + Fleet Strip)

```
┌─────────────────────────────────────────────────────────────────────┐
│ M365 Companion Agent                             [Fleet] [⚙] [◑]   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  (conversation / latest answer)                                      │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ USER: SWE-bench 失敗を明日朝までに分析して                   │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ AGENT: 承りました。4 lanes に分割して実行中...               │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  ╔═══════════════════════════════════════════════════════════════╗   │
│  ║ CURRENT DELEGATION                              → Fleet       ║   │
│  ║ 4 lanes · 1 needs attention · evidence 8s ago                ║   │
│  ║                                                               ║   │
│  ║ W0 Running    W1 Verifying    W2 ⚠ Attention    W3 Queued   ║   │
│  ║ (cause-clust) (scaffold)      (harness)         (readme)     ║   │
│  ╚═══════════════════════════════════════════════════════════════╝   │
│                                                                      │
├─────────────────────────────────────────────────────────────────────┤
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Message or directive...                                       │  │
│  │                                                               │  │
│  │  "/" for commands                              [Send →]      │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 5c. What is hidden (Active state)

- Raw status.json fields (started timestamp, max_concurrent, open_tabs, avail_mb, disk_floor_gb)
- Turn numbers beyond summary (turn 14/40 shown; full transcript behind "Open evidence")
- Refuter messages (raw)
- Internal prompt
- Worker name / key (shown only in debug drawer)
- Plan steps (shown only in lane detail if `--plan` mode, else hidden)
- All lanes beyond 4-row summary (scroll to reach them; no pagination)
- History tab (archived runs) — behind ☰

### 5d. Where intervention happens

The **intervention composer** at bottom is the single input surface. It accepts:
- Free text steering (sent to a running lane via steer mechanism)
- Slash commands: `/resume`, `/stop W2`, `/status`, `/diff`
- For W2 specifically: inline [Resume] / [Open evidence] / [Stop] buttons on the attention lane row

The composer placeholder adapts context: when the last event is an attention lane, it suggests `"W2: resume after browser reset"`.

### 5e. Trust evidence displayed (Active state)

| Evidence shown | Tag | Source |
|----------------|-----|--------|
| Freshness: "evidence 8s ago" | `[COMPUTED]` | `now - updated` (top-level status.json field) |
| Lane phase label | `[COMPUTED]` | remap of `status` field |
| Turn progress "turn 14/40" | `[REAL]` | `turn`, `max_turns` per worker |
| Last text excerpt | `[REAL]` | `last` field (600-char tail) |
| "last confirmed evidence 14m ago" on W2 | `[COMPUTED]` | last transcript `ts` before status went stuck |
| Stopped reason | `[REAL]` | `reason` field |
| Spine timestamps | `[COMPUTED]` | per-turn `ts` from transcript jsonl |

### 5f. Element Table — Active

| Element | Tag | Source | Fallback |
|---------|-----|--------|----------|
| Header: "4 lanes" | `[COMPUTED]` | count of workers in status.json | "0 lanes" |
| Header: "1 needs attention" | `[COMPUTED]` | count where status ∈ {stuck, maxturns, error} | omitted if 0 |
| Header: "evidence Xs ago" | `[COMPUTED]` | `now - updated` (top-level `updated` field) | "no recent evidence" |
| Directive text | `[REAL]` | `goal` of first/primary worker (or fleet-level if added) | "No directive text" |
| "started HH:MM · Xh elapsed" | `[COMPUTED]` | top-level `started` field → local datetime | omit if no `started` |
| Lane name | `[REAL]` | `name` field per worker | Worker key |
| Lane phase | `[COMPUTED]` | remap of `status` | raw status value |
| turn N/max | `[REAL]` | `turn`, `max_turns` per worker | "turn N" only if no max_turns |
| Last text excerpt | `[REAL]` | `last` (600-char tail) | "(no excerpt yet)" |
| Last excerpt age "19s ago" | `[COMPUTED]` | last transcript jsonl `ts` | "(unknown)" |
| Stopped reason | `[REAL]` | `reason` field | "(no reason recorded)" |
| W3 "waiting for capacity" | `[COMPUTED]` | status == "pending" AND free_disk_gb near disk_floor_gb | "Queued" |
| Execution timeline spine | `[COMPUTED]` | per-turn ts from transcript + polled status changes | "Execution timeline unavailable (no transcript)" |
| Proper phase events | `[FUTURE]` | `phase_events[]` from relay | Fall back to [COMPUTED] timeline above |
| "confidence medium" | `[FUTURE]` | Not in data model | **Must not be shown** until backend exists |
| "next expected step" | `[FUTURE]` | Not in data model | **Must not be shown** |

---

## 6. Failure as a Recovery Surface

W2's "Needs attention" row is the primary failure surface. **Not an error card — a recovery surface.**

### Wireframe (W2 attention row, expanded)

```
┌─────────────────────────────────────────────────────────────────────┐
│ W2  harness-verification                                        ⚠   │
│                                                                      │
│  Needs attention                                                     │
│                                                                      │
│  The run stopped because the browser session stopped responding.    │
│  Last confirmed evidence was 14 minutes ago.                        │
│                                                                      │
│  Reason on record:                                                   │
│  "browser unresponsive after 3 retries"         [REAL: reason]      │
│                                                                      │
│  What was confirmed before stopping:                                 │
│  turn 19 · "ran pytest on 12 files, 8 passed, fixture mismatch      │
│  on 4 — investigating"                          [REAL: last]        │
│                                                                      │
│  ┌──────────────┐  ┌──────────────────┐  ┌──────────────────────┐  │
│  │   Resume     │  │  Open evidence   │  │  Stop this lane      │  │
│  └──────────────┘  └──────────────────┘  └──────────────────────┘  │
│                                                                      │
│  ─ [Transcript] [Debug ▾] ──────────────────────────────────────── │
└─────────────────────────────────────────────────────────────────────┘
```

### Recovery actions — real vs. future

| Action | Implementation | Tag |
|--------|---------------|-----|
| [Resume] | Writes `{"resume": "W2"}` to commands.json (relay consumes) | `[REAL]` — fleet_runner already handles resume command |
| [Open evidence] | Opens transcript jsonl path (`transcript` field) | `[REAL]` — `transcript` field exists |
| [Stop this lane] | Writes `{"release": "W2"}` to commands.json | `[REAL]` — release command exists |
| Suggested next action | "Resume from latest session" | `[FUTURE]` — no relay logic to suggest recovery step; hardcode generic text for now |

### Copy rules

- Never write "ERROR" as a heading. Heading is "Needs attention" (Attention color, not Dead color).
- Always include the `reason` text verbatim — it is the human's ground truth.
- Always include freshness: "last confirmed evidence N minutes ago" — so the human can judge staleness.
- The three buttons are always the same set; their visual weight is equal (no default/destructive distinction in this state — the human decides).

---

## 7. State 3 — Returned Next Morning

Example: run finished or partially failed. W0, W1, W3 done; W2 stopped (maxturns).

### 7a. Fleet Window (Option A: Ledger-First)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Fleet                 3 done · 1 needs attention · run ended         │
│                       [New delegation]  [Effort:auto▾] [⚙] [◑]     │
├─────────────────────────────────────────────────────────────────────┤
│  DIRECTIVE                                                           │
│  ─────────────────────────────────────────────────────────────────  │
│  SWE-bench 失敗 85 件を分類し、修正案を明日朝 08:00 までにまとめる  │
│  started 09:14 yesterday · ended 06:47 · 21h 33m elapsed            │
├─────────────────────────────────────────────────────────────────────┤
│  EVIDENCE SPINE               LANE BOARD                            │
│  │                            ──────────────────────────────────── │
│  ● 09:14  directive recv'd    W0  cause-clustering            ✓    │
│  │                                Done  ·  verified             │
│  ● 09:14  lanes dispatched        "Grouped 85 cases into 6         │
│  │                                 categories, doc written"        │
│  ● 10:44  W2 needs attention       21h ago · [Open evidence]       │
│  │                            ──────────────────────────────────── │
│  ● 06:47  W0, W1, W3 done     W1  scaffold-patch                   ✓│
│  │        W2 hit max turns         Done  ·  verified               │
│  ● now                             "All 12 tests pass on patched    │
│                                    scaffold"  ·  21h ago           │
│  ─ execution timeline ─            [Open evidence]                  │
│  (from conversation turns)    ──────────────────────────────────── │
│  [COMPUTED]                   W2  harness-verification         ⚠   │
│                                    Needs attention  ·  max turns    │
│                                    stopped: "reached turn limit     │
│                                    at fixture mismatch step"        │
│                                    last evidence 22h ago           │
│                                    [Resume] [Open evidence] [Stop]  │
│                               ──────────────────────────────────── │
│                               W3  readme-update                  ✓  │
│                                    Done  ·  not verified            │
│                                    "README updated with new         │
│                                     run instructions"  ·  20h ago  │
│                                    [Open evidence]                  │
│                               ──────────────────────────────────── │
├─────────────────────────────────────────────────────────────────────┤
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Follow up or start new delegation...                         │  │
│  │                                                               │  │
│  │  "/" for commands                              [Send →]      │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 7b. Main Window (Option B) — Morning Return

```
┌─────────────────────────────────────────────────────────────────────┐
│ M365 Companion Agent                             [Fleet] [⚙] [◑]   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ AGENT (06:47): 完了しました。3 lanes 完了、1 lane が        │    │
│  │ turn 上限で停止しています。詳細は Fleet で確認できます。     │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  ╔═══════════════════════════════════════════════════════════════╗   │
│  ║ DELEGATION COMPLETE (partially)             → Fleet           ║   │
│  ║ 3 done · 1 needs attention (max turns)                       ║   │
│  ║                                                               ║   │
│  ║ W0 ✓ Done    W1 ✓ Done    W2 ⚠ Attention    W3 ✓ Done      ║   │
│  ╚═══════════════════════════════════════════════════════════════╝   │
│                                                                      │
├─────────────────────────────────────────────────────────────────────┤
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Follow up or start new task...                               │  │
│  │                                                               │  │
│  │  "/" for commands                              [Send →]      │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 7c. What is hidden (Morning-return state)

- Raw JSON fields (started, total, max_concurrent, open_tabs, etc.)
- Full transcript — behind "Open evidence"
- Plan steps (shown only in lane detail)
- Verify_attempts count (shown in lane detail drawer, not summary row)
- Debug drawer (hidden until explicitly requested)
- History of previous runs (behind ☰)

### 7d. Where intervention happens

- W2: inline [Resume] / [Open evidence] / [Stop] on the attention row
- Composer: free-text follow-up or new delegation
- "[New delegation]" button in header clears directive and opens composer

### 7e. Trust evidence displayed (Morning-return)

| Evidence | Tag | Source |
|----------|-----|--------|
| "verified" badge on W0, W1 | `[REAL]` | `verified == true` per worker |
| "not verified" note on W3 | `[REAL]` | `verified == false`, `verify_attempts` |
| Outcome text excerpt | `[REAL]` | `outcome` field or `last` field |
| Freshness "21h ago" | `[COMPUTED]` | now - last transcript `ts` |
| "reached turn limit" reason | `[REAL]` | `reason` field |
| Run elapsed "21h 33m" | `[COMPUTED]` | top-level `updated - started` |

### 7f. Element Table — Morning-return

| Element | Tag | Source | Fallback |
|---------|-----|--------|----------|
| "3 done · 1 needs attention" | `[COMPUTED]` | count by status group | shows counts accurately |
| "run ended" | `[COMPUTED]` | `running == false` AND all workers terminal | "run may still be active" |
| Verified badge ✓ | `[REAL]` | `verified == true` | omit badge, not "unverified" label (silent) |
| "not verified" note | `[REAL]` | `verified == false` | shown only when verify_attempts > 0 but not verified |
| Outcome excerpt | `[REAL]` | `outcome` field; fallback to `last` | "(no outcome recorded)" |
| "W3 not verified" reason | `[FUTURE]` | No structured reason for verify skip | Just show "not verified" without explanation |
| "evidence Xh ago" | `[COMPUTED]` | now - last transcript ts | "(unknown)" |
| History of prior runs | `[REAL]` | history.json | "(no prior runs)" |

---

## 8. Lane Detail Drawer (Expanded Row)

When a lane row is expanded (click or chevron), a detail drawer opens below it. This is how Ph3/Ph4 card content is **demoted** rather than deleted.

### Wireframe (W1 scaffold-patch, done + verified)

```
┌────────────────────────────────────────────────────────────────────┐
│ W1  scaffold-patch                                           ✓ Done │
│     Verified · turn 28/40 · 21h ago                          [▲]   │
├────────────────────────────────────────────────────────────────────┤
│  [Summary]  [Evidence]  [Acceptance]  [Transcript]  [Debug ▾]      │
├────────────────────────────────────────────────────────────────────┤
│  SUMMARY                                                            │
│  ─────────────────────────────────────────────────────────────── │
│  outcome:                                             [REAL]        │
│  "All 12 tests pass on patched scaffold. Fixture mismatch          │
│   resolved by separating env setup from test body."                │
│                                                                     │
│  ACCEPTANCE CHECKS                                                  │
│  ─────────────────────────────────────────────────────────────── │
│  checks[] (from goal):                                [REAL]        │
│  (empty — no structured checks specified)                           │
│                                                                     │
│  PLAN STEPS (--plan mode only)                                      │
│  ─────────────────────────────────────────────────────────────── │
│  (not shown — this run used approval=run)             [REAL]        │
│                                                                     │
│  verify_attempts: 2 → verified: true                  [REAL]        │
│  eval_busy_until: —                                   [REAL]        │
│  cwd: /m365-companion                                 [REAL]        │
│  conv_url: https://...                                [REAL]        │
└────────────────────────────────────────────────────────────────────┘
```

### Mapping Ph3/Ph4 Card Tabs to Drawer Tabs

| Old tab | New drawer tab | Note |
|---------|---------------|------|
| Overview | Summary | `outcome` + `last` text `[REAL]` |
| Conversation | Transcript | Link to `conv_url` + inline transcript reader `[REAL]` |
| Review | Acceptance | `checks[]` + `verified` + `verify_attempts` `[REAL]` |
| Logs | Evidence | `last` (600-char tail), spine timeline `[COMPUTED]` |
| (debug) | Debug ▾ | Raw `status.json` worker entry; hidden by default `[REAL]` |

The Ph3/Ph4 card's steer TextBox becomes the **intervention composer** at the Fleet window bottom (not per-card). Per-lane steering is dispatched by prefixing with worker name: `W2: try restarting fixture`.

---

## 9. Migration / Non-Destructive Note

### What changes structurally

1. The header's "N workers" chip + elapsed subtitle → kept as-is but relocated to header subtitle row
2. Filter tabs (All / Active / Needs input / Done) → demoted to secondary; default view is lane board without filter
3. Per-worker card → becomes a lane row (collapsed default, expands to drawer)
4. Effort / Approval dropdowns → stay in header (NOT moved into contract panel in this iteration)
5. Autoscale toggle → stays in header
6. Settings gear → stays; disk floor, RAM floor, retry settings stay inside it
7. The composer at bottom → semantics shift from "add goals" (idle) to "steer / intervene" (active); placeholder text adapts

### What must NOT be touched now

- The existing card expand/collapse mechanism (`_expanded` HashSet) — reused for drawer
- The `commands.json` mechanism for resume/release/stop — reused for recovery actions
- The `history.json` / `cockpit_hidden.json` persistence — unchanged
- The VirtualizingStackPanel approach — lane rows are the items, same pattern

### Constraint: KEEP effort/approval dropdowns in header

The direction doc suggests the contract panel as "the right place for approval design." However, the current settings are persistent user preferences (not per-run), and users are accustomed to the header location. **This spec keeps them in the header** and additionally surfaces them in the contract pre-flight panel — both showing the same value.

---

## 10. Implementation Phasing

### Bucket A: UI-now (no backend change needed)

All fields are `[REAL]` or `[COMPUTED]` from existing status.json. Can ship in a single build-cockpit.bat cycle.

1. **Palette update** — 5 token value changes in Theme.cs + 1 new `Secondary()` token
2. **Lane rows replace cards** — collapse existing card to summary row; drawer on expand
3. **Directive section** — read `goal` from first worker; display at top
4. **Freshness in header** — `now - updated` (top-level `updated` field)
5. **Phase labels** — status → display phase remap (table in §2)
6. **Execution timeline spine** — `[COMPUTED]` turn-ts approximation (labeled honestly)
7. **Attention row with recovery copy** — `reason` text + freshness + Resume/Open/Stop buttons
8. **Intervention composer** — rename "Add tasks" placeholder to "Steer or intervene..." when run active
9. **Verified badge** — `verified == true` → ✓ mark; `verified == false` + verify_attempts > 0 → "not verified"
10. **Run-ended state** — `running == false` + all terminal → "run ended" header label + [New delegation]

### Bucket B: Backend contract change (requires relay edit)

1. **phase_events list** — add to fleet_runner.py state-machine transitions; emit `{ts, event, label}` on every status change. Enables proper Evidence Spine.
2. **Fleet-level directive** — add a top-level `directive` field to status.json (today there are only N independent goals). Needed for the "single directive → N lanes" mental model to be accurate.

### Bucket C: New behavior (significant new logic)

1. **Autonomy contract enforcement** — "Ask before" / "Stop when" actually enforced at relay level
2. **Confidence scoring** — per-lane confidence estimation (requires agent cooperation or external judge)
3. **Next expected step** — structured step prediction (requires agent or relay inference)
4. **Human-prose acceptance criteria** — structured `acceptance` field distinct from shell `checks[]`

**Ship order:** A first (all REAL/COMPUTED, zero fabrication). B second (relay-only change, no agent impact). C is a product decision, not a spec commitment.

---

## 11. Elements That Must Never Be Fabricated

The following `[FUTURE]` elements must be **absent or clearly labeled as unimplemented intent** until the backend contract exists. They must not appear with fake values at ship time:

| Element | Why it must not be faked |
|---------|--------------------------|
| Confidence ("medium", "high") | Would misrepresent actual task state |
| Next expected step | Would invent information the system doesn't know |
| Phase event log (structured) | Would invent timestamps the relay didn't record |
| "Ask before" / "Stop when" enforcement | Would imply a runtime guarantee that doesn't exist |
| Single "directive" concept | Today it is N goals; fleet-level directive is a future field |

---

## Appendix: Compact Field Reference

### status.json top-level fields `[REAL]`
`started · updated · total · done_count · running · paused · max_concurrent · open_tabs · avail_mb · disk_floor_gb · free_disk_gb · ram_floor_mb`

### Per-worker fields `[REAL]`
`name · goal · status · pill · color · outcome · turn · max_turns · reason · closed · conv_url · conv_title · verified · verify_attempts · eval_busy_until · plan · last · transcript · checks · cwd`

### Worker status vocabulary `[REAL]`
`pending | ready | waiting | verifying | refuting | researching | done | stuck | maxturns | error | cancelled`

### Transcript jsonl `[REAL]`
Line 1: `{meta:true, key, name, goal, ts}` — Lane metadata, `ts` = epoch start  
Per turn: `{turn, role:"user"|"assistant", text, ts}` — Each turn with timestamp  
**No phase tags, no tool-call log, no confidence scores in current format.**

### settings.txt keys `[REAL]`
`dark · lang · maxtabs · autoscale · autoscale_max · autoretry · autoretry_max · disk_floor_gb · ram_floor_mb · effort · approval`
