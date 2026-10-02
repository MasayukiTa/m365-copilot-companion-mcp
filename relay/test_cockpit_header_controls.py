"""The cockpit header is a PINNED set of controls.

Why this exists: six slices in a row (effort policy, fan-out on/off, depth, the four budget
limits, write scope, hierarchical merge) each put their own control in the header, and the row
became unreadable. Those controls now live in the gear popup (BuildSettingsPanel).

IF THIS TEST FAILS BECAUSE YOU ADDED A CONTROL TO THE HEADER: do not edit the list below. Put
the setting in the settings popup instead (a row in an existing section, or a new section
there). Only change this list when the owner has asked for a new header control.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Exactly what the header composes, in order. (The set from before the effort-policy slice.)
HEADER = [
    "_workerChipBorder",      # read-only live tab chip, hidden while idle
    "EffortControl()",        # reasoning effort
    "ApprovalControl()",      # run mode
    "ApprovalCenterControl()",  # pending approvals
    "FleetControls()",        # pause / stop
    "SettingsControl()",      # the gear -> settings popup
    "_langBtn",
    "_themeBtn",
    "OverflowControl()",
]

# Builders that must live in the settings popup and never in the header.
POPUP = [
    "EffortPolicyControl()",
    "FanoutControl()",
    "FanoutDepthControl()",
    "HierarchicalMergeControl()",
    "WriteScopeControl()",
    "FanoutBudgetControl()",
]


def _src():
    with open(os.path.join(REPO, "ui", "FleetCockpit.cs"), encoding="utf-8") as f:
        return f.read()


def _code(src):
    return re.sub(r"//[^\n]*", "", src)


def test_the_header_composes_exactly_the_pinned_controls():
    adds = re.findall(r"\bctrls\.Children\.Add\((.+?)\);", _code(_src()))
    assert adds == HEADER, (
        "header controls changed: put new settings in the gear popup (BuildSettingsPanel) "
        "instead of the header. got %r" % (adds,))


def test_the_header_is_no_larger_than_before_the_effort_policy_slice():
    assert len(HEADER) <= 9


def test_the_moved_controls_live_in_the_settings_popup():
    src = _code(_src())
    start = src.index("UIElement BuildSettingsPanel()")
    end = src.index('SectionHeader(L("詳細設定", "Advanced"))', start)
    panel = src[start:end]
    positions = []
    for b in POPUP:
        assert "col.Children.Add(%s);" % b in panel, b
        assert "ctrls.Children.Add(%s);" % b not in src, b
        positions.append(panel.index("col.Children.Add(%s);" % b))
    # one labelled fan-out section holding the fan-out controls
    assert 'SectionHeader(L("分割 / Fan-out", "Fan-out"))' in panel
    sect = panel.index('SectionHeader(L("分割 / Fan-out", "Fan-out"))')
    for b in POPUP[1:]:
        assert panel.index("col.Children.Add(%s);" % b) > sect, b
    # the effort policy sits in its own effort section, before the fan-out one
    eff = panel.index('SectionHeader(L("推論", "Effort"))')
    assert eff < panel.index("col.Children.Add(EffortPolicyControl());") < sect


def test_the_popup_controls_are_repainted_when_settings_txt_changes():
    src = _code(_src())
    m = re.search(r"LoadSettings\(\);\s*(.*?)if \(d0 != _dark\)", src, re.S)
    assert m, "OnTick must repaint the popup controls right after LoadSettings()"
    for call in ("PaintEffortPolicy();", "PaintFanout();"):
        assert call in m.group(1), call


def test_the_popup_controls_do_not_close_their_own_popup():
    src = _code(_src())
    for box in ("_effortPolicyBox", "_fanoutBox", "_fdBox", "_hmBox", "_wsBox"):
        assert '%s.DropDownOpened += delegate { CloseHeaderPopups("settings"); };' % box in src, box
        assert '%s.DropDownOpened += delegate { CloseHeaderPopups("effort"); };' % box not in src, box


def test_every_paint_of_a_popup_control_is_null_safe():
    # the popup is built on first open, so a repaint before then must not dereference its controls
    src = _code(_src())
    for name, guard in (
        ("PaintEffortPolicy", "if (_effortPolicyBox == null) return;"),
        ("PaintFanout", "if (_fanoutBox == null) return;"),
        ("PaintFanoutDepth", "if (_fdBox == null) return;"),
        ("PaintHierarchicalMerge", "if (_hmBox == null) return;"),
        ("PaintWriteScope", "if (_wsBox == null) return;"),
    ):
        m = re.search(r"void %s\(\).*?\n    }\n" % name, src, re.S)
        assert m and guard in m.group(0), name
    for name, guard in (
        ("PaintEffortPolicyInEffect", "if (_effortPolicyNow == null || _effortPolicyWarn == null) return;"),
        ("PaintFanoutInEffect", "if (_fanoutNow == null || _fanoutPending == null) return;"),
        ("PaintFanoutBudgetInEffect", "if (_fbNow == null || _fbPending == null) return;"),
        ("PaintFanoutDepthInEffect", "if (_fdNow == null || _fdPending == null) return;"),
        ("PaintHierarchicalMergeInEffect", "if (_hmNow == null || _hmPending == null) return;"),
        ("PaintWriteScopeInEffect", "if (_wsNow == null || _wsPending == null) return;"),
    ):
        m = re.search(r"void %s\(.*?\n    }\n" % name, src, re.S)
        assert m and guard in m.group(0), name
