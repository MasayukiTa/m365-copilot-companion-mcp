## 2026-10-02 - ledger merge conflicts: union attribute and per-entry files
- Change (branch chore/ledger-union-merge-20261002): `.gitattributes` marks the ledger `merge=union` (local merges keep both appended blocks; measured in a scratch repo: without it append/append conflicts, with it both blocks merge cleanly; edits to the same earlier line on both sides are both kept silently, acceptable because earlier lines are never edited). GitHub's merge button ignores merge=union, so new entries go in docs/private/ledger.d/<date>-<slice>.md and scripts/ledger_compile.py compiles baseline + entries.
- Behaviour: the baseline ledger is unchanged. Entries are validated by `ledger_compile.py --check` in CI.
- Tests: tests/test_ledger_compile.py (in ci.yml). Open: PR review/merge.
