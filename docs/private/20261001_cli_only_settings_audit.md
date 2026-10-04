# Settings and behaviors reachable only from the command line (audit, 2026-10-01)

Rule being applied: a setting or behavior that can be changed only through a CLI flag, an
environment variable or a hand edit, and has no GUI path, does not exist as a product feature.
Either the cockpit exposes it, or it is an internal developer/diagnostic knob and stays out of
the product's claims.

Method: the settings registry (`tools/settings_keys.py`), the argparse flags of
`relay/fleet_runner.py`, the `param()` block of `scripts/supervisor.ps1`, every `MCP_*` and
`FLEET_*` environment variable read by product Python (non-test, outside `bench/`), and the
cockpit sources (`ui/*.cs`) as the GUI side. Counts are from the tree at this commit.

## Fan-out (the case that started this)

| Item | State before | State now |
| --- | --- | --- |
| `fanout` default | ON in the cockpit field, the runner flag and the autostart decision; the registry said "undecided" | ON everywhere; the registry says `True` and a test pins all layers |
| GUI path | only the typed `/fanout on\|off` command | visible On/Off box in the header next to the effort policy, plus "in effect" text from `status.json` |
| Autostart with `fanout=off` | **no flag was passed, so the runner's own default (ON) won**: an explicit off was ignored | `--no-fanout` is passed explicitly, so off is honoured |
| `FLEET_INTAKE_AUTOSTART_FANOUT` env | wins over the setting | unchanged (see table C); the GUI "in effect" text shows when it does |

Where the owner's `fanout=off` came from: no product path seeds it (no setup, quickstart,
`start_all` or supervisor writes the key, and a fresh install has no `fanout` line, which means
ON). The cockpit writes only the one key it was asked to change (`SaveKey`), so it does not freeze
displayed defaults into the file. The only product writer is the `/fanout off` command. One
bench script, `scripts/win/run_swe_via_ui.ps1`, rewrites an existing `fanout=on` to `fanout=off`
by hand in a settings file and never restores it; the live GUI acceptance script
(`scripts/live_gui_browser_submit_acceptance.py`) refuses to run unless the value is off. An
existing `fanout=off` line is therefore a saved choice (or a bench leftover) that cannot be
told apart from a deliberate choice, so no migration is done. It is changed through the GUI box.

## A. Keys in settings.txt (31 keys in the registry)

| Key | What it does | GUI path | Recommendation |
| --- | --- | --- | --- |
| `fanout` | split long goals and merge | yes (new header box; `/fanout` too) | done |
| `autoscale_per_tab_mb` | memory assumed per tab for autoscale | **no** (file only) | add a stepper next to `autoscale_max` |
| `autoretry_max` = 0 | 0 means "off" | partly: the panel clamps 1..3 and uses the toggle for off | keep (the toggle is the GUI path) |
| `disk_floor_gb`, `ram_floor_mb`, `maxtabs`, `rate_ceiling_rpm`, `autoscale`, `autoscale_max`, `effort`, `effort_policy`, `autoretry`, `fleet_log_days`, `fleet_store_days`, `fleet_scratch_days`, `fleet_compress_hours`, `session_retention_days`, `session_max_mb`, `approval`, `job_approval_mode`, `runtime`, `autoarchive`, `deletemode` | operational settings | yes | none |
| `dark`, `lang`, `ui_scale`, `ui_scale_target`, `sidebar_collapsed`, `last_open_conv` | window state | written by the window itself | none |

Result: 1 key without a GUI path out of 31.

## B. `fleet_runner` flags (37)

The cockpit passes `--cdp-url`, `--state-dir`, `--effort`, `--plan`, `--fanout/--no-fanout` and
`--wait-for-state-dir-seconds` (6 of 37).

| Group | Flags | GUI path | Recommendation |
| --- | --- | --- | --- |
| mirrors a settings key | `--disk-floor-gb`, `--ram-floor-mb`, `--max-concurrent`, `--autoscale`, `--autoscale-max`, `--autoscale-per-tab-mb`, `--effort` | yes, via the key | keep; a flag beats the key, so launchers must not pass them unless the operator chose |
| run shape, no key | `--max-turns`, `--max-transient`, `--max-fresh-replays`, `--max-recover`, `--no-auto-recover`, `--no-recycle`, `--poll-s`, `--stall-s`, `--resilience-profile` | no | keep internal (tuning); document as developer knobs |
| reviewer/planner options | `--refuter`, `--max-refute`, `--panel`, `--max-research`, `--accuracy`, `--plan` (`--plan` is GUI) | no (except `--plan`) | add GUI only if they are meant for operators; otherwise keep internal |
| autoscale internals | `--autoscale-default`, `--autoscale-headroom-mb`, `--autoscale-up-margin-mb`, `--eval-disk-gb` | no | keep internal |
| plumbing | `--goals-file`, `--agent-url`, `--adopt-command`, `--force`, `--resume` | no (launcher-supplied) | keep internal |

Count: about 28 flags with no GUI path; none of them is a product setting an operator is told to
change.

## C. Environment variables

197 distinct `MCP_*` / `FLEET_*` names are read by product code; 10 of them are even mentioned in
the GUI sources (endpoints, paths and the access list). The rest are timeouts, backoffs, retention
and probe tuning. The ones an operator could plausibly want:

| Name | What it does | GUI path | Recommendation |
| --- | --- | --- | --- |
| `FLEET_INTAKE_AUTOSTART` | start a fleet when a goal arrives with none running | no | add a GUI toggle (it decides whether tunnel goals run at all) |
| `FLEET_INTAKE_AUTOSTART_FANOUT` | forces fan-out on/off for autostart, beating the setting | no | remove, or show in the "in effect" line as an override (the setting is the GUI path) |
| `FLEET_INTAKE_AUTOSTART_FANOUT_MIN_CHARS` | `<= 0` is a hidden fan-out kill switch; otherwise unused | no | remove (the number no longer gates anything) |
| `MCP_QUOTA_RPM`, `MCP_FLEET_MAX_CONCURRENT`, `MCP_FLEET_RAM_FLOOR_MB`, `MCP_FLEET_PER_TAB_MB`, `MCP_FLEET_LOG_DAYS`, `MCP_FLEET_STORE_DAYS`, `MCP_FLEET_SCRATCH_DAYS`, `MCP_FLEET_COMPRESS_HOURS` | env spelling of a settings key | yes, via the key | keep as overrides; document that env beats the file |
| `MCP_EFFORT_POLICY` | env override of `effort_policy` | yes (the box shows a conflict warning) | keep |
| `MCP_RETRY_UNVERIFIED_DONE` | retry a DONE that was not verified | no | decide: GUI toggle or remove |
| all other `MCP_FLEET_SOCKET_*`, `MCP_BRIDGE_*`, `MCP_CAPTURE_*`, probe and settle timings | transport and diagnostics tuning | no | keep internal; not product features |

## D. Supervisor parameters and scripts

| Name | What it does | GUI path | Recommendation |
| --- | --- | --- | --- |
| `supervisor.ps1 -FleetCycleResumeLive` | per-cycle resume of an interrupted fleet is a dry run unless passed | no | add a GUI toggle ("resume interrupted fleet automatically") or make it the default; today the live behavior is CLI-only |
| `supervisor.ps1 -FleetResumeDryRun` | log what a startup resume would do | no | keep internal (diagnostic) |
| `supervisor.ps1 -TunnelName/-Port/-IntervalSeconds/-FailuresBeforeAction/-StartupGraceSeconds` | process wiring and debounce | no | keep internal |
| `scripts/win/resume_interrupted_fleet.py --resume` | resume an interrupted run | cockpit has its own resume path | keep |
| `run_swe_via_ui.ps1`, `run_bestofn.ps1`, `run_effort_ab.ps1` | bench drivers that edit the operator's settings file by hand | no | keep internal, but they must not leave changed values behind; prefer passing flags to their own launches |

## Summary

- Settings keys: 1 of 31 without GUI (`autoscale_per_tab_mb`); `fanout` fixed in this change.
- Runner flags: about 28 of 37 CLI-only, all tuning or plumbing.
- Environment: 197 names, about 187 not in any GUI source, nearly all internal tuning.
- Top items to add to the GUI next: `FLEET_INTAKE_AUTOSTART`, `-FleetCycleResumeLive`,
  `autoscale_per_tab_mb`, `MCP_RETRY_UNVERIFIED_DONE`.
- Top items to remove: `FLEET_INTAKE_AUTOSTART_FANOUT_MIN_CHARS`; the env override
  `FLEET_INTAKE_AUTOSTART_FANOUT` once the GUI box is the way to choose.

This change does not remove or add any of these except the fan-out box; the table is the backlog.
