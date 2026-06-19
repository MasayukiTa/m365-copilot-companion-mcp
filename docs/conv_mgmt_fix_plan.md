# Conversation-management hardening plan (execute AFTER the running ⑤A/B completes)

User report (2026-06-19): native-chat conversation management is "非常に不便". Two concrete
symptoms, both traced to the SAME root: native-chat READ is alive (URL binding persists), but
every UI-DRIVEN op (delete, history scrape) goes through a bridge Edge that is down / SSO-stuck,
or is self-suppressed mid-run. Fix the drive path, not the binding.

## Root causes (verified in code this session)

- A. **Auto-delete fails** ("自動削除はできませんでした", `ui/CopilotChat.cs:105`). `/delete`
     (cockpit → bridge HTTP :8765 → bridge Edge :9223) needs a live bridge Edge ON a real
     conversation surface. Now: :9223 DOWN (no start_bridge proc) and the bridge server's page
     is parked on `?redirfrom=CsrToSSR&auth=2` (SSO landing). DeleteFailBucket: guid mismatch /
     timeout / menu failed / confirm failed.
- B. **"この会話の本文はまだ取得できません"** (`ui/CopilotChat.cs:696`, the `else` fallback at
     692-697). Body load has 3 sources (621-628): (1) persisted jsonl transcript [disk, always
     safe], (2) /switch+/history DOM scrape — **suppressed while a run is live** (line 631
     `!running`, regression guard: it used to PAGE.goto the SHARED :9222 fleet Edge), (3)
     status.json snapshot. Past completed chat with no jsonl + scrape suppressed mid-run → all
     three empty → placeholder. **Binding intact** (keyed on ConvUrl at 658).
- C. Past completed chats lack a persisted jsonl transcript → no disk fallback (#1).
- D. **SSO-redirect orphan tabs accumulate** on both Edges (saw 2 reaped on :9222 this session;
     bridge :9223 also parked on the SSO landing).

## Unifying insight

The bridge/fleet Edge split (the "2" chosen this session, `reference_bridge_fleet_edge_split`):
fleet owns :9222 (profile copilot-companion-edge), bridge owns :9223 (copilot-bridge-edge), and
the fleet NEVER touches :9223. So once the bridge Edge is healthy, history+delete can run THROUGH
:9223 even mid-run WITHOUT clobbering the fleet. The "never scrape during a live run" guard was
written for the OLD shared-Edge world and is now over-broad.

## Implementation items

1. **Bridge robustness (linchpin).**
   - `start_bridge.ps1` must reliably bring up :9223 and KEEP it alive (supervisor/scheduled task,
     analogous to the eval host SweDockerd). Initial SSO login is interactive → FOREGROUND
     (`feedback_interactive_auth_foreground`).
   - Add SSO-redirect recovery to the bridge Edge: when its page lands on `?redirfrom=CsrToSSR`
     (or any non-`on_agent_surface` landing), re-navigate to the target conv/agent URL. Reuse the
     `_maybe_renav_off_redirect` / `looks_like_redirect_landing` / `on_agent_surface` helpers added
     to `relay/relay_fleet.py` this session; lift them into `relay/bridge/copilot_bridge.py`.
   - Confirm/fix that the :8765 bridge server's `MCP_CDP_URL` points at :9223 (NOT :9222). It
     returned an SSO URL with the agent titleId — verify it is bound to the bridge Edge, never the
     fleet Edge. Bridge ops must NEVER touch :9222.

2. **Allow mid-run reads via the bridge (remove the over-broad suppression).**
   - `ui/CopilotChat.cs`: the `!running` guard at line 631 was to protect the SHARED Edge. Route
     /switch+/history through the bridge Edge (:9223) and allow the scrape DURING a run. Keep disk
     transcript (#1) as the primary, cheapest path. Net effect: clicking a past chat mid-run loads
     its body via the bridge without disturbing the fleet on :9222.

3. **Universal transcript persistence.**
   - Persist a jsonl transcript for EVERY conversation (native chat turns + every fleet worker) at
     the path `ReadTranscript` expects, so path #1 always has content even mid-run / even if the
     bridge momentarily drops. (SWE workers already persist; extend to native chat + all workers.)

4. **Orphan reaper.**
   - close-on-done AND a periodic sweep detect SSO-redirect landings (`?redirfrom=CsrToSSR`,
     empty-guid `/chat/?...`) and close them on BOTH :9222 and :9223. Prevents accumulation over
     long runs. (close logic proven this session via CDP /json/close on the bare-redirect target.)

5b. **Cockpit expand-jump (related UX inconvenience).** Pressing ">" on a worker (esp. a RUNNING
    one like W2) scrolls far away, slow to return. Cause: `ToggleExpand`/700ms `OnTick` reassign the
    WHOLE `_list.ItemsSource` with a new List (`SetRows`, FleetCockpit.cs:1466) → the virtualizing
    ListBox treats it as a collection reset → discards realized containers + resets scroll to top;
    offset is only restored on a deferred `DispatcherPriority.Background` pass (visible jump + lag).
    Amplified by item-based scrolling (`SetCanContentScroll(_list,true)`, :442) and the 700ms live
    refresh on running cards reassigning ItemsSource and fighting the restore.
    Fix: make `class Row` (:1493) implement INotifyPropertyChanged; expose `IsExpanded` + live-text
    as bindable properties. Toggle = flip that ONE row's property (no ItemsSource reassign → scroll
    offset untouched → zero jump). Convert the 700ms live update to per-row PropertyChanged updates
    instead of full ItemsSource replacement (kills both the expand jump and the periodic flicker).
    Reconsider CanContentScroll=true vs pixel scrolling.

## Verification (do BEFORE declaring done — "未検証は実装していないと同義")

- Run NOT active: open a past completed chat → body loads (not the placeholder). Auto-delete a
  throwaway conversation → succeeds (GUID disappears from history; bucket-free).
- Run active (start a tiny fleet on :9222): repeat both ops through the bridge → succeed, and
  confirm the fleet on :9222 is untouched (no send clobber, status.json keeps progressing).
- Confirm no SSO-redirect orphan tabs remain on either Edge after a run.

## Build / ship

- Rebuild `ui/CopilotChat.exe` and `ui/FleetCockpit` as needed (Windows; if eval-host-style
  build, NODE_OPTIONS=--max-old-space-size=16384 — though this is companion-mcp, check its build).
- Commit (English subject/body, NO `Co-Authored-By: Claude`), push to MasayukiTa/
  m365-copilot-companion-mcp main (direct push OK, `feedback_push_main_ok`).

## Progress (DONE NOW — measurement-safe files only; relay/* untouched)

Done while the ⑤A/B run is live, because these files are NOT imported by the per-chunk
`relay.fleet_runner` subprocess (verified: fleet_runner only *mentions* FleetCockpit.exe in a
comment), so editing them cannot contaminate arm B:

- [x] **#6 expand-jump** — `ui/FleetCockpit.cs`: bound a persistent `ObservableCollection<object>
  _rows` to the ListBox ONCE; `SetRows` now reconciles it in place (per-row `Sig` compare → Replace
  only changed rows, tail Add/Remove) instead of reassigning a fresh List. No collection Reset → no
  scroll-to-top → no expand jump / no 700ms flicker. Compiles clean via csc (77 KB). RUNTIME-VERIFY
  + exe swap = post-run.
- [x] **#1/#4 bridge SSO-recovery + reaper** — `bridge/copilot_bridge.py` (NB: path is
  `bridge/`, not `relay/bridge/`): added `_looks_redirected` / `_goto_settled` (re-navigates off a
  `?redirfrom=CsrToSSR` SSO landing, surfaces the hidden Edge on a hard login wall) and
  `_reap_orphan_tabs`. Wired `_goto_settled` into `/switch`, `/history`, and the delete-by-GUID
  path (the line that read "guid mismatch" on an SSO bounce → fixes "自動削除はできませんでした");
  reaper into the read handlers. py_compile clean. This also fixes the NON-running
  "本文はまだ取得できません" (history scrape now recovers from the bounce).
- [x] **#1 bridge keep-alive** — `start_bridge.ps1`: added `-Keepalive` supervisor (restart the
  bridge on exit, re-bring-up the Edge if CDP :9223 drops). PS parse clean. ACTIVATION (launch
  :9223 + one-time interactive SSO + RAM) = post-run.

## NEW root cause found (2026-06-19, while grading arm A) — empty conv_url

Symptom (user): the latest conversation's body won't scrape ("差分スクレイピングがうまくいって
いない"). Diagnosis: SWE git-diff scraping is FINE (all 8 arm-A worktree diffs non-empty, correct
files). The real gap: every completed worker this run has EMPTY `conv_url` AND empty `last` —
status.json (8/8) and history.json (0/97 have conv_url). So the cockpit/chat have no URL to open
and no cached body → nothing to show.
Why: `_capture_url` (relay_fleet.py:623) only matches `/conversation/<guid>`, but `on_agent_surface`
(the correct helper, :128) accepts `/conversation/` OR `/chat/<id>`. Historically (conversations.json
6/18, agent **T_446b4c09…**) capture worked: 281 convs with `/conversation/<guid>`. THIS run uses a
**different agent (T_02140b8c…)** whose conversation URL is apparently NOT `/conversation/<guid>`, so
capture silently misses → conv_url empty.
Fix (post-run, relay_fleet → arm-B import, DEFER): broaden `_capture_url` to capture when
`on_agent_surface(u)` is true (covers `/chat/<guid>`); add a final capture just before close-on-done;
pair with #3 transcript persistence so a missed conv_url still leaves a disk body. CONFIRM the real
URL form by inspecting the live :9222 page URL during arm B. (Task #7.)

## Still post-run (need runtime / touch measurement-imported code)

- [ ] #2 CopilotChat.cs mid-run scrape: safe only once the bridge is confirmed on :9223; code +
  verify together post-run.
- [ ] #3 universal jsonl transcript persistence (touches relay_fleet/fleet_runner → arm B import).
- [ ] #4 FLEET-Edge reaper (edge_recover/relay_fleet → arm B import). Bridge-Edge reaper done above.
- [ ] Launch bridge :9223 (RAM + interactive SSO); swap the rebuilt exes; full verification (#5).

## Trigger

The ⑤A/B runner (background task) notifies on completion. Execute the post-run items then. Do NOT
touch relay/* or launch the bridge / swap exes mid-measurement.
