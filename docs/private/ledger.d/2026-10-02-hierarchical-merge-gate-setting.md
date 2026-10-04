## 2026-10-02 - Hierarchical merge gate becomes a GUI setting (default off, behaviour unchanged)

- Change: new each_gate setting `fanout_hierarchical_merge` (off|on, default off) in
  `tools/settings_keys.py`. `relay/fanout.py` adds `hierarchical_merge_setting()` and
  `hierarchical_merge_enabled()`; `effective_max_depth()` returns the configured depth only
  when the setting is on, else `min(configured, 1)`. `HIERARCHICAL_MERGE_READY` stays as a
  test hook only (production never sets it). status.json `fanout_depth` keeps
  configured/effective/reason (reason now "hierarchical merge setting is off") and gains
  `hierarchical_merge`. The cockpit has an off/on selector beside the split depth
  (`HierarchicalMergeView` in `ui/EffortPolicy.cs`, SaveKey only, no re-fire on paint).
- Behaviour: with the setting off or absent the effective depth is 1 for configured 2/3, as on
  main (golden digests in `relay/test_recursive_fanout_depth.py` unchanged). The owner flips it
  in the GUI after verifying with small goals.
- Tests: `tests/test_hierarchical_merge_setting.py` (registered in ci.yml), a reader entry in
  `tools/test_a_setting_declares_when_it_takes_effect.py`, updated report assertions in
  `relay/test_recursive_fanout_depth.py`.
- Open: nested merging has not been verified live; keep the setting off until then.
