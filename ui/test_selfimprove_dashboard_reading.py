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
    # Fg when it is the only text on the line; Muted once a summary sits above it, because
    # the recorded words then play the supporting part rather than the leading one.
    assert "var reasonTb = ClipLine(Convert.ToString(reason), sum.Length > 0 ? Muted : Fg," in body


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
    assert "if (rows == null || rows.Count == 0) return null;" in body
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
    assert "ClipLine(Convert.ToString(reason)," in body
    assert "reasonTb.Visibility = Visibility.Collapsed;" in body   # collapsed, never dropped


def test_the_quote_form_stays_reserved_for_verbatim_material():
    body = _authority()
    seg = body[body.index('if (auth != null'):]
    assert "Convert.ToString(auth)" in seg
    assert "qt.Text" in seg and "u201c" in seg.replace(chr(92), "")  # the quote glyphs, however escaped


# ── the derived summary, and the boundary around it ─────────────────────────────────

def test_the_summary_follows_the_interface_language():
    """The complaint that started this: toggling to English left those lines in Japanese,
    because a record's reason is not interface text."""
    body = _fn("    string SummaryFor(Dictionary<string, object> row, Dictionary<string, object> cache)")
    assert 'entry.TryGetValue(_lang == 0 ? "ja" : "en", out v);' in body


def test_a_summary_is_looked_up_by_the_records_own_hash():
    """Any other key would let a summary attach to a different record."""
    body = _fn("    string SummaryFor(Dictionary<string, object> row, Dictionary<string, object> cache)")
    assert 'row.TryGetValue("hash", out h);' in body
    assert 'if (key.Length == 0) return "";' in body


def test_a_missing_cache_falls_back_to_the_recorded_words():
    body = _fn("    Dictionary<string, object> LoadSummaries()")
    assert "if (!File.Exists(p)) return null;" in body
    assert "catch (Exception) { return null; }" in body
    rec = _authority()
    assert 'string sum = SummaryFor(rows[i], summaries);' in rec
    assert 'sum.Length > 0 ? Muted : Fg' in rec


def test_the_recorded_words_are_collapsed_and_never_removed():
    """A summary is only trustworthy if the original is one click away rather than gone."""
    rec = _authority()
    assert "reasonTb.Visibility = Visibility.Collapsed;" in rec
    assert "rTb.Visibility = Visibility.Visible;" in rec


def test_a_summary_says_that_it_is_one():
    rec = _authority()
    assert 'tag.Text = T("ev_summary");' in rec
    assert 'sumRow.ToolTip = T("ev_summary_tip");' in rec


def test_the_tip_says_it_is_not_the_record():
    src = SRC[SRC.index('if (k == "ev_summary_tip")'):]
    src = src[:src.index("if (k == \"ev_by\")")] if 'if (k == "ev_by")' in src else src[:400]
    assert "台帳の記録ではない" in src
    assert "not the ledger's record" in src


def test_the_summary_is_not_the_loudest_thing_on_the_row():
    """The deterministic headline is the bold one. Making the least reliable element the most
    prominent inverts the visual grammar."""
    rec = _authority()
    i_head = rec.index("titleTb.FontWeight = FontWeights.SemiBold;")
    seg = rec[rec.index("var sumTb = new TextBlock();"):]
    seg = seg[:seg.index("sumRow.Children.Add(sumTb);")]
    assert "FontWeight" not in seg
    assert i_head < rec.index("var sumTb = new TextBlock();")


def test_the_rate_says_which_files_are_absorbing_it():
    """A count says the ledger is growing; it does not say what to do. Repeated re-signings of
    one file are the actual signal -- either the workflow keeps touching something that should
    not be frozen, or one change is being approved in pieces."""
    body = _authority()
    assert "var churn = new Dictionary<string, int>();" in body
    assert 'T("ev_churn")' in body
    assert 'Convert.ToString(e1) != "rebless"' in body


def test_the_churn_window_matches_the_rate_window():
    """Two windows over the same rows would disagree in public."""
    body = _authority()
    assert "if (age <= 7 * 86400) last7++" in body
    assert "if (now - Convert.ToDouble(t1) > 7 * 86400) continue;" in body


# ── a card you can decide from ──────────────────────────────────────────────────────

def _pending():
    return _fn("    UIElement BuildPending(Dictionary<string, object> state)")


def test_a_pending_card_offers_somewhere_to_decide():
    """"Copy command" alone states that a decision is waiting without offering anywhere to
    make it -- the same shape as the notification that opened a text file of commands."""
    body = _pending()
    assert 'yes.Content = T("pd_approve");' in body
    assert 'no.Content = T("pd_reject");' in body


def test_a_card_carries_the_argument_not_just_a_headline():
    body = _pending()
    assert 'string detail = S(r, "detail");' in body


def test_an_approval_is_a_phrase_the_operator_picked_or_wrote():
    """A button recording "approved from the dashboard" would be this window putting words in
    their mouth. A phrase they PICKED is theirs -- it is shown verbatim before the click --
    and demanding typed prose for every routine yes is friction that gets a decision surface
    abandoned."""
    body = _pending()
    assert 'AskForDecision(T("pd_ask_t")' in body
    assert 'T("pd_a1"), T("pd_a2")' in body
    assert 'RecordDecision(theId, "--approve", said, kind)' in body


def test_the_record_says_whether_it_was_picked_or_written():
    """A preset says "yes"; typed words can say "yes, but". A reader should not have to guess
    which they are looking at."""
    dlg = _fn("    string AskForDecision(string title, string[] choices, out string kind)")
    assert 'picked[1] = "preset"' in dlg
    assert 'picked[1] = "typed"' in dlg
    py = (UI.parent / "relay" / "selfimprove" / "pending.py").read_text(encoding="utf-8")
    assert '"authorization_kind"' in py


def test_the_phrase_is_shown_before_it_is_recorded():
    """Nothing may be recorded that the operator did not see, word for word."""
    dlg = _fn("    string AskForDecision(string title, string[] choices, out string kind)")
    assert 'note.Text = T("pd_recorded");' in dlg
    assert "b.Content = choice;" in dlg


def test_typing_stays_available_and_cannot_be_empty():
    dlg = _fn("    string AskForDecision(string title, string[] choices, out string kind)")
    assert "own.Text.Trim().Length == 0" in dlg
    py = (UI.parent / "relay" / "selfimprove" / "pending.py").read_text(encoding="utf-8")
    assert "REFUSED: --approve needs --authorization" in py


def test_cancelling_records_nothing():
    body = _pending()
    assert "if (said == null) return;" in body
    dlg = _fn("    string AskForDecision(string title, string[] choices, out string kind)")
    assert "cancel.Click += delegate { picked[0] = null; w.Close(); };" in dlg


def test_an_approved_item_stays_on_screen_with_the_words_that_approved_it():
    """Removing it at the moment of approval is what made approving feel identical to being
    ignored: the entry vanishes, which is what a lost decision also looks like."""
    body = _pending()
    assert 'if (status == "approved")' in body
    assert "qt.Text" in body and "+ words +" in body
    assert 'T("pd_approved")' in body
    py = (UI.parent / "relay" / "selfimprove" / "pending.py").read_text(encoding="utf-8")
    assert "LIVE = (OPEN, APPROVED)" in py


def test_an_approved_command_no_longer_carries_the_placeholder():
    """It can be run as it stands once the words exist."""
    body = _pending()
    assert "cmd.Replace(" in body and "u8a00" in body.replace(chr(92), "")
    assert '.Replace("<your words>", words)' in body


def test_a_failed_recording_is_shown_rather_than_swallowed():
    """A decision that failed to record silently is worse than no button: the operator
    believes they answered."""
    body = _fn("    bool RecordDecision(string pid, string verb, string words, string kind)")
    assert "proc.ExitCode != 0" in body
    assert 'T("pd_failed")' in body


def test_the_decision_goes_through_the_module_that_owns_the_format():
    body = _fn("    bool RecordDecision(string pid, string verb, string words, string kind)")
    assert '"-m relay.selfimprove.pending "' in body


# ── the queue itself, not a snapshot of it ──────────────────────────────────────────

def test_the_pending_cards_read_the_queue_and_not_a_regenerated_file():
    """Approving worked -- the queue recorded the chosen phrase correctly -- and the card did
    not move, because the window re-rendered .fleet/selfimprove_dashboard.json, which only a
    separate `dashboard --write` refreshes. A real approval with a screen saying otherwise is
    worse than a button that does nothing: there is nothing to retry and nothing looks wrong."""
    body = _pending()
    assert "var rows = ReadPendingQueue(true);" in body   # unanswered only
    assert 'Arr(state, "pending_decisions")' not in body
    rq = _fn("    List<Dictionary<string, object>> ReadPendingQueue(bool wantOpen)")
    assert '"pending_decisions.jsonl"' in rq


def test_the_queue_reader_replays_resolutions_over_the_queued_rows():
    rq = _fn("    List<Dictionary<string, object>> ReadPendingQueue(bool wantOpen)")
    assert 'if (ev == "queued")' in rq
    assert 'else if (ev == "resolved" && byId.ContainsKey(id))' in rq
    assert 'byId[id]["authorization"] = S(row, "authorization");' in rq


def test_a_torn_line_in_the_queue_does_not_hide_the_rest():
    rq = _fn("    List<Dictionary<string, object>> ReadPendingQueue(bool wantOpen)")
    assert "catch (Exception) { continue; }" in rq


def test_an_answered_proposal_leaves_the_awaiting_section_entirely():
    """It stayed there so that approving did not look like being ignored, which put decided
    items under a heading that says they are still waiting -- the heading and its contents
    contradicting each other."""
    rq = _fn("    List<Dictionary<string, object>> ReadPendingQueue(bool wantOpen)")
    assert 'if (wantOpen == (st == "open")) open_.Add(byId[id]);' in rq
    hist = _fn("    UIElement BuildDecisions()")
    assert "ReadPendingQueue(false)" in hist


def test_the_history_reads_newest_first():
    rq = _fn("    List<Dictionary<string, object>> ReadPendingQueue(bool wantOpen)")
    assert "if (!wantOpen) open_.Reverse();" in rq


def test_an_approved_proposal_stays_visible_until_it_is_carried_out():
    """An approval that vanishes the moment it is given is indistinguishable from one that
    was lost."""
    hist = _fn("    UIElement BuildDecisions()")
    assert 'status == "approved" ? mark + " \\u00b7 " + T("pd_waiting") : mark' in hist
    py = (UI.parent / "relay" / "selfimprove" / "pending.py").read_text(encoding="utf-8")
    assert "LIVE = (OPEN, APPROVED)" in py


def test_a_rejected_proposal_keeps_its_reason():
    """So the next time the same thing comes up, the earlier decision can be read."""
    hist = _fn("    UIElement BuildDecisions()")
    assert 'T("dh_dropped")' in hist
    assert "words.Length > 0" in hist


def test_the_history_is_absent_when_nothing_has_been_decided():
    hist = _fn("    UIElement BuildDecisions()")
    assert "if (rows == null || rows.Count == 0) return null;" in hist
    order = _fn("    void Render(Dictionary<string, object> state)")
    assert "if (decided != null) _body.Children.Add(decided);" in order


def test_the_count_chip_counts_only_what_is_unanswered():
    """The section holds only unanswered items now, so its own count is the count."""
    body = _pending()
    assert 'Pill(rows.Count.ToString()' in body


def test_an_answered_card_offers_no_second_approval():
    body = _pending()
    assert 'if (status != "approved")' in body
