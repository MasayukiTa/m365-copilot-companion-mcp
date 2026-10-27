# -*- coding: utf-8 -*-
"""A COMPLETED card shows how long the task RAN -- duration format + source-level checks.

## The requirement

「完了したタスクのカードで、ターン数の右に、そのタスクが走っていた時間（開始から
完了までの所要時間）を、「所要 23分」「所要 1時間12分」のように人が読みやすい表現で出す。
開始か完了の時刻が記録に無いカードは、推測せず所要時間を出さない（空欄）。」

## What this asserts

The WPF render needs a live GUI, so (as with the other cockpit source tests -- see
`test_a_completed_card_shows_when_not_which_worker.py`) the source-level decisions are pinned
here, and the duration FORMATTER is reimplemented in Python and checked at its boundaries.

1. `DurationHuman(sec, ja)` in FleetCockpit.Header.cs: < 60s -> seconds; < 1h -> whole minutes;
   >= 1h -> hours (+ minutes only when non-zero); a day part past 24h; sec < 0 -> "" (no guess).
2. The collapsed meta line appends the run duration to the RIGHT of the turn count, only on a
   terminal card, and only when BOTH the start ts and the completion ts are known.
3. The PR #300 invariant is not disturbed: `name.ToUpper()` still sits on the live (else)
   branch, so a completed card still hides its worker number.

`FleetCockpit.*` parts are read by name from THIS working tree (utf-8-sig), matching the sibling
cockpit source test, because tools.cockpit_source resolves against the installed package.
"""
import os

UI = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8-sig") as fh:
        return fh.read()


# ── the Python reference for DurationHuman, kept byte-for-byte equivalent to the C# ─────────

def duration_human(sec, ja):
    """Mirror of FleetCockpit.Header.cs DurationHuman. sec is seconds (may be fractional);
    sec < 0 means a start or completion ts was missing, and yields ""."""
    if sec < 0:
        return ""
    s = int(sec)  # truncation toward zero, like C# (long)sec for sec >= 0
    dd = s // 86400
    s -= dd * 86400
    hh = s // 3600
    s -= hh * 3600
    mm = s // 60
    s -= mm * 60
    out = []
    if dd > 0:
        out.append("%d%s" % (dd, "\u65e5" if ja else "d"))
    if dd > 0 or hh > 0:
        out.append("%d%s" % (hh, "\u6642\u9593" if ja else "h"))
    if dd > 0 or hh > 0:
        if mm > 0:
            out.append("%d%s" % (mm, "\u5206" if ja else "m"))
    elif mm > 0:
        out.append("%d%s" % (mm, "\u5206" if ja else "m"))
    else:
        out.append("%d%s" % (s, "\u79d2" if ja else "s"))
    return "".join(out)


# ── boundary cases: 59s, 60s, 59m, 1h, and the no-ts case ─────────────────────────────

def test_59_seconds_is_still_seconds():
    assert duration_human(59, ja=True) == "59\u79d2"
    assert duration_human(59, ja=False) == "59s"


def test_60_seconds_rolls_to_one_minute():
    assert duration_human(60, ja=True) == "1\u5206"
    assert duration_human(60, ja=False) == "1m"


def test_59_minutes_is_still_minutes():
    # 59 minutes exactly = 3540s; seconds are dropped below the hour
    assert duration_human(3540, ja=True) == "59\u5206"
    assert duration_human(3540, ja=False) == "59m"
    # 59m59s still reads as 59m (seconds dropped), never rolls to an hour early
    assert duration_human(3540 + 59, ja=True) == "59\u5206"


def test_one_hour_is_an_hour_with_no_minute_part():
    assert duration_human(3600, ja=True) == "1\u6642\u9593"
    assert duration_human(3600, ja=False) == "1h"


def test_one_hour_twelve_minutes_reads_as_prose():
    assert duration_human(3600 + 12 * 60, ja=True) == "1\u6642\u959312\u5206"
    assert duration_human(3600 + 12 * 60, ja=False) == "1h12m"


def test_a_day_part_appears_past_twenty_four_hours():
    assert duration_human(86400 + 3600, ja=True) == "1\u65e51\u6642\u9593"
    assert duration_human(86400, ja=True) == "1\u65e50\u6642\u9593"


def test_a_missing_timestamp_yields_no_duration():
    # sec < 0 is the sentinel the caller passes when a start or done ts is absent
    assert duration_human(-1, ja=True) == ""
    assert duration_human(-1, ja=False) == ""


def test_zero_seconds_is_zero_seconds_not_empty():
    # a 0s run is a known duration, distinct from the missing-ts case
    assert duration_human(0, ja=True) == "0\u79d2"
    assert duration_human(0, ja=False) == "0s"


# ── the C# side exists and reads the way the Python reference assumes ──────────────────

def test_duration_human_helper_exists_in_header():
    s = _read("FleetCockpit.Header.cs")
    assert "static string DurationHuman(double sec, bool ja)" in s, \
        "DurationHuman helper is missing from FleetCockpit.Header.cs"
    # the sec < 0 -> "" sentinel the caller relies on
    assert 'if (sec < 0) return "";' in s, s[s.index("DurationHuman"):][:400]


def _meta_block(s):
    # the exact slice the PR #300 test uses, so both read the same region
    i = s.index("var meta = new StringBuilder();")
    return s[i:s.index("var ml = new TextBlock", i)]


def test_duration_is_appended_to_the_right_of_the_turn_count():
    b = _meta_block(_read("FleetCockpit.Card.cs"))
    # the turn count append, and the duration append, both present
    turn_at = b.index('.Append(T("turn"))')
    assert "DurationHuman(runDoneTs - startTs" in b, b
    dur_at = b.index("DurationHuman(runDoneTs - startTs")
    # RIGHT of the turn count == appears later in the build order
    assert dur_at > turn_at, "duration must be appended after (to the right of) the turn count"
    # the JA/EN label, short enough for the card width
    assert '"\u6240\u8981 "' in b, b
    assert '"took "' in b, b


def test_duration_only_renders_when_both_timestamps_are_known():
    b = _meta_block(_read("FleetCockpit.Card.cs"))
    # guarded by terminal AND both ts > 0; nothing is appended otherwise (no guessing)
    assert "if (startTs > 0 && runDoneTs > 0)" in b, b
    i = b.index("DurationHuman(runDoneTs - startTs")
    pre = b[:i]
    assert "if (terminal)" in pre, "the duration append must sit under a terminal guard"


def test_the_pr300_worker_hiding_invariant_is_untouched():
    # name.ToUpper() must still sit on the live (else) branch, never the terminal one --
    # adding the duration must not move it. Same assertion shape as the PR #300 test.
    b = _meta_block(_read("FleetCockpit.Card.cs"))
    assert "meta.Append(name.ToUpper());" in b, b
    pre = b[:b.index("meta.Append(name.ToUpper());")]
    assert pre.rfind("else") > pre.rfind("if (terminal)"), \
        "name.ToUpper() must stay on the else (live) branch"
