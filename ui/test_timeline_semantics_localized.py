# -*- coding: utf-8 -*-
"""Execution timeline keeps its historical semantic colours and follows the UI language."""
from pathlib import Path

THEME = Path(__file__).with_name('Theme.cs').read_text(encoding='utf-8')
COCKPIT = Path(__file__).with_name('FleetCockpit.cs').read_text(encoding='utf-8-sig')


def test_timeline_has_four_operator_meaning_colours():
    # Timeline colours are intentionally narrower than general status colours:
    # neutral=ordinary text, work=blue, attention=orange, completed=green.
    assert 'public static string TimelineColor(string canonical, bool dark)' in THEME
    block = THEME[THEME.index('public static string TimelineColor('):]
    block = block[:block.index('\n    }', 20) + 6]
    assert 'return Text(dark);' in block
    assert 'return Info(dark);' in block
    assert 'return Warning(dark);' in block
    assert 'return Success(dark);' in block
    assert 'Danger(dark)' not in block


def test_protocol_timeline_events_have_japanese_and_english_labels():
    # These are measured event names in .fleet/history.json, not hypothetical vocabulary.
    pairs = {
        'pending': ('キュー待ち', 'Queued'),
        'ready': ('開始', 'Starting'),
        'waiting': ('実行中', 'Running'),
        'researching': ('調査中', 'Researching'),
        'refuting': ('レビュー中', 'Reviewing'),
        'verifying': ('検証中', 'Verifying'),
        'waiting_runtime': ('実行環境待ち', 'Runtime paused'),
        'awaiting_gate': ('承認待ち', 'Needs approval'),
        'done': ('完了', 'Done'),
        'stuck': ('要対応', 'Needs attention'),
        'error': ('エラー停止', 'Stopped (error)'),
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


def test_spine_uses_timeline_colour_and_localized_labels():
    i = COCKPIT.index('UIElement BuildSpineContent(')
    b = COCKPIT[i:COCKPIT.index('\n    //', i + 14000)]
    assert 'Theme.StatusLabel(peEvent, _lang)' in b
    assert 'Theme.TimelineColor(peEvent, _dark)' in b
    assert 'sectionLbl.Text = ja ? "実行タイムライン" : "Execution timeline";' in b
    assert 'ja ? "(フェーズ遷移)" : "(phase transitions)"' in b
    assert 'ja ? "(ターン記録から推定)" : "(estimated from turns)"' in b


def test_expanded_timeline_no_longer_forces_every_event_to_muted_gray():
    i = COCKPIT.index('// ── Timeline section') if '// ── Timeline section' in COCKPIT else COCKPIT.index('var tsEvents = BuildTimelineEvents')
    b = COCKPIT[i:i + 2200]
    assert 'Foreground = Theme.Br(ev.Item2)' in b
    assert 'Foreground = Muted' not in b[:1200]
    assert 'List<Tuple<string, string>> BuildTimelineEvents' in COCKPIT
    assert 'Theme.TimelineColor(peEvent, _dark)' in COCKPIT[COCKPIT.index('List<Tuple<string, string>> BuildTimelineEvents'):]


def test_timeline_section_heading_localizes_in_both_views():
    assert 'SectLabel(_lang == 0 ? "タイムライン" : "Timeline")' in COCKPIT
