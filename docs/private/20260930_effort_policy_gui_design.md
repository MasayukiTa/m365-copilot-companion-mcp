# Effort policy: cockpit GUI design (no C# in this PR)

Status: design only. Python side (`effort_policy` settings key, `mode_info()`) is in the same PR.
Line numbers are from `ui/FleetCockpit.cs` on origin/main at the time of writing (read-only);
re-grep before editing, codex is changing that file.

## Principle

The setting is honoured on the one path that reads it, and the screen shows what is really in
effect. Order in Python: env `MCP_EFFORT_POLICY` > settings.txt `effort_policy` > off. The
cockpit never passes `MCP_EFFORT_POLICY` to child processes itself (that would silently beat
its own control); it only reports if the environment it inherited does.

## 1. Where the control lives

Next to the existing effort selector, same surface: `EffortControl()` (~L8985-9015) builds the
header `ComboBox _effortBox` beside `_effortLbl`. Add `EffortPolicyControl()` returning a second
`StackPanel` (label + `ComboBox _effortPolicyBox`) placed immediately after `EffortControl()`
where the header composes it (grep `EffortControl()`); also add one read-only row to the settings
popup block at ~L7402-7440 (the `effortRow`) showing the current value.

Labels follow the file's `T(k)` table (~L1474 `if (k == "effort") return ja ? ... : ...`):

| key | ja | en |
|---|---|---|
| `effort_policy` | 推論方針 | Effort policy |
| `ep_off` | オフ | Off |
| `ep_shadow` | シャドウ(記録のみ) | Shadow (record only) |
| `ep_on` | オン(初期effortを適用) | On (apply initial effort) |
| `ep_conflict` | 環境変数 MCP_EFFORT_POLICY={0} が設定 {1} を上書き中 | env MCP_EFFORT_POLICY={0} overrides this setting ({1}) |
| `ep_help` | オン=ファンアウト子の初期effortを1段下げる。走行中の切替は記録のみ。 | On = fan-out children start one level lower; live switching is record-only. |

Values persisted are the English tokens `off|shadow|on` (like `_effortModes`, with a parallel
`static readonly string[] _effortPolicyModes = { "off", "shadow", "on" }` and
`FillComboWithHelp(..., EffortPolicyHelp(), _effortPolicy)`).

## 2. Settings key and parse rule (mirror `effort=`)

Key: `effort_policy=off|shadow|on`, default `off`. Mirror exactly these `effort` code paths:

- field: `string _effort = "auto";` (L815) -> `string _effortPolicy = "off";`
- settings load: the `else if (ln.StartsWith("effort="))` branch (~L1812-1816) -> add
  `else if (ln.StartsWith("effort_policy="))` **before** it is not needed (prefixes differ:
  `effort=` does not match `effort_policy=`), accepting only `off|shadow|on`, else keep default.
- persist: `SaveKey("effort", _effort)` in `SelectionChanged` (~L9008) -> `SaveKey("effort_policy", ...)`.
- paint: `PaintEffort()` (~L9017) -> `PaintEffortPolicy()`, same "assign only if different so
  SelectionChanged does not re-fire" guard; call it wherever `PaintEffort()` is called (L5634,
  L6795, L9014, theme repaint).
- timing table: the `case "effort":` list (~L7772, returns `"sweep_start"`) has a mirror of
  `tools/settings_keys.py`; add `case "effort_policy": return "each_gate";` in the each_gate group.
  `tools/test_a_setting_declares_when_it_takes_effect.py` already requires the Python side.
- slash command (optional, parity with `/effort`): `/effortpolicy off|shadow|on`.

Take-effect wording for the tooltip: "Applies to the next worker / fan-out / turn evaluation, no
restart. Decisions already made stand." (That is the each_gate granularity the Python test pins.)

## 3. "In effect" indicator

The cockpit must not assume its own combo is what runs. Source of truth: the runner writes the
resolved `mode_info()` into `.fleet/status.json` top level (additive):
`"effort_policy": {"mode": "shadow", "source": "env|settings|default", "conflict": false}`
(written where `fleet_runner.py` builds the snapshot next to `"pending_gates"`, ~L1707).
Cockpit shows, beside the combo, a small text: `有効: シャドウ (設定)` / `In effect: shadow (settings)`.
If `source == "env"` show `(環境変数)`; if `conflict` show the `ep_conflict` warning in the
amber warning style already used by the settings-off notice (see
`ui/test_a_setting_that_is_off_is_said_out_loud.py` for the pattern: a setting that does not
do what the screen says must say so). If status.json lacks the field (old runner), show nothing
rather than guessing.

## 4. Per-worker badge

Data, additive per-worker fields in the worker row of status.json (`fleet_runner.py` ~L1631,
where `"conv_url"` is written from the worker object; add from `getattr(w, ...)`):

- `effort_level`: `min|max|auto|ultra` (from `effort_policy.worker_level(w)`)
- `effort_source`: `run|goal|parent|policy|escalation` (`EffortState.source`)
- `effort_last_switch`: `{"turn": n, "from": a, "to": b, "reason": "..."}` or absent

relay_fleet.py owns the worker and its `EffortState`; it must expose them as attributes (e.g.
`w.effort_state`) so the snapshot reads them the same way it reads `conv_url`. Live switching is
still record-only, so `effort_last_switch` initially only appears in shadow as "would switch";
the badge must label it `(記録のみ)` / `(shadow)` until switching is real.
Card badge: small pill `推論 max` with tooltip `source: parent; last: auto->max (refuted)`.
Absent fields = no badge.

## 5. Test plan (repo C# pattern)

Put the parse/format logic in a WPF-free static class `ui/EffortPolicy.cs`
(`EffortPolicyView.ParseMode(string line)`, `Describe(mode, source, conflict, ja)`,
`BadgeText(level, source, lastSwitch, ja)`), used by FleetCockpit.cs, so it can be compiled
without WPF. Steps, following `ui/test_a_submitted_task_is_on_top_at_once.py`:

1. `ui/EffortPolicy.cs` (shipped, no `System.Windows`), `ui/harness/EffortPolicyHarness.cs`
   (test-only console `Main` printing results as lines/JSON).
2. `ui/test_the_effort_policy_says_what_is_in_effect.py`: fixture compiles
   `[EffortPolicy.cs, harness/EffortPolicyHarness.cs]` with `%FW%\csc.exe /target:exe`, skip
   when not Windows / no csc unless `REQUIRE_CSC=1` (CI). Cases: parse `effort_policy=on`,
   BOM, invalid -> default off, `effort=on` must not match, conflict text ja/en, badge text.
   Include a mutation check (swap env/settings precedence in the view text) in the test.
3. Register `EffortPolicy.cs` in the `Build "FleetCockpit" @(...)` line of `ui/rebuild_ui.ps1`
   (L51) so it ships.
4. Register `ui/harness/EffortPolicyHarness.cs` in `COMPILED_BY_A_TEST` in
   `ui/test_one_list_says_what_the_ui_compiles.py` (~L129), mapped to the new test, whose body
   must name `"harness"` and `"EffortPolicyHarness.cs"` (the ratchet checks both directions).
5. Add the test to `.github/workflows/ci.yml` (manifest check) and run
   `ui/test_both_windows_can_be_constructed.py` (selftest) and
   `tools/test_a_setting_declares_when_it_takes_effect.py`
   (`test_every_key_the_panel_writes_is_declared` will require the key, already declared).

## 6. Merge-conflict plan

`FleetCockpit.cs` is under active change by codex. Do not edit it now. After codex's current
FleetCockpit changes are committed and merged, make ONE small PR from a fresh branch off
main touching FleetCockpit.cs only at the anchor points above (field, load branch, combo,
paint, case label, header compose call); all logic lives in the new `EffortPolicy.cs`, so the
diff to the contested file is ~25 lines. Rebase immediately before opening, re-grep the line
numbers, and run the whole `ui/` test set. The Python fields in section 3/4 (status.json) can
land earlier and independently: absent fields are simply not shown.
