## 2026-10-02 - Nested split groups in relay.family_view

- Change: `build_groups` now returns nested groups using the recorded tree identity (campaign
  header parent_task_id / parent_campaign_id / root_id, worker-row parent_task_id) read through
  `relay.task_tree`. The old flat logic is `build_flat_groups` (unchanged); `task_tree` calls that
  one so the two modules cannot recurse. `render_text` indents nested groups by depth.
- Behaviour: additive keys parent_group_id, root_id, depth, child_group_ids, descendant_count,
  descendant_turns, orphan. merge_state may be waiting_on_subgroups or unknown (derived, never
  guessed). A flat fleet keeps every existing key byte-identical. Caps: MAX_DEPTH, MAX_CHILD_IDS.
  The goal text is still never included. fleet_runner is untouched; its caller carries the keys.
- Tests: relay/test_family_view_nesting.py (flat identical, two-level tree, orphan, caps, loop,
  waiting vs unknown, render indentation), registered in ci.yml.
- Open: the UI does not read the new keys yet.
