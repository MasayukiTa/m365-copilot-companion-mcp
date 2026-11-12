import json
import sqlite3

import pytest

from scripts import ledger_dump


@pytest.mark.parametrize("prefix", ["", "a", "abc", "日本", "\ufffe", "\uffff",
                                    "\ud7ff", "\U0010ffff", "a\U0010ffff"])
def test_prefix_range_matches_old_query_and_uses_index(tmp_path, prefix):
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE fleet_turns (id INTEGER PRIMARY KEY, key TEXT, role TEXT, "
                 "turn INTEGER, text TEXT, extra TEXT, ts REAL)")
    conn.execute("CREATE INDEX fleet_turns_key_idx ON fleet_turns(key, id)")
    keys = ["", "a", "abc", "abcd", "abd", "日本", "日本語", "日木", "\ud7ff",
            "\ue000", "\ufffe", "\uffff", "\uffffz", chr(0x10000),
            "\U0010ffff", "\U0010ffffx", "a\U0010ffff", "a\U0010ffffx", "b"]
    for i, key in enumerate(keys):
        conn.execute("INSERT INTO fleet_turns VALUES (?, ?, ?, 0, '', '', ?)",
                     (i + 1, key, "refuter_user", len(keys) - i))
    conn.execute("INSERT INTO fleet_turns VALUES (100, ?, 'user', 0, '', '', 0)", (prefix,))
    old = conn.execute("SELECT id FROM fleet_turns WHERE substr(key, 1, ?) = ? "
                       "AND role LIKE 'refuter\\_%' ESCAPE '\\' ORDER BY ts, id",
                       (len(prefix), prefix)).fetchall()
    assert [r["id"] for r in ledger_dump._prefix_rows(conn, prefix)] == [r["id"] for r in old]
    sql, args = ledger_dump._prefix_query(prefix)
    plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]
    assert any("SEARCH fleet_turns USING INDEX fleet_turns_key_idx" in item for item in plan), plan
    conn.close()


def test_error_reports_exception_and_elapsed_time(monkeypatch, capsysbinary):
    def fail(_key):
        raise RuntimeError("read failed in detail")
    times = iter([10.0, 12.5])
    monkeypatch.setattr(ledger_dump, "dump", fail)
    monkeypatch.setattr(ledger_dump.time, "monotonic", lambda: next(times))
    assert ledger_dump.main(["--key", "example"]) == 0
    result = json.loads(capsysbinary.readouterr().out)
    assert result["error"] == "Elapsed 2.50 s: RuntimeError: read failed in detail"
    assert result["turns"] == [] and result["recorded"] is False
