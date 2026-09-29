# -*- coding: utf-8 -*-
"""Expanded execution-history timeline keeps semantic colours and localized vocabulary."""
from pathlib import Path

THEME = Path(__file__).with_name('Theme.cs').read_text(encoding='utf-8')
COCKPIT = Path(__file__).with_name('FleetCockpit.cs').read_text(encoding='utf-8-sig')


def test_timeline_has_four_operator_meaning_colours():
    # Timeline colours are intentionally narrower than general status colours:
    # neutral=secondary graphite, work=blue, attention=orange, completed=green.
    assert 'public static string TimelineColor(string canonical, bool dark)' in THEME
    block = THEME[THEME.index('public static string TimelineColor('):]
    block = block[:block.index('\n    }', 20) + 6]
    assert 'return Secondary(dark);' in block
    assert 'return Text(dark);' not in block
    assert 'return Info(dark);' in block
    assert 'return Warning(dark);' in block
    assert 'return Success(dark);' in block
    assert 'Danger(dark)' not in block


def test_protocol_timeline_events_have_japanese_and_english_labels():
    # These are measured event names in .fleet/history.json, not hypothetical vocabulary.
    pairs = {
        'pending': ('待機', 'Queued'),
        'ready': ('開始', 'Starting'),
        'waiting': ('実行中', 'Running'),
        'researching': ('調査中', 'Researching'),
        'refuting': ('レビュー中', 'Reviewing'),
        'verifying': ('検証中', 'Verifying'),
        'waiting_runtime': ('実行環境待ち', 'Runtime paused'),
        'awaiting_gate': ('承認待ち', 'Needs approval'),
        'done': ('完了', 'Done'),
        'stuck': ('要対応', 'Needs attention'),
        'error': ('停止(エラー)', 'Stopped (error)'),
        'cancelled': ('停止', 'Stopped'),
        'job_created': ('ジョブ作成', 'Job created'),
        'ui_trigger_attempt': ('UI起動試行', 'UI trigger attempt'),
        'ui_trigger_sent': ('UI起動送信', 'UI trigger sent'),
        'protocol_bootstrap_sent': ('プロトコル開始指示送信', 'Protocol bootstrap sent'),
        'turn_finished_without_commit': ('コミットなしでターン終了', 'Turn finished without commit'),
        'turn_controller_retry': ('コントローラ再試行', 'Controller retry'),
        'conversation_rotated': ('会話を切替', 'Conversation rotated'),
        'job_cancelled': ('ジョブ停止', 'Job cancelled'),
    }
    for key, (ja, en) in pairs.items():
        assert f'case "{key}"' in THEME, key
        assert ja in THEME, (key, ja)
        assert en in THEME, (key, en)


def test_timeline_status_lookup_normalizes_protocol_event_case():
    assert 'canonical.Trim().ToLowerInvariant()' in THEME


def test_left_spine_is_not_a_second_timeline():
    i = COCKPIT.index('UIElement BuildSpineContent(')
    b = COCKPIT[i:COCKPIT.index('List<Tuple<string, string>> BuildTimelineEvents', i)]
    assert 'sectionLbl.Text = ja ? "内容詳細" : "Content details";' in b
    assert 'Theme.TimelineLabel(peEvent, _lang)' not in b
    assert 'Theme.TimelineColor(peEvent, _dark)' not in b
    assert 'Execution timeline' not in b


def test_expanded_timeline_no_longer_forces_every_event_to_muted_gray():
    i = COCKPIT.index('// ── Timeline section') if '// ── Timeline section' in COCKPIT else COCKPIT.index('var tsEvents = BuildTimelineEvents')
    b = COCKPIT[i:i + 2200]
    assert 'Foreground = Theme.Br(ev.Item2)' in b
    assert 'Foreground = Muted' not in b[:1200]
    assert 'List<Tuple<string, string>> BuildTimelineEvents' in COCKPIT
    assert 'Theme.TimelineColor(peEvent, _dark)' in COCKPIT[COCKPIT.index('List<Tuple<string, string>> BuildTimelineEvents'):]


def test_timeline_section_heading_localizes_in_both_views():
    assert 'SectLabel(_lang == 0 ? "タイムライン" : "Timeline")' in COCKPIT


def test_neutral_timeline_uses_graphite_not_body_black():
    # Light theme values are the visible regression from the supplied screenshot.
    assert 'public static string Text(bool d)          { return d ? "#F4F4F5" : "#18181B"; }' in THEME
    assert 'public static string Secondary(bool d)    { return d ? "#A1A1AA" : "#3F3F46"; }' in THEME
    block = THEME[THEME.index('public static string TimelineColor('):]
    block = block[:block.index('\n    }', 20) + 6]
    assert 'return Secondary(dark);' in block

def test_color_restore_does_not_rewrite_historical_event_wording():
    # Timeline evidence now lives only in the expanded card. Keep its historical fallback wording
    # stable while allowing the left task-inspection surface to evolve independently.
    i = COCKPIT.index('List<Tuple<string, string>> BuildTimelineEvents')
    timeline = COCKPIT[i:COCKPIT.index('double ReadTranscriptStartTs', i)]
    assert 'queuedTs + (ja ? "投入" : "Queued")' in timeline
    assert 'startTs + (ja ? "開始" : "Started")' in timeline
    assert 'ja ? ("レビュー (" + reviews + "x)")' in timeline
    assert 'outcomeEv = ja ? "ターン上限" : "Max turns reached"' in timeline
    assert 'outcomeEv = ja ? "停滞" : "Stuck"' in timeline
    assert 'Theme.TimelineColor("pending", _dark)' in timeline
    assert 'Theme.TimelineColor("ready", _dark)' in timeline


def test_legacy_status_copy_survives_new_protocol_vocabulary():
    # New event keys may be added, but existing operator vocabulary must not drift as collateral.
    assert 'case "pending":     return jp ? "待機"' in THEME
    assert 'case "awaiting":    return jp ? "承認待ち"' in THEME
    assert 'case "error":       return jp ? "停止(エラー)"' in THEME
    assert 'case "freed":       return jp ? "解放済"' in THEME


def test_real_mode_uses_event_history_vocabulary_not_status_chip_copy():
    assert 'public static string TimelineLabel(string canonical, int lang)' in THEME
    expected = {
        'pending': ('投入', 'Queued'),
        'ready': ('開始', 'Started'),
        'waiting': ('実行中', 'Running'),
        'done': ('完了', 'Completed'),
        'stuck': ('停滞', 'Stuck'),
        'maxturns': ('ターン上限', 'Max turns reached'),
        'error': ('エラー', 'Error'),
        'cancelled': ('停止', 'Cancelled'),
    }
    t = THEME[THEME.index('public static string TimelineLabel('):]
    t = t[:t.index('\n    }', 20) + 6]
    for key, (ja, en) in expected.items():
        assert f'case "{key}"' in t
        assert ja in t and en in t

    # The single REAL phase-event renderer is the expanded-card Timeline. The left spine is
    # content details now; status-chip vocabulary must still not leak into event history.
    assert COCKPIT.count('Theme.TimelineLabel(peEvent, _lang)') == 1
    timeline = COCKPIT[COCKPIT.index('List<Tuple<string, string>> BuildTimelineEvents'):]
    assert 'Theme.StatusLabel(peEvent, _lang)' not in timeline
