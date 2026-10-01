# -*- coding: utf-8 -*-
"""Ledger entries live in one file each and compile deterministically (no shared tail)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ledger_compile as lc  # noqa: E402


def _entry(d, name, title):
    (d / name).write_text(f"## {title}\n- body\n", encoding="utf-8")


def test_compile_is_baseline_then_entries_in_filename_order(tmp_path):
    base = tmp_path / "base.md"
    base.write_text("# L\n## old\n- x\n", encoding="utf-8")
    d = tmp_path / "ledger.d"
    d.mkdir()
    _entry(d, "2026-10-03-b.md", "2026-10-03 - B")
    _entry(d, "2026-10-02-z.md", "2026-10-02 - Z")
    (d / "README.md").write_text("not an entry\n", encoding="utf-8")
    out = lc.compile_ledger(base, d)
    assert out.startswith("# L\n## old\n- x\n")
    assert out.index("2026-10-02 - Z") < out.index("2026-10-03 - B")
    assert "not an entry" not in out
    assert out == lc.compile_ledger(base, d)


def test_bad_entries_are_reported(tmp_path):
    d = tmp_path / "ledger.d"
    d.mkdir()
    (d / "Bad Name.md").write_text("no heading", encoding="utf-8")
    msgs = "\n".join(lc.problems(d))
    assert "name must be" in msgs and "heading" in msgs and "newline" in msgs


def test_repository_entries_are_valid_and_baseline_is_untouched_by_compile():
    assert lc.problems() == []
    base = lc.BASELINE.read_text(encoding="utf-8")
    assert lc.compile_ledger().startswith(base.rstrip("\n") + "\n")


def test_cli_check_runs():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_compile.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr