## 2026-11-07 - result cache shadow report takes a `--tool` filter (K3 PR 2)

- Change. `scripts/result_cache_report.py` gains a repeatable `--tool NAME` argument
  (`action="append"`, default `None`). When present, the rows read from the shadow
  record are narrowed to the exact tool names given (`row.get("tool") in selected`)
  before any summarising, so every downstream view -- the default verdict report,
  the `--same-fingerprint` pairing, and the `--by-tool` breakdown -- sees only the
  selected tools. The selection is echoed back: JSON output carries a sorted
  `selected_tools` key and text output is prefixed with `Selected tools: ...` so a
  filtered report can never be mistaken for the whole-population one.

- Behaviour. The script stays read-only; the filter only removes rows from the
  in-memory view and never touches the record. Narrowing to one tool changes the
  sample the verdict thresholds are applied to: a population that reads
  UNSAFE_TO_SERVE in aggregate can read WORTH_A_SERVING_TEST once restricted to a
  single well-behaved tool, and an unknown tool name yields an empty sample that
  reads INSUFFICIENT rather than a false pass. Default (no `--tool`) is unchanged:
  no `selected_tools` key, no prefix, whole-population verdict as before.

- Tests. New `test_tool_filter_changes_verdict_and_applies_sample_thresholds` in
  `scripts/test_result_cache_report.py` pins all four facets: the unfiltered
  UNSAFE_TO_SERVE baseline, a single-tool filter flipping the verdict to
  WORTH_A_SERVING_TEST with `selected_tools == ["read_file"]`, an unknown tool
  giving INSUFFICIENT, and a two-tool text run emitting
  `Selected tools: list_directory, read_file`. Verified green in CI on PR #457
  (all 10 checks pass, including the hermetic `test` job and the ledger-compile
  check).

- Open. None. The slice shipped via PR #457 (squash-merged to main as `9ff412b0`);
  this ledger entry is the after-the-fact record for that merge, mirroring the
  prior `ELN entry ... (PR #454)` pattern. The original unpushed local branch
  `feat/k3-cache-safety_terra` held the same change and is fully superseded by the
  merged superset.

