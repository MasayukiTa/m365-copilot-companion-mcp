# Toward a Claude-Code-like feel (cockpit / companion chat)

User direction (2026-06-19): "もう少し機能を Claude Code に寄せたい … Claude Code 使ってる人なら
簡単に使いこなせる、そんなのを目指している." Could not fully articulate — this doc articulates it.

## The core insight

Claude Code's feel = **ONE coherent thread where the agent's activity is visible and expandable
inline**: each tool call and each sub-agent (Task) shows up as a collapsible block you can open to
see detail, top-to-bottom, with live status and the ability to steer.

This system already has the PRIMITIVES; the gap is PRESENTATION + sub-conversation capture.

## Confirmed diagnosis (the trigger)

research / refute / analyze run as **ephemeral side pages**: `ResearchSession.start()` does
`context.new_page()` (agent_profiles.py:323), drives the Researcher, then `close()` (:385-393).
NO conv_url registration, NO transcript — the sub-conversation evaporates. Only the final REPORT
string is fed back into the MAIN transcript as the next turn (`_poll_research` → self.job →
`_tx.user`, relay_fleet.py:910/742). So:
- research/refute/analyze PROCESS = invisible by design (side page closed without capture).
- the research REPORT = in the main transcript, but currently unreachable because conv_url is
  empty for the new agent (see conv_mgmt_fix_plan.md #7) so the cockpit can't open the main convo.

The user's read is exactly right: "continue で続くのに完了で終わる" = research returned a good
result and the worker solved; but you can't see what the research did.

## Primitive → Claude-Code-concept map

| Claude Code concept | existing primitive | gap |
|---|---|---|
| sub-agent (Task) result shown inline, expandable | research/refute/analyze side pages | not captured → invisible |
| tool-call visibility (what it's doing now) | worker status (researching/refuting/verifying) + `reason` | only transient; not in history |
| single top-to-bottom activity thread | `_Transcript` jsonl per worker | cockpit can't open it (#7); no nesting |
| slash commands | bridge /research, /analyze | not surfaced in the chat UI |
| steering / interrupt-redirect | `steer()` | exists; surface it as CC-style |
| plan mode / TODO list | `plan_mode`, `plan_steps`, awaiting-approval | weak presentation |
| diff / edit visualization | SWE `git diff` per worktree | not shown on the card |

## Proposed implementation (post-run; relay = arm-B import, DEFER)

Phase 1 — make sub-agent work visible (this IS the CC core, and fixes "research が見えない"):
1. **Capture sub-conversations.** Before `ResearchSession.close()` (and the refute/analyze
   equivalents), persist the side page's transcript (reuse `_Transcript`) and record its conv_url,
   linked to the parent worker + the turn that spawned it (parent_key + turn + kind=research|refute
   |analyze). Don't just keep the report — keep the process.
2. **Nest them in the thread.** In the cockpit/chat transcript view, render each captured
   sub-conversation as a collapsible block under the spawning turn ("🔎 research: <query>" →
   expand → the researcher's turns). Mirrors CC's tool-call / sub-agent cards.

Phase 2 — make the thread read like Claude Code:
3. Activity log: persist status transitions (researching→…→verifying→done) as inline step markers,
   not just the transient `reason`.
4. Surface slash commands (/research, /analyze, /plan, steer) in the chat input affordance.
5. Plan mode: render plan_steps as a checklist with approve/steer, CC-style.
6. Diff block: show the worker's produced diff inline (expandable), like CC shows edits.

Phase 0 prerequisites (already planned): #7 conv_url capture (so the main thread is openable) and
#3 transcript persistence (disk body always present). Those unblock everything here.

## User priority (answered 2026-06-19): ALL FOUR + deeper UI/UX

User picked all of (a) sub-agent visibility, (b) single expandable thread, (c) slash + steering,
(d) plan/TODO — AND added the key signal:

> "UI/UX 関連も。言葉にできないほど差が大きい。機能は同じでも、私が使うときに戸惑う場面がある
> 時点で操作性が大きく違うはず。"

So the real target is NOT feature parity (the features mostly exist) — it is **eliminating the
friction points where the user hesitates/gets confused**. The confusion points ARE the spec.

### Workstream 0 — Friction inventory (do this FIRST; it is the spec)

Turn "言葉にできない" into a concrete list: audit the cockpit (FleetCockpit.cs) + companion chat
(CopilotChat.cs) interaction model against Claude-Code conventions and enumerate every spot where
operability diverges / a CC user would hesitate — discoverability (is the action findable?),
affordance (does it look interactive?), feedback (did my action register?), naming, default view,
keyboard flow, where state lives, what's hidden vs shown. Each friction → a concrete fix, mapped to
(a)-(d). This inventory drives Phases 1-2; don't guess the UX, derive it from the friction list.

Implementation order: Phase 0 prereqs (#7 conv_url, #3 transcript) → Workstream 0 (friction
inventory) → Phase 1 (sub-agent capture + nesting) → Phase 2 (thread/slash/plan/diff polish),
folding friction fixes throughout. All post-run.
