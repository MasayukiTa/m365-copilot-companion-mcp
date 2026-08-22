"""The dashboard has to be readable, and it has to lead with what the loop is for.

Three complaints, all about the same screen:

  "この緑とオレンジの丸っぽいやつこれデザインとしてだめ。実行中とかの"
  "これ権限の履歴まったくもって可読性に欠ける"
  "genomeのほうが重要やし"

The first is not a matter of taste. ui/Theme.cs states the design target in as many words --
status is "a thin left rail + small chip (never a full-card fill)" -- and Pill was a saturated
full fill at radius 999, which is pixel-identical to the cockpit's RUNNING indicator. Three of
them over a static integrity check therefore read as three things spinning.

Source-level checks, like ui/test_fleet_cockpit_approval_center.py: the C# is not importable
from pytest, so the wiring is asserted on the file.

Run: pytest -q ui/test_selfimprove_dashboard_reading.py
"""
from pathlib import Path

UI = Path(__file__).parent
SRC = (UI / "SelfImproveDashboard.cs").read_text(encoding="utf-8")


def _fn(name: str, end: str = "\n    }") -> str:
    body = SRC[SRC.index(name):]
    return body[:body.index(end)]


# ── the chip ────────────────────────────────────────────────────────────────────────

def test_the_chip_is_not_a_saturated_fill_with_white_text():
    body = _fn("    Border Pill(string text, string ck)")
    assert "new SolidColorBrush(StatusColorFor(ck, _dark))" not in body, \
        "the background is the raw status colour again -- that is the full fill Theme.cs forbids"
    assert "t.Foreground = White" not in body


def test_the_chip_tints_against_the_card_and_colours_the_text():
    """Named line by line. A looser check ("Mix appears somewhere in here") survived a
    mutation that restored the saturated background and left only the border mixed."""
    body = _fn("    Border Pill(string text, string ck)")
    assert "b.Background      = new SolidColorBrush(Mix(c, CardColor()" in body
    assert "b.BorderBrush     = new SolidColorBrush(Mix(c, CardColor()" in body
    assert "t.Foreground      = new SolidColorBrush(c);" in body


def test_the_chip_is_not_a_lozenge():
    """Radius 999 is what makes it read as a status LED rather than a label."""
    body = _fn("    Border Pill(string text, string ck)")
    assert "CornerRadius(999)" not in body
    assert "CornerRadius(Theme.RadSmall)" in body


def test_a_normal_state_spends_no_colour():
    body = _fn("    Border Pill(string text, string ck)")
    assert 'if (ck == "good" || ck == "warn" || ck == "bad")' in body
    assert "t.Foreground = Muted;" in body


def test_no_bar_asks_for_a_radius_larger_than_its_own_height():
    """An 8px bar with CornerRadius 999 does not clamp per corner -- WPF scales the whole
    geometry and it comes out as a pointed almond. Measured on screen, not reasoned about."""
    for line in SRC.splitlines():
        code = line.split("//")[0]          # the comment says WHY 999 was wrong; it is not code
        if "Height = 8" in code and "CornerRadius" in code:
            assert "999" not in code, line


# ── the authority header ────────────────────────────────────────────────────────────

def test_a_passing_check_produces_a_line_not_a_badge():
    body = _fn("    UIElement BuildAuthority()", "\n    void RevokeLastRebless")
    assert "okBits.Add(T(\"auth_intact\")" in body
    assert "okBits.Add(T(\"auth_anchor\")" in body
    assert "okBits.Add(T(\"auth_chain\")" in body


def test_a_failing_check_is_the_only_thing_that_gets_a_chip():
    body = _fn("    UIElement BuildAuthority()", "\n    void RevokeLastRebless")
    for key in ('T("auth_intact")', 'T("auth_anchor")', 'T("auth_chain")'):
        seg = body[body.index("var okBits"):]
        assert ("else head.Children.Add(Pill(" + key) in seg or \
               ("else head.Children.Add(Pill(" + key.replace('T("', 'T("')) in seg, key


def test_collapsing_withholds_detail_but_never_the_verdict():
    """Every count and every verdict stays on screen when the pane is shut; only records,
    the disclosure and the undo button move inside it."""
    body = _fn("    UIElement BuildAuthority()", "\n    void RevokeLastRebless")
    assert "bool allWell = intact && anchorOk && linksOk && !hot;" in body
    assert "detail.Visibility = allWell ? Visibility.Collapsed : Visibility.Visible;" in body
    assert "col.Children.Add(okLine);" in body          # the verdict is outside the pane
    assert "detail.Children.Add(rec);" in body          # the records are inside it
    assert "detail.Children.Add(btn);" in body


def test_a_failed_check_opens_the_pane_by_itself():
    body = _fn("    UIElement BuildAuthority()", "\n    void RevokeLastRebless")
    i = body.index("bool allWell =")
    assert "Visibility.Visible" in body[i:i + 400]


# ── the records ─────────────────────────────────────────────────────────────────────

def test_a_record_shows_when_it_happened():
    """The ledger has carried a ts on every record from the beginning and the history
    displayed none of it. A list of acts with no times is not a history."""
    body = _fn("    UIElement BuildAuthority()", "\n    void RevokeLastRebless")
    assert 'rows[i].TryGetValue("ts", out ts);' in body
    assert "AgoText(Convert.ToDouble(ts), now)" in body
    assert "AgoText" in SRC[SRC.index("    string AgoText"):]


def test_the_four_parts_of_a_record_are_no_longer_one_concatenated_string():
    body = _fn("    UIElement BuildAuthority()", "\n    void RevokeLastRebless")
    assert 'line.Text = kind + "  " + Convert.ToString(actor) + scope;' not in body
    assert "kindTb.FontFamily = new FontFamily(Theme.CodeFont);" in body


def test_the_reason_is_body_text_not_a_footnote():
    """It was muted underneath the run-on line -- the record's actual content rendered as
    the faintest thing in it."""
    body = _fn("    UIElement BuildAuthority()", "\n    void RevokeLastRebless")
    assert "var reasonTb = ClipLine(Convert.ToString(reason), Fg," in body


def test_long_lines_clip_rather_than_wrap_and_keep_their_full_text():
    body = _fn("    TextBlock ClipLine(string text")
    assert "TextTrimming.CharacterEllipsis" in body
    assert "t.ToolTip = text" in body


def test_a_record_can_be_opened_in_place():
    body = _fn("    UIElement BuildAuthority()", "\n    void RevokeLastRebless")
    assert "rec.MouseLeftButtonUp" in body
    assert "SetClip(rTb, open); SetClip(sTb, open); SetClip(aTb, open);" in body


def test_the_rail_encodes_the_kind_of_act():
    body = _fn("    Brush RailForEvent(string kind)")
    assert '"baseline_mismatch"' in body and '"revoke"' in body
    assert '"rebless"' in body


# ── what leads ──────────────────────────────────────────────────────────────────────

def test_the_genomes_lead_and_the_audit_log_trails():
    body = _fn("    void Render(Dictionary<string, object> state)")
    order = [ln.strip() for ln in body.splitlines() if "_body.Children.Add(Build" in ln]
    assert order[0].startswith("_body.Children.Add(BuildArchive")
    assert order[-1].startswith("_body.Children.Add(BuildAuthority")


def test_the_genomes_themselves_are_shown_not_just_counted():
    """id, parent, pass@1, gate verdict and descriptors were all in the feed already; the
    card rendered two integers derived from that list and discarded the list."""
    body = _fn("    UIElement BuildArchive(Dictionary<string, object> state)")
    assert 'Arr(arc, "genomes")' in body
    for field in ('"id"', '"gate_verdict"', '"parent_id"', '"pass_at_1"', '"descriptors"'):
        assert field in body, field


def test_the_lead_card_does_not_restate_a_number_the_next_card_carries():
    """latest pass@1 was on this card, on the scorecard directly below it, and on every row
    of the table underneath. Three statements of one number is not emphasis."""
    body = _fn("    UIElement BuildArchive(Dictionary<string, object> state)")
    assert '"latest_pass_at_1"' not in body
    assert 'MetricCell(T("g_best")' in body


def test_best_pass_is_computed_from_the_genomes_and_survives_a_missing_field():
    body = _fn("    string BestPass(Dictionary<string, object> arc)")
    assert 'return "?"' in body
    assert "catch (Exception) { }" in body
