# -*- coding: utf-8 -*-
"""Status labels add a Chinese column and a `recovering` state, and "needs attention" has ONE
definition shared by every screen.

Three things are pinned here, each a hole the inventory found:

1. `recovering` is a real, non-terminal status meaning the harness is auto-recovering a worker
   (canned-nonanswer requeue, reconnect, token renew, INFRA_STUCK retry, interrupted auto-resume).
   It is INFO blue, not warning, and it is NOT a lane a person must touch -- so it never counts as
   "needs attention". It is a STATUS with no OUTCOME, so it must stay OUT of outcomes.STATUS_OF
   (whose key-set is asserted equal to the closed OUTCOMES set at import).

2. Every operator-facing status label has a Chinese (Simplified) wording beside the Japanese and
   English, without disturbing the exact ja/en lines the older tests freeze.

3. "Needs attention" was counted two different ways: the cockpit header excluded INFRA_STUCK and
   local_loop control workers; the chat window's fleet band counted {stuck,maxturns,error,awaiting}
   with no exclusions. Both now go through Theme.CountsAsNeedsAttention, and `awaiting` is a
   separate tally rather than attention.

The C# side is read as source text (WPF, not unit-compilable), in the style of the sibling tests.
"""
import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with io.open(os.path.join(REPO, rel), encoding="utf-8-sig") as fh:
        return fh.read()


THEME = _read("ui/Theme.cs")
HEADER = _read("ui/FleetCockpit.Header.cs")
BAND = _read("ui/CopilotChat.FleetStatus.cs")


# ── 1. recovering as an info, non-attention status ──────────────────────────────────────────

def test_recovering_is_info_not_warning_in_the_status_model():
    assert re.search(r'\{ "recovering",\s*"info"\s*\}', THEME)
    # interrupted stays warning (resume exhausted); recovering is the in-progress state
    assert re.search(r'\{ "interrupted", "warning" \}', THEME)


def test_recovering_has_all_three_language_labels():
    sl = THEME[THEME.index("public static string StatusLabel("):]
    sl = sl[:sl.index("\n    }", sl.index("switch"))]
    assert '自動回復中' in THEME and 'Auto-recovering' in THEME and '自动恢复中' in THEME


def test_recovering_is_never_counted_as_needs_attention():
    fn = THEME[THEME.index("public static bool CountsAsNeedsAttention("):]
    fn = fn[:fn.index("\n    }") + 6]
    assert 'INFRA_STUCK' in fn                     # auto-retry is excluded
    assert '"stuck"' in fn and '"maxturns"' in fn
    # brief rule 5: error is its own danger count, NOT folded into needs-attention
    assert '!= "stuck" && s != "maxturns"' in fn
    # awaiting and recovering are NOT part of the attention definition
    assert '"awaiting"' not in fn


def test_error_is_its_own_count_not_needs_attention():
    # rule 5: error keeps its red label and is counted apart from needs-attention
    assert 'public static bool CountsAsError(string status)' in THEME
    assert 'cntErr' in HEADER
    assert 'errorCount2' in BAND


# ── 2. Chinese column across the operator vocabulary ────────────────────────────────────────

def test_status_label_has_a_chinese_branch_for_the_core_states():
    # The Chinese switch is resolved first (lang == 2) so the ja/en lines stay byte-identical.
    assert 'if (lang == 2)' in THEME
    for zh in ('等待中', '运行中', '完成', '需要处理', '等待批准', '中断', '自动恢复中'):
        assert zh in THEME, zh


def test_timeline_label_also_has_a_chinese_branch():
    tl = THEME[THEME.index("public static string TimelineLabel("):]
    assert 'if (lang == 2)' in tl
    for zh in ('已投入', '运行中', '完成', '停滞', '自动恢复'):
        assert zh in tl, zh


def test_existing_japanese_and_english_lines_are_untouched():
    # the frozen ja/en wording the older suites depend on must survive the Chinese addition
    assert 'case "pending":     return jp ? "待機"                   : "Queued";' in THEME
    assert 'case "error":       return jp ? "停止(エラー)"' in THEME


# ── 3. one definition of "needs attention", shared by both screens ──────────────────────────

def test_both_screens_route_attention_through_the_shared_definition():
    assert 'Theme.CountsAsNeedsAttention(' in HEADER
    assert 'Theme.CountsAsNeedsAttention(' in BAND


def test_the_band_no_longer_folds_awaiting_into_attention():
    # the old inline test counted awaiting as attention; it is now its own gate tally
    i = BAND.index("int doneCount2")
    loop = BAND[i:BAND.index("// Freshness string", i)]
    assert 'CountsAsNeedsAttention' in loop
    assert 'gateCount2' in loop
    # the old welded condition is gone
    assert 'st == "stuck" || st == "maxturns" || st == "error" || st == "awaiting"' not in loop


def test_the_header_keeps_the_local_loop_guard_the_shared_core_cannot_see():
    i = HEADER.index("Theme.CountsAsNeedsAttention(")
    near = HEADER[i:i + 240]
    assert 'IsLocalLoopControlGoal(' in near


# ── Python side: recovering is a status with no outcome ─────────────────────────────────────

def test_recovering_is_a_phase_label_but_not_an_outcome():
    from relay import relay_fleet, outcomes
    assert relay_fleet._PHASE_LABELS["recovering"] == "Auto-recovering"
    assert outcomes.RECOVERING_STATUS == "recovering"
    assert "recovering" not in outcomes.STATUS_OF        # not an outcome; closed set stays intact
    assert "RECOVERING" not in outcomes.OUTCOMES
