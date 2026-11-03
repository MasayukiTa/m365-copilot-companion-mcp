"""CLI-level tests for the weekly-scan (a)+(b) driver in --dry-run mode.

Dry-run uses no-op providers (no DB, no judge), so these tests prove the
orchestration, ledger round-trip and summary line on real-shaped ledger data
without side effects. No Windows-specific paths.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.weekly_scan import cli


def _write_registered(docs_root: Path, dir_rel: str, k_req):
    d = docs_root / dir_rel
    d.mkdir(parents=True, exist_ok=True)
    obj = {
        "verdict": {
            "verdict": "HOLD",
            "reasons": ["D_FAIL_NO_LOCK"],
            "hashes": {"lock_sha256": None},
            "diagnostics": {"capacity": {"k_required_p80": k_req}},
        }
    }
    (d / "judge_result.json").write_text(json.dumps(obj), encoding="utf-8")


def test_cli_dry_run_processes_bcd_and_writes_ledger(tmp_path, capsys):
    docs = tmp_path / "docs"
    _write_registered(docs, "b1", 100)
    _write_registered(docs, "c1", 200)
    _write_registered(docs, "d1", None)
    # Use a past date as the pre-scan value so "scanned today" is observably
    # different from it regardless of the real calendar date during the run.
    OLD = "2000-01-01"
    rows = [
        {"analysis_id": "b1", "class": "B", "dir": "b1", "verdict": "HOLD",
         "last_scanned": OLD},
        {"analysis_id": "c1", "class": "C", "dir": "c1", "verdict": "HOLD",
         "last_scanned": OLD},
        {"analysis_id": "d1", "class": "D", "dir": "d1", "verdict": "ERROR",
         "reasons": "INPUT_NO_ROWS", "last_scanned": OLD},
        {"analysis_id": "a1", "class": "A", "dir": "a1", "verdict": "HOLD",
         "last_scanned": OLD},
    ]
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    rc = cli.main(["--ledger", str(ledger), "--docs-root", str(docs), "--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "B/C rows=2" in out
    assert "D rows=1" in out

    back = json.loads(ledger.read_text(encoding="utf-8"))
    by_id = {r["analysis_id"]: r for r in back}
    # B, C, D were scanned today; A was left untouched (still the old value).
    assert by_id["b1"]["last_scanned"] != OLD
    assert by_id["c1"]["last_scanned"] != OLD
    assert by_id["d1"]["last_scanned"] != OLD
    assert by_id["a1"]["last_scanned"] == OLD


def test_cli_no_write_leaves_ledger_unchanged(tmp_path, capsys):
    docs = tmp_path / "docs"
    _write_registered(docs, "b1", 100)
    rows = [{"analysis_id": "b1", "class": "B", "dir": "b1",
             "last_scanned": "2026-11-04"}]
    ledger = tmp_path / "ledger.json"
    original = json.dumps(rows, ensure_ascii=False)
    ledger.write_text(original, encoding="utf-8")

    rc = cli.main(["--ledger", str(ledger), "--docs-root", str(docs),
                   "--dry-run", "--no-write"])
    assert rc == 0
    assert ledger.read_text(encoding="utf-8") == original


def test_cli_non_dry_without_foundation_returns_2(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    ledger = tmp_path / "ledger.json"
    ledger.write_text("[]", encoding="utf-8")
    rc = cli.main(["--ledger", str(ledger), "--docs-root", str(docs)])
    # Foundation (subtask 2) absent -> clean exit code 2, not a crash.
    assert rc == 2
