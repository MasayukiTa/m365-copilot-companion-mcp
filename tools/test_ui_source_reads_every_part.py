"""tools/ui_source.py hands a test the WHOLE source of a split UI unit, and only that unit's.

ui/CopilotChat.cs and ui/SelfImproveDashboard.cs are being split into partial-class parts
(`CopilotChat.<Topic>.cs`, `SelfImproveDashboard.<Topic>.cs`). The ~24 tests that assert on their
source read through tools/ui_source.py so that moving a method into a part does not turn an
`assert "..." in src` red, and -- worse -- does not turn an `assert "..." not in src` green for the
wrong reason. This file pins the two things that make that safe:

1. the concatenation: main part first, then the topic parts in sorted file-name order, each part
   decoded on its own, joined by a newline, a byte-order mark inside the text dropped;
2. the part rule does not collide: a file in the repository's real ui/ that is not a part of the
   class must never be picked up (ChatSend.cs, StartupGate.cs, FleetCockpit.*.cs ...), and every
   file the rule does pick up must declare the partial class it belongs to.

The first test builds fake parts in a temp directory, so it keeps proving the rule after the real
files have been split. The second reads the real ui/ folder.
"""
import os
import re

import pytest

from tools import ui_source

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(REPO, "ui")

# The class each stem's parts must declare (`partial class <Name>` once split; today a plain
# `class <Name>` in the single file).
CLASS_OF = {"CopilotChat": "ChatWindow", "SelfImproveDashboard": "SelfImproveDashboardWindow"}


def _write(d, name, text, bom=False):
    with open(os.path.join(str(d), name), "wb") as fh:
        fh.write((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))


def test_the_parts_are_joined_main_first_then_by_name(tmp_path):
    _write(tmp_path, "CopilotChat.cs", "MAIN", bom=True)
    _write(tmp_path, "CopilotChat.Store.cs", "STORE", bom=True)
    _write(tmp_path, "CopilotChat.Messages.cs", "MESSAGES")
    _write(tmp_path, "CopilotChat.Settings.cs", "SETTINGS")
    # decoys: none of these is a part of CopilotChat
    _write(tmp_path, "ChatSend.cs", "DECOY_SEND")
    _write(tmp_path, "CopilotChatX.cs", "DECOY_X")
    _write(tmp_path, "CopilotChat.a.b.cs", "DECOY_TWO_SEGMENTS")
    _write(tmp_path, "CopilotChat.exe.config", "DECOY_CONFIG")
    _write(tmp_path, "StartupGate.cs", "DECOY_GATE")
    _write(tmp_path, "FleetCockpit.Chrome.cs", "DECOY_COCKPIT")
    _write(tmp_path, "SelfImproveDashboard.cs", "SID_MAIN")
    _write(tmp_path, "SelfImproveDashboard.Widgets.cs", "SID_WIDGETS")
    _write(tmp_path, "SelfImproveDashboard.Authority.cs", "SID_AUTH")

    got = [os.path.basename(p) for p in ui_source.part_paths("CopilotChat", ui_dir=str(tmp_path))]
    assert got == ["CopilotChat.cs", "CopilotChat.Messages.cs", "CopilotChat.Settings.cs",
                   "CopilotChat.Store.cs"], got

    # utf-8-sig drops every BOM; plain utf-8 keeps the first part's (as open() on the one file did)
    # but never leaves one in the middle of the text.
    assert ui_source.chat_window_source(ui_dir=str(tmp_path)) == "MAIN\nMESSAGES\nSETTINGS\nSTORE"
    assert ui_source.chat_window_source(encoding="utf-8", ui_dir=str(tmp_path)) == \
        "\ufeffMAIN\nMESSAGES\nSETTINGS\nSTORE"
    assert ui_source.read_parts("CopilotChat", ui_dir=str(tmp_path)) == \
        ui_source.chat_window_source(ui_dir=str(tmp_path))

    # the other unit is independent of the first
    assert ui_source.self_improve_source(ui_dir=str(tmp_path)) == "SID_MAIN\nSID_AUTH\nSID_WIDGETS"
    assert [os.path.basename(p) for p in ui_source.self_improve_paths(ui_dir=str(tmp_path))] == \
        ["SelfImproveDashboard.cs", "SelfImproveDashboard.Authority.cs",
         "SelfImproveDashboard.Widgets.cs"]


def test_a_single_unsplit_file_reads_exactly_as_open_did(tmp_path):
    text = "// header\r\nclass ChatWindow {\r\n}\r\n"
    _write(tmp_path, "CopilotChat.cs", text, bom=True)
    with open(os.path.join(str(tmp_path), "CopilotChat.cs"), encoding="utf-8-sig") as fh:
        expected = fh.read()
    assert ui_source.chat_window_source(ui_dir=str(tmp_path)) == expected
    assert "\r" not in expected  # universal newlines, as in every test that read the file before


def test_encoding_and_errors_are_passed_through(tmp_path):
    with open(os.path.join(str(tmp_path), "CopilotChat.cs"), "wb") as fh:
        fh.write(b"ok \xff bad")
    with pytest.raises(UnicodeDecodeError):
        ui_source.chat_window_source(ui_dir=str(tmp_path))
    assert ui_source.chat_window_source(errors="replace", ui_dir=str(tmp_path)) == "ok \ufffd bad"


def test_no_part_at_all_is_an_error_not_an_empty_string(tmp_path):
    _write(tmp_path, "ChatSend.cs", "unrelated")
    with pytest.raises(FileNotFoundError):
        ui_source.chat_window_source(ui_dir=str(tmp_path))
    assert ui_source.part_paths("CopilotChat", ui_dir=str(tmp_path)) == []
    assert ui_source.part_paths("CopilotChat", ui_dir=os.path.join(str(tmp_path), "missing")) == []


def test_the_part_rule_accepts_topics_and_nothing_else():
    ok = ui_source.is_part
    assert ok("CopilotChat", "CopilotChat.cs")
    assert ok("CopilotChat", "CopilotChat.Store.cs")
    assert ok("CopilotChat", "CopilotChat.Fleet_Conv2.cs")
    assert not ok("CopilotChat", "CopilotChat.cs.bak")
    assert not ok("CopilotChat", "CopilotChat..cs")
    assert not ok("CopilotChat", "CopilotChat.1x.cs")
    assert not ok("CopilotChat", "CopilotChat.a.b.cs")
    assert not ok("CopilotChat", "copilotchat.Store.cs")
    assert not ok("CopilotChat", "CopilotChatStore.cs")
    assert not ok("CopilotChat", "CopilotChat.Store.cs.orig")
    assert not ok("CopilotChat", "SelfImproveDashboard.Store.cs")
    # the stem is matched literally, not as a pattern
    assert not ok("Copilot.hat", "CopilotXhat.cs")


@pytest.mark.parametrize("stem", sorted(CLASS_OF))
def test_the_real_ui_names_do_not_collide_with_the_part_rule(stem):
    """Every real ui/*.cs the rule picks up must belong to the class; none may belong to two units."""
    names = sorted(n for n in os.listdir(UI) if n.endswith(".cs"))
    picked = [n for n in names if ui_source.is_part(stem, n)]
    assert stem + ".cs" in picked, "the main part ui/%s.cs is gone" % stem
    for other in sorted(CLASS_OF):
        if other != stem:
            both = [n for n in picked if ui_source.is_part(other, n)]
            assert not both, "%s are parts of both %s and %s" % (both, stem, other)
    # part_paths agrees with the rule applied to the directory listing
    assert [os.path.basename(p) for p in ui_source.part_paths(stem)] == [stem + ".cs"] + \
        [n for n in picked if n != stem + ".cs"]
    decl = re.compile(r"\b(?:partial\s+)?class\s+" + re.escape(CLASS_OF[stem]) + r"\b")
    for n in picked:
        with open(os.path.join(UI, n), encoding="utf-8-sig") as fh:
            src = fh.read()
        assert decl.search(src), "ui/%s matches the part rule of %s but declares no class %s" % (
            n, stem, CLASS_OF[stem])


def test_the_neighbouring_files_are_not_parts():
    """The names that share a prefix or a topic style with the two units must stay out."""
    for stem in CLASS_OF:
        for n in ("ChatSend.cs", "StartupGate.cs", "Theme.cs", "Markdown.cs", "FleetCockpit.cs",
                  "FleetCockpit.Chrome.cs", "BridgeClient.cs", "FleetConvIdentity.cs",
                  "CopilotChat.exe.config"):
            assert not ui_source.is_part(stem, n), (stem, n)
