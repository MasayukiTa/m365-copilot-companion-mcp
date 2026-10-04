## 2026-10-02 - splittability judge is depth-aware

- Change: `splittability.judge(text, depth=0, max_depth=None)`. A goal carrying the child scope
  markers used to be NO_SPLIT unconditionally, so a depth-1 child was never asked to split even
  with the effective depth at 2 (depth 2 was unreachable). Now, when `0 < depth < max_depth`
  and the text has a scope block, the heuristics run on the child's own slice
  (`child_own_slice`, via `fanout._own_scope_step`), not on the parent goal and the contract.
  One call site: `RelayWorker.__init__` passes the envelope depth and
  `fanout.effective_max_depth()`.
- Behaviour: with the default arguments, or at effective depth 1 (including a configured 2
  with hierarchical merge off), every text judges exactly as before (golden digest). At
  effective depth 2 a child with 2-3 independent parts is UNCERTAIN (asked), a single small
  slice is NO_SPLIT, a grandchild and an aggregator never split. No default or setting changed.
- Tests: `relay/test_judge_is_depth_aware.py` (registered in ci.yml).
- Open: the live exit verification at depth 2 with hierarchical merge on.
