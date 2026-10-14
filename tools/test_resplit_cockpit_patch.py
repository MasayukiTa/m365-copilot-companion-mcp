"""tools/resplit_cockpit_patch.py places an edit written against the OLD single-file cockpit onto the
split parts by CONTENT, preserves each part's line endings and BOM, and refuses to guess.

Synthetic parts only: the real ones are checked by building them (bench/ui_build_check.py)."""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import resplit_cockpit_patch as R  # noqa: E402

MAIN = ["class CockpitWindow : Window", "{", "    int _a;", "    void Ctor() { }", "}"]
PART_A = ["partial class CockpitWindow", "{", "    void Alpha()", "    {", "        Work(1);", "        Work(2);",
          "    }", "    void Same() { Dup(); }", "}"]
PART_B = ["partial class CockpitWindow", "{", "    void Beta()", "    {", "        Other(1);", "    }",
          "    void Same() { Dup(); }", "}"]


def _write(path, lines, crlf, bom):
    data = ("\r\n" if crlf else "\n").join(lines) + ("\r\n" if crlf else "\n")
    with open(path, "wb") as fh:
        fh.write((b"\xef\xbb\xbf" if bom else b"") + data.encode("utf-8"))


def _tree(tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    _write(str(ui / "FleetCockpit.cs"), MAIN, True, True)
    _write(str(ui / "FleetCockpit.A.cs"), PART_A, True, True)
    _write(str(ui / "FleetCockpit.B.cs"), PART_B, False, False)
    return ui


def _patch(tmp_path, body):
    p = tmp_path / "e.patch"
    p.write_text("--- a/ui/FleetCockpit.cs\n+++ b/ui/FleetCockpit.cs\n" + body, encoding="utf-8")
    return str(p)


def test_a_hunk_goes_to_the_part_that_now_holds_the_lines(tmp_path):
    ui = _tree(tmp_path)
    patch = _patch(tmp_path, "@@ -900,5 +900,5 @@\n     {\n         Other(1);\n-    }\n+        Other(9);\n+    }\n")
    assert R.main([patch, "--ui-dir", str(ui), "--apply"]) == 0
    b = (ui / "FleetCockpit.B.cs").read_bytes()
    assert b"Other(9);" in b and b"\r\n" not in b and not b.startswith(b"\xef\xbb\xbf")
    a = (ui / "FleetCockpit.A.cs").read_bytes()
    assert b"Other" not in a and a.startswith(b"\xef\xbb\xbf") and b"\r\n" in a


def test_an_edit_in_a_crlf_part_keeps_crlf_and_bom(tmp_path):
    ui = _tree(tmp_path)
    patch = _patch(tmp_path, "@@ -1,3 +1,3 @@\n     {\n-        Work(1);\n+        Work(7);\n         Work(2);\n")
    assert R.main([patch, "--ui-dir", str(ui), "--apply"]) == 0
    a = (ui / "FleetCockpit.A.cs").read_bytes()
    assert a.startswith(b"\xef\xbb\xbf") and b"Work(7);\r\n" in a and b"Work(1)" not in a


def test_context_spanning_two_parts_is_trimmed_not_guessed(tmp_path):
    ui = _tree(tmp_path)
    # the context line below the edit does not exist next to it in any part
    patch = _patch(tmp_path, "@@ -1,3 +1,3 @@\n         Work(2);\n-    }\n+        Work(3);\n+    }\n     void Gone() { }\n")
    assert R.main([patch, "--ui-dir", str(ui), "--apply"]) == 0
    assert b"Work(3);" in (ui / "FleetCockpit.A.cs").read_bytes()


def test_an_ambiguous_or_missing_hunk_writes_nothing(tmp_path):
    ui = _tree(tmp_path)
    before = {p.name: p.read_bytes() for p in ui.iterdir()}
    amb = _patch(tmp_path, "@@ -1,1 +1,1 @@\n-    void Same() { Dup(); }\n+    void Same() { Dup2(); }\n")
    assert R.main([amb, "--ui-dir", str(ui), "--apply"]) == 1
    miss = _patch(tmp_path, "@@ -1,1 +1,1 @@\n-    void Nowhere() { }\n+    void Nowhere() { X(); }\n")
    assert R.main([miss, "--ui-dir", str(ui), "--apply"]) == 1
    assert {p.name: p.read_bytes() for p in ui.iterdir()} == before


def test_dry_run_changes_nothing(tmp_path):
    ui = _tree(tmp_path)
    before = {p.name: p.read_bytes() for p in ui.iterdir()}
    patch = _patch(tmp_path, "@@ -1,3 +1,3 @@\n     {\n-        Work(1);\n+        Work(7);\n         Work(2);\n")
    assert R.main([patch, "--ui-dir", str(ui)]) == 0
    assert {p.name: p.read_bytes() for p in ui.iterdir()} == before
