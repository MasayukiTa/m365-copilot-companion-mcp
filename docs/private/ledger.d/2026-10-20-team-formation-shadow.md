## 2026-10-20 - Dynamic team formation (C4), shadow only

- Change: setting `team_mode` (off|shadow, default off, each_gate). New module
  `relay/team_formation.py`. At each real split in `RelayWorker._decide` (children built, right
  before they are handed to the spawner) it appends one row to `.fleet/team_formation_shadow.jsonl`:
  the team the static split built ({subtask: N, aggregator: 1}) and the team a small rule would
  have proposed. Rules: `fold_tiny` (steps under 40 characters are folded in pairs, never below 2
  children) and `review` (one `reviewer` is added when the role profile labels subtask or
  aggregator success as poor; unknown adds nothing and the row says `profile_known: false`).
- Behaviour: SHADOW ONLY. The steps, children, prompts, queue and merge are unchanged. In off
  nothing is read or written. A write error is counted and swallowed. A row holds role names,
  counts and lengths, never goal or step text. The log rotates at 1 MiB to `.1`. A retried split
  of one campaign is one row. There is no value that forms a team.
- Report: `scripts/team_formation_report.py` (read-only) says how often the proposed team differs
  and which rule did it. Enough data = 60 splits, 30 with a known profile, 5 runs, 3 distinct
  shapes. Enough data and >= 10% differing means "run the paired comparison (C0)", not "switch
  on": the proposed team was never run, so a row has no outcome to compare.
- Cockpit: an off/shadow selector in the settings popup beside load-aware routing (SaveKey only),
  in-effect line from the additive status.json field `team_mode: {mode, recorded, differed}`.
- Tests: relay/test_team_formation.py (rule, recorder, bounds, failure safety, and a worker driven
  through a split with the mode off, shadow, and shadow with an unwritable log: children, spawner
  arguments, next prompt, status, outcome and reason byte-identical),
  tests/test_team_mode_agrees.py, scripts/test_team_formation_report.py.
- Open: the profile only labels success for roles that have recorded outcomes; subtask and
  aggregator turns are unknown, so the rule cannot use cost yet. Expect `profile_known` to stay
  low until enough campaigns have merged.
