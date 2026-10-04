## 2026-10-02 - Fan-out budget no longer disabled by a large campaigns ledger

- Change: relay/fanout_budget.py `read_campaign_rows` refused campaigns.jsonl past 2 MB (returned
  None = usage unknown), so on any machine whose ledger had grown every split failed closed and
  fan-out was effectively off. The ledger is now streamed line by line into a header index
  (only campaign headers, only the fields the budget reads), cached by (mtime, size) and extended
  incrementally on append; the CPU bound is 50 MB. A top-level split (no root_id) is a brand-new
  root, so its usage is zero by construction and the ledger is not read (`ledger_rows_for_split`).
  Only a nested split reads its own root's rows, and only an unreadable ledger refuses it, with a
  `fanout_budget_usage_unknown` mechanism row. The status.json `tree_budget` export uses the same
  reader (it previously exported nothing past 2 MB) and lists trees with live workers first.
- Retention: campaigns.jsonl is NOT compacted in this slice. Added a `campaigns_ledger_large`
  mechanism warning past 8 MB (relay/fleet_retention.py, called from apply) and a skipped test spec
  for the compaction (tests/test_campaigns_ledger_retention.py).
- Tests: tests/test_fanout_budget_large_ledger.py (synthetic 7 MB ledger, first-level grant, nested
  limits, unreadable refuses nested only, golden against the old whole-file reader, id-prefix
  isolation, incremental append, timing guard); tests/test_fanout_budget.py status test now points
  at a state dir.
- Open: compaction of finished families into campaigns.jsonl.1; the split-group view
  (relay/fleet_runner.py `_campaign_lines`) still returns nothing past 2 MB.
