## 2026-11-09 - close cancels only a worker's TRUE descendants, not its siblings or parent (PR #469 review fix)

- Finding. PR #468 (fleet: close also cancels already-running fan-out descendants, squash-merged to main as 544d0ad0) selected the live workers to cancel by a shared id union, so closing ONE fan-out child also matched its siblings and its parent -- they all carry the same campaign_id/root_id -- and would cancel the whole tree.

- Fix. relay/fleet_tombstone.py: descendants_to_cancel now anchors ONLY on a closed worker's OWN identity (jid, task_id) via closing_anchor_ids. A group-root id (campaign_id/root_id) becomes an anchor ONLY when the closed worker IS the root of that tree (the group id equals its own jid/task_id). Selection walks the ancestor chain (parent_task_id -> that parent's parent -> ... / inherited jid) to a fixed point, so a grandchild two levels down is still caught, while siblings and the parent are spared. _inherited_ancestor_ids drops the worker's own task_id/jid so a worker is never its own ancestor.

- Tests. relay/test_fleet_close_descendants.py pins the finding: test_closing_one_child_does_not_cancel_its_sibling_or_its_parent, test_closing_a_child_cancels_only_its_own_grandchild_not_the_tree, test_a_sibling_sharing_only_a_different_campaign_is_not_cancelled, plus multi-level and dedupe cases. Local run: 23 passed. Full suite via pre-push hook: 27 passed.

- Delivery. Branch fix/closed-child-no-resurrect (PR #469, base main) updated to db902cb9 (Merge main #468 squashed into #469, keeping the 469 child_blocked no-resurrect fix + its CI test, preserving main-side changes). PR is MERGEABLE. gh pr checks 468: all pass. gh pr checks 469: quoted in the completion report.

- Open. None pending beyond the PR469 CI run completing green.
