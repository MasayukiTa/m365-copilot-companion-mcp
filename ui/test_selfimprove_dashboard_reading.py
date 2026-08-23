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


# BuildAuthority is long enough that "the next closing brace" lands inside it, so its end is
# named by the method that follows.
END_OF_AUTHORITY = "\n    void RevokeLastRebless"


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
    assert "titleTb.FontWeight = FontWeights.SemiBold;" in body   # headline
    assert "var reasonTb = ClipLine(" in body                     # why
    assert "scopeTb = ClipLine(tail," in body                     # where, and by whom


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
    assert "SetClip(rTb, open); SetClip(sTb, open); SetClip(tTb, open);" in body


def test_no_content_block_wears_a_coloured_left_rail():
    """Standing instruction, stated more than once before this: a thick coloured bar down
    the left edge of a block reads as a sticky note. It was reintroduced here on the strength
    of a line in Theme.cs recommending exactly that, so the recommendation is pinned too --
    a stale one in the single source of truth gets followed again by the next reader."""
    import re
    theme = (UI / "Theme.cs").read_text(encoding="utf-8")
    assert "NO COLOURED LEFT RAILS ON CONTENT BLOCKS" in theme
    assert "status shown as a thin left rail" not in theme

    for m in re.finditer(r"BorderThickness\s*=\s*new Thickness\(\s*([0-9.]+)\s*,\s*0\s*,\s*0\s*,\s*0\s*\)", SRC):
        near = SRC[m.start():m.start() + 260]
        assert "StatusColorFor" not in near,             "a left-only border is being painted a status colour: " + near.splitlines()[0]


def test_the_severity_is_carried_by_the_event_name():
    body = _fn("    Brush KindBrush(string kind)")
    assert '"baseline_mismatch"' in body and '"revoke"' in body
    assert "return Fg;" in body, "a routine re-signing must stay neutral"
    body2 = _fn("    UIElement BuildAuthority()", END_OF_AUTHORITY)
    # The headline carries it now, and only for the state that is actually abnormal.
    assert 'unresolved ? KindBrush("baseline_mismatch") : Fg' in body2


def test_records_are_separated_by_a_rule_not_by_a_strip():
    body = _fn("    UIElement BuildAuthority()", END_OF_AUTHORITY)
    assert "rec.BorderThickness = new Thickness(0, 0, 0, 1);" in body


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


# ── awaiting a decision ─────────────────────────────────────────────────────────────

def test_the_pending_section_is_absent_when_nothing_is_pending():
    """An empty call to action is noise, and it would sit above everything else."""
    body = _fn("    UIElement BuildPending(Dictionary<string, object> state)")
    assert "if (rows == null || rows.Length == 0) return null;" in body
    order = _fn("    void Render(Dictionary<string, object> state)")
    assert "if (pending != null) _body.Children.Add(pending);" in order


def test_pending_decisions_lead_when_there_are_any():
    order = _fn("    void Render(Dictionary<string, object> state)")
    i_pending = order.index("BuildPending(state)")
    i_archive = order.index("_body.Children.Add(BuildArchive")
    assert i_pending < i_archive


def test_an_entry_carries_the_command_and_a_way_to_take_it():
    """The command is the point of the entry; a command you cannot copy is a picture of one."""
    body = _fn("    UIElement BuildPending(Dictionary<string, object> state)")
    assert 'S(r, "command")' in body
    assert "Clipboard.SetText(theCmd)" in body


def test_the_pending_entries_wear_no_rail_either():
    body = _fn("    UIElement BuildPending(Dictionary<string, object> state)")
    assert "rec.BorderThickness = new Thickness(0, 0, 0, 1);" in body
    assert "RailW" not in body


def test_the_section_does_not_claim_to_enforce_anything():
    """It runs in the same privilege domain as the agent that fills it. Saying otherwise on
    the screen would be the most consequential kind of wrong."""
    src = SRC[SRC.index('if (k == "pending_exp")'):]
    src = src[:src.index("if (k == \"pending_copy\")")]
    for word in ("強制", "enforce", "blocks", "prevents"):
        assert word not in src, word


# ── the ledger reads as a list of events, not as a list of my own input ─────────────

def _authority():
    return _fn("    UIElement BuildAuthority()", END_OF_AUTHORITY)


def test_the_headline_is_computed_from_the_record_not_typed_into_it():
    """The first line used to be the actor -- a module path taking two values across the whole
    ledger -- and the line that actually read as the title was the raw --reason the agent had
    typed. A record whose headline is its own input text cannot be scanned."""
    body = _authority()
    assert "string title = EventVerb(kind);" in body
    assert "string targets = BaseNames(paths);" in body


def test_the_actor_is_no_longer_the_headline():
    body = _authority()
    i_title = body.index("titleTb.FontWeight = FontWeights.SemiBold;")
    i_actor = body.index('string who = Convert.ToString(actor);')
    assert i_title < i_actor, "the actor must sit below the headline, not be it"


def test_the_verb_comes_from_a_lookup_and_not_from_a_model():
    """Every part of a headline is computable from the record. Substituting a language model
    for a lookup table is the design fault this system has committed before."""
    body = _fn("    string EventVerb(string kind)")
    for kind in ("rebless", "baseline_mismatch", "rebless_revoke",
                 "genome_apply", "genome_revert"):
        assert '"' + kind + '"' in body, kind


def test_the_pairing_runs_forwards_in_time():
    """Chronologically the drift is detected first and the operator re-signs after. Read off
    the screen -- newest first -- the pair looks reversed, and collapsing "the mismatch after a
    rebless" hides the ones nothing has answered yet."""
    body = _authority()
    seg = body[body.index("var closedBy = new Dictionary<int, int>();"):]
    seg = seg[:seg.index("int testRecords")]
    assert 'rows[i].TryGetValue("event", out a);' in seg
    assert 'rows[i + 1].TryGetValue("event", out b);' in seg
    assert 'Convert.ToString(a) != "baseline_mismatch"' in seg
    assert 'Convert.ToString(b) != "rebless"' in seg


def test_a_pair_is_only_collapsed_when_it_names_the_same_files():
    """A mismatch that detected something other than what was then approved is the anomaly a
    reader should see first."""
    body = _authority()
    assert "if (!SameTargets(rows[i], rows[i + 1])) continue;" in body


def test_the_unpinned_marker_does_not_look_like_a_different_file():
    """It is a state marker on the same path. Compared raw, 2 of 21 pairs looked like
    detected-is-not-approved, which is a real anomaly and must stay distinguishable."""
    body = _fn("    static string StripPin(string path)")
    assert '"UNPINNED:"' in body


def test_a_broken_chain_stops_the_collapsing_entirely():
    """No formatting on top of a ledger that failed its own integrity check."""
    body = _authority()
    assert "if (linksOk)\n        {" in body
    assert "if (linksOk && IsTestRecord(rows[i])) continue;" in body


def test_a_broken_chain_does_not_call_every_mismatch_unresolved():
    """No pairing is computed there, so the unresolved label would fire on all of them -- a
    false alarm produced by the condition that already warns the reader."""
    body = _authority()
    assert 'bool unresolved = linksOk && kind == "baseline_mismatch";' in body


def test_an_unresolved_mismatch_is_never_collapsed_and_says_so():
    body = _authority()
    assert 'T("ev_unresolved")' in body
    assert 'KindBrush("baseline_mismatch")' in body


def test_test_written_records_are_counted_not_deleted():
    """They are real rows in an append-only file. What they are not is events in this
    repository's life, and 20 of 78 crowded out the ones that were."""
    body = _authority()
    assert 'string.Format(T("ev_testrecords"), testRecords)' in body
    ident = _fn("    static bool IsTestRecord(Dictionary<string, object> row)")
    assert '"Temp"' in ident and '"tmp"' in ident


def test_the_count_is_exact_rather_than_approximate():
    body = _authority()
    assert "if (linksOk) foreach (var r0 in rows) if (IsTestRecord(r0)) testRecords++;" in body


def test_the_reason_shown_is_still_the_recorded_one():
    """Nothing on this screen is generated yet, and when something is, it must not replace
    the record."""
    body = _authority()
    assert "ClipLine(Convert.ToString(reason), Fg, Theme.FsMeta, false)" in body


def test_the_quote_form_stays_reserved_for_verbatim_material():
    body = _authority()
    seg = body[body.index('if (auth != null'):]
    assert "Convert.ToString(auth)" in seg
    assert "qt.Text" in seg and "u201c" in seg.replace(chr(92), "")  # the quote glyphs, however escaped
