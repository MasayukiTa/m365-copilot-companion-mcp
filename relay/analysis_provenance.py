# -*- coding: utf-8 -*-
"""A number in a data-analysis answer has to have been COMPUTED in this run.

THE FAILURE THIS PREVENTS (owner, verbatim intent 2026-11-02)

  Using a spreadsheet is natural -- the source data is in spreadsheets. But a task folder
  also holds DERIVED artifacts: result workbooks, conclusion reports, pre-made numbers that
  an earlier run (or anyone) left behind. Reporting those as the answer is a harness defect:
  one could plant a conspiracy-theory dataset's "results" in the folder beforehand and the
  agent would read them back out as fact. Evidence: job 065d8c1c4a14 answered "the
  investigation is already complete, result files exist" and reported correlations from them
  WITHOUT running a single computation on the raw tables.

THE RULE, DOMAIN-FREE

  A derived artifact is UNTRUSTED. Its numbers may appear in the final answer only if the
  SAME numbers were recomputed in THIS run from the raw input, by an executed computation
  whose output is in this run's tool results. A number that appears only in a file read from
  the folder -- or nowhere in this run's outputs at all -- fails completion with a correction
  that names what to do: recompute from the raw input; treat existing result files as claims
  to verify, not as the answer. An analysis answer that comes with ZERO executed computations
  on raw data cannot complete.

WHY A SEPARATE AXIS FROM relay/provenance.py. That module is about AUTHORITY -- whether a
piece of evidence may justify changing the harness. This one is about RECOMPUTATION -- whether
a number in an answer was produced by this run or merely transcribed. A number can be perfectly
trusted in authority terms and still be a transcription; the two checks do not substitute.

WHAT IS HERE, AND WHAT IS NOT. Pure functions over (final answer text, this run's computation
outputs). No clock, no I/O, no ledger reading: the caller supplies the outputs it already
collected from the attributed ledger rows, so a test can drive the whole decision without a
ledger. Deliberately conservative against FALSE rejection -- rounding, units, and
percent-versus-fraction must still match -- because a gate that rejects honest answers is
turned off, and then it protects nothing.
"""
from __future__ import annotations

import os
import re

#: Tools whose OUTPUT is a computation over raw data this run performed itself. A number that
#: appears in one of these outputs was produced in this run, not transcribed from a folder.
#: run_python is the general compute path; odbc_query / odbc_to_excel pull the raw input tables
#: and (for a query that aggregates) compute over them. read_file / read_excel / open_workbook
#: are deliberately ABSENT: reading a derived results file is exactly the move this gate exists
#: to refuse, so a number seen only through a read does not count as computed.
COMPUTE_TOOLS = frozenset({"run_python", "odbc_query", "odbc_to_excel"})

#: A number in an answer is matched against a computed number within this RELATIVE tolerance,
#: to absorb rounding and display precision (0.1234 reported as 0.12, 1234.5 as 1235). Chosen
#: rather than an absolute epsilon because the magnitudes in an analysis answer span many
#: orders (a p-value of 0.003 and an n of 20000 in the same report), and one absolute epsilon
#: cannot be right for both.
REL_TOL = 5e-3

#: Below this magnitude a relative tolerance collapses (everything is "close" to zero), so an
#: absolute floor takes over. 5e-4 passes 0.0 vs 0.0004 -- a p-value rounded to three places --
#: without passing 0.0 vs 0.05.
ABS_FLOOR = 5e-4

# A run of digits with optional sign, decimal part, thousands separators, exponent, and a
# trailing percent sign. The percent is captured so a value written as "12.5%" can also be
# matched against a computed fraction 0.125 (see _match_one).
#
# A trailing lookahead rejects a digit run that is immediately followed by a letter, an
# underscore, or a dot-letter -- i.e. the head of an identifier or hash like "511e078026f6"
# or "20abc". The original guard only looked BEHIND, so "511" at the start of a hex id slipped
# through (it is this exact false positive -- the "511" of "[ref:511e078026f6]" -- that this
# change exists to stop). Pure-digit groups followed by "%" or end-of-number are still matched.
_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"           # not mid-identifier and not the tail of a longer number
    r"([+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?"   # 1,234 or 1,234.5
    r"|[+-]?\d+\.\d+"                       # 0.125
    r"|[+-]?\.\d+"                          # .5
    r"|[+-]?\d+)"                           # 42
    r"(?:[eE][+-]?\d+)?"                    # 1e-3
    r"(\s*%)?"                             # trailing percent
    r"(?![A-Za-z_])"                       # not immediately followed by an identifier letter
)

#: Spans masked out of the answer text BEFORE number extraction, because the digits in them
#: are identifiers or metadata, never a reported analysis value. Each is replaced by spaces of
#: equal length (so positions and the lookbehind/lookahead around surviving numbers are kept).
#: Ordered general-to-specific where it matters; applied as a batch in _mask_non_values.
#: Evidence: two real FALSE positives this gate produced --
#:   * "[ref:511e078026f6]" was read as the number 511 (citation tag + hex id);
#:   * a "top 50" screening answer was read as the number 20. The 20 that mattered is a
#:     genuine computed statistic (K_coat_lots_total=20 in this run's outputs) and MUST stay
#:     policed; the spurious 20s are non-values -- a version/id token ("v20", "_v20") and
#:     markdown list scaffolding -- which the version and list masks below already remove. A
#:     number written in prose as a fold/times magnitude (「20倍」) is NOT masked: if it is a
#:     real reported value it will be found among the computed outputs, and if it was copied
#:     from a results file with no computation behind it the gate SHOULD flag it.
#: We mask citation tags, hex hashes/ids, file paths and names, dates and times, version
#: strings, markdown list numbering and headings, and #-prefixed PR/issue numbers. A SECOND
#: class -- standard/product/format NAMES and their units (ISO 8601, UTF-8, SHA-256, RFC <n>,
#: HTTP 200, Python 3.10, CP932, p95, ordinals, counters, ports, timezones, code spans) -- is
#: in _MASK_STANDARD_NAMES just below. Years are handled separately by range in extract_numbers.
#: We do NOT mask bare decimals, percentages, or unit-bearing counts: those are exactly the
#: statistics the gate must keep policing.
_MASK_RES = (
    # Citation tags like [ref:511e078026f6] (any bracketed ref:/cite:/id: + token).
    re.compile(r"\[(?:ref|cite|id|cid)\s*:[^\]]*\]", re.IGNORECASE),
    # Explicitly labelled hashes/ids: sha, commit, job, run, pr-id, uuid, session, hash.
    re.compile(r"\b(?:sha(?:256|1)?|commit|job|run|session|uuid|guid|hash|id)\b[:#=\s]*[0-9a-fA-F-]{4,}", re.IGNORECASE),
    # Bare hex-ish ids: a run of [0-9a-f] of length >=5 that contains at least one a-f letter
    # (so pure-digit numbers are NOT caught). Catches 511e078026f6, 8c3daa76ccf0, 267a5740.
    re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{5,}\b"),
    # File paths (contain a slash or backslash) and bare file names (name.ext).
    re.compile(r"[A-Za-z0-9_./\\-]*[\\/][A-Za-z0-9_./\\-]+"),
    re.compile(r"\b[\w-]+\.(?:csv|xlsx|xlsm|xls|json|md|py|txt|png|jpg|parquet|sql|yml|yaml|log)\b", re.IGNORECASE),
    # Source-line locus after a file/colon: ":510-511", ":304-320" (and the single ":830" the
    # port mask already covers). The filename rule above blanks "analysis_stable.py" but leaves
    # the trailing ":510-511"; the port mask then eats only ":510" and the "-511" survives and is
    # read as the number -511 -- the exact FALSE positive job 8c3daa76ccf0 failed on (a docstring
    # pointer "analysis_stable.py:510-511", not an analysis value). A colon-prefixed numeric RANGE
    # is a code locus, never a reported statistic (an interval is written [a, b] or a\u2013b, never
    # ":a-b"), so masking the whole ":\d-\d" span is safe and keeps real intervals policed.
    re.compile(r":\d{1,5}-\d{1,5}\b"),
    # ISO-ish dates and date-times: 2026-11-02, 2026/11/02, optional T12:43:18(+09:00).
    re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)?"),
    # Clock times on their own: 12:43, 09:20:05.
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    # Version strings: v20, v1.8, _v2, version 3, ver. 2.1.
    re.compile(r"(?:\bver(?:sion)?\.?\s*|(?<![A-Za-z0-9])_?v)\d+(?:\.\d+)*", re.IGNORECASE),
    # #-prefixed PR / issue numbers: #401, #12.
    re.compile(r"#\d+\b"),
    # Markdown list numbering / headings at line start: "1.", "2)", "  3. ".
    re.compile(r"(?m)^\s*\d+[.)]\s"),
)

#: A SECOND class of false positives: digits that belong to a STANDARD / PRODUCT / FORMAT name,
#: a unit of a format, an ordinal label, or a counter -- never a measured analysis value.
#: Evidence: job e6541c75bcd9 (a Friday merge worker) ended VERIFY_FAILED on "8601", which was
#: the "8601" of the standard name "ISO 8601" in the worker's explanation of the judge_request
#: format ("fetched_at(ISO 8601 の時刻)"). It is not a statistic anyone computed. The same shape
#: covers UTF-8/UTF-16, SHA-256/SHA1/MD5, RFC <n>, HTTP status codes ("HTTP 200"), interpreter
#: and app version names (Python 3.10, Windows 11, Excel 2016), CP932, base64, "p95"/"p99"
#: percentile NAMES, ordinal labels ("第1段", "step 2", "attempt 1/3", "(1/3)"), turn/job
#: counters, port numbers, time-of-day/timezone strings, and anything inside a code span or
#: backtick-quoted identifier. These are masked BEFORE number extraction so their digits never
#: become claimed numbers. We still do NOT mask bare decimals, percentages, or unit-bearing
#: counts: a planted statistic must stay policed (see the tests that keep those rejected).
_MASK_STANDARD_NAMES = (
    # Code spans / backtick-quoted identifiers and file names: `foo 3.10`, ``a | b``. Everything
    # between matching backticks is an identifier/snippet, not prose, so its digits are masked.
    re.compile(r"`[^`]*`"),
    # ISO 8601 (and ISO <n> family), optionally with a trailing part number.
    re.compile(r"\bISO[\s/-]?\d{3,5}(?:[-:]\d+)*\b", re.IGNORECASE),
    # Character encodings: UTF-8, UTF-16, UTF-32, UCS-2.
    re.compile(r"\b(?:UTF|UCS)[\s-]?\d{1,2}\b", re.IGNORECASE),
    # Hash/digest algorithm names: SHA-256, SHA256, SHA-1, SHA1, SHA-512, MD5, CRC32.
    re.compile(r"\b(?:SHA|MD|CRC)[\s-]?\d{1,3}\b", re.IGNORECASE),
    # Code page / encoding ids written with digits: CP932, CP1252, Latin1, base64, UTF8 etc.
    re.compile(r"\b(?:CP|Latin|Win|ISO-?8859-|Shift_JIS)[\s-]?\d{1,4}\b", re.IGNORECASE),
    re.compile(r"\bbase64\b", re.IGNORECASE),
    # RFC / PEP / errno-style standard references: RFC 3339, PEP 8.
    re.compile(r"\b(?:RFC|PEP|ECMA|ANSI|IEEE|EN)[\s-]?\d{1,5}\b", re.IGNORECASE),
    # HTTP status codes and protocol versions: "HTTP 200", "HTTP/1.1", "status 404".
    re.compile(r"\bHTTP\s*/?\s*\d(?:\.\d)?\d*\b", re.IGNORECASE),
    re.compile(r"\b(?:status|code)\s+(?:1\d\d|2\d\d|3\d\d|4\d\d|5\d\d)\b", re.IGNORECASE),
    # Interpreter / application / OS version NAMES: Python 3.10, Windows 11, Excel 2016,
    # Node 18, Java 21, Pandas 2.1, numpy 1.26, .NET 8, Ubuntu 22.04, macOS 14.
    re.compile(r"\b(?:Python|Windows|Excel|Word|PowerPoint|Office|Node(?:\.js)?|Java|Pandas|"
               r"NumPy|SciPy|Ubuntu|Debian|macOS|Linux|SQLite|PostgreSQL|MySQL|SQL\s*Server|"
               r"\.NET|dotnet|openpyxl|pyodbc)\s*v?\d+(?:\.\d+)*\b", re.IGNORECASE),
    # Percentile / quantile NAMES written as a label: p95, p99, P50, q90.
    re.compile(r"(?<![A-Za-z0-9_])[pPqQ]\d{1,3}\b"),
    # Western ordinal / step / attempt labels: "step 2", "attempt 1/3", "turn 10", "job 5",
    # "phase 2", "round 3", "part 4", "item 7", "figure 1", "table 2".
    re.compile(r"\b(?:step|attempt|turn|job|phase|round|part|item|figure|fig|table|tab|retry|"
               r"iteration|iter|slice|subtask|task|worker|batch|epoch|trial)\.?\s*#?\d+"
               r"(?:\s*(?:/|of)\s*\d+)?\b", re.IGNORECASE),
    # Japanese ordinal labels: 第1段, 第2回, 3件目 is NOT masked (件 is a unit) -- only the
    # 第<n><counter> form where 第 marks it as an ordinal, e.g. 第1段/第2章/第3回/第1部.
    re.compile(r"第\s*\d+\s*(?:段|回|章|部|節|項|版|次|位|号)"),
    # Bare "(1/3)" / "1/3" progress counters: a fraction-of-attempts, not a measured ratio.
    re.compile(r"[(\uff08]\s*\d+\s*/\s*\d+\s*[)\uff09]"),
    # Port numbers: "port 8080", ":50379" following a host/identifier.
    re.compile(r"\bport\s*:?\s*\d{2,5}\b", re.IGNORECASE),
    # A host:port or bare ":<port>" token. Clock times (12:43) are already masked by _MASK_RES
    # above, so a surviving ":<2-5 digits>" here is a port, not a time.
    re.compile(r":\d{2,5}\b"),
    # Time-of-day and timezone strings not caught by the date rules: "UTC+9", "+09:00", "JST",
    # "GMT+0900". The bare clock "12:43" is already masked by _MASK_RES.
    re.compile(r"\b(?:UTC|GMT)\s*[+-]\s*\d{1,2}(?::?\d{2})?\b", re.IGNORECASE),
    re.compile(r"(?<![0-9]):[+-]\d{2}:?\d{2}\b"),
)


def _mask_non_values(text: str) -> str:
    """Blank out identifier/metadata spans so their digits never become claimed numbers.

    Each matched span is replaced by spaces of the SAME length, so byte offsets -- and hence
    the lookbehind/lookahead that protect surviving numbers -- stay valid. This is applied
    once before _NUM_RE runs; it is deliberately conservative: it only removes shapes that are
    not analysis values (citations, hashes, paths, dates, versions, list markers, #numbers),
    leaving every decimal, percentage, and unit-bearing count in place to be checked.
    """
    def blank(m):
        return " " * (m.end() - m.start())
    for rx in _MASK_RES:
        text = rx.sub(blank, text)
    # Second class: standard/product/format names, units of a format, ordinal labels, counters,
    # ports, timezones, and code-span contents -- their digits are never a measured value.
    for rx in _MASK_STANDARD_NAMES:
        text = rx.sub(blank, text)
    return text

#: Tokens that are identifiers or labels rather than measured quantities. A version "v1.8", a
#: year "2026", a claim id "config_12", a path "part 3" -- matching these against computed
#: numbers would be noise, and a FALSE match (a year that happens to equal a count) would let a
#: transcription through. Years are the common one and are dropped by range below; the rest are
#: dropped by the non-identifier lookbehind in _NUM_RE.
_YEAR_RANGE = (1900, 2100)

#: SMALL WHOLE COUNTS ARE NOT POLICED AS CLAIMS. A bare integer whose magnitude is in 0..10,
#: written with no decimal, no thousands separator and no percent, is almost always a COUNT or
#: an ORDER -- "7 ロットを確認", "0 件の逸脱", "上位 4 件", "2 群" -- not a measured statistic a
#: results file could have planted. Evidence (owner, 2026-11-03): in the real rejection record
#: (.fleet/status.json) 37.5% of the numbers this gate flagged were exactly such small integers
#: (0,4,7,...), and the detection value on them is low while the false-rejection cost is high:
#: an honest worker who writes "7 ロット" after counting rows in run_python output is sent back
#: for a 7 that trivially appears everywhere. So on the CLAIM side (policing=True) a small whole
#: number is dropped before it becomes something the answer must justify. It is NOT dropped on
#: the COMPUTED side: a computation that legitimately emits 7 still counts as support for any
#: larger claim, and nothing about the computed list changes. A decimal (4.0 written as "4.00"),
#: a percent ("4%"), or a thousands-grouped value is never small-count: those keep being policed.
_SMALL_COUNT_MAX = 10


def _is_small_count(digits: str, had_percent: bool, val: float) -> bool:
    """Whether this match is a small whole count/order (0..10 integer, no decimal/percent/comma)
    that the gate should not police as a claimed statistic. Mirrors the year-drop shape."""
    return (not had_percent and "." not in digits and "," not in digits
            and val == int(val) and 0 <= abs(val) <= _SMALL_COUNT_MAX)


def _to_float(digits: str, percent: bool):
    """The numeric value of one match, or None if it will not parse. Percent is NOT folded in
    here -- the raw face value and the percent flag are both returned to the matcher, which
    tries the value both as written and as a fraction."""
    try:
        return float(digits.replace(",", ""))
    except (ValueError, AttributeError):
        return None


#: The CHARACTERS that, when they sit immediately beside a digit run, make that run part of an
#: IDENTIFIER rather than a standalone measured quantity: ASCII letters/underscore (already
#: largely handled by _NUM_RE's own lookaround and the path/version masks), the '#' and path
#: separators, and -- the gap those leave -- any CJK letter. "679かすれ" reads 679 with a CJK
#: ideograph glued to its right: that is the requester's case NAME, not a statistic. A Western
#: digit-then-letter token like "20abc" is already refused by _NUM_RE; this set exists so the
#: same refusal reaches digit-then-CJK, which _NUM_RE's [A-Za-z_] lookahead does not cover.
_IDENT_ADJACENT_RE = re.compile(
    r"[A-Za-z0-9_#/\\"
    r"\u3040-\u30ff"          # Hiragana + Katakana
    r"\u3400-\u4dbf"          # CJK Extension A
    r"\u4e00-\u9fff"          # CJK Unified Ideographs
    r"\uf900-\ufaff"          # CJK Compatibility Ideographs
    r"\uff66-\uff9f"          # Half-width Katakana
    r"]")

#: Units/affixes that turn a digit run into a STATED QUANTITY even when a CJK character is
#: adjacent: a percent sign, and the common Japanese counters and magnitude words. When one of
#: these follows the digits ("679 件", "679%", "20倍"), the token is a measurement the gate must
#: still police -- it is NOT exempted as an identifier, even if the same digits are in the goal.
#: This is deliberately a quantity-form allow-list layered OVER the identifier test below, so a
#: name like "679かすれ" (かすれ is not a counter) stays exempt while "679件" does not.
_QUANTITY_SUFFIX_RE = re.compile(
    r"\s*(?:%|\uff05|\u500d"                               # %, ％, 倍
    r"|\u4ef6|\u500b|\u672c|\u53f0|\u56de|\u540d|\u679a"   # 件 個 本 台 回 名 枚
    r"|\u884c|\u5217|\u793e|\u4eba|\u70b9|\u5272"         # 行 列 社 人 点 割
    r"|\u30ed\u30c3\u30c8"                                 # ロット
    r")")


def _request_digit_runs(request_text) -> frozenset:
    """The maximal runs of digits that appear in the requester's own words (goal/request text),
    as strings, length >= 2.

    These are the digit sequences the gate may treat as part of an IDENTIFIER the requester
    supplied -- the '679' of "679かすれ調査", "e679_v26", "example_analysis". Length >= 2 keeps a
    lone '0'/'1'/... (which occurs in almost any text) from exempting real single-digit
    statistics. Returns an empty set for non-strings so the caller degrades to the old behaviour.
    """
    if not isinstance(request_text, str) or not request_text:
        return frozenset()
    return frozenset(r for r in re.findall(r"\d+", request_text) if len(r) >= 2)


def extract_numbers(text, request_digits=None, *, policing=False) -> list:
    """Every numeric quantity in `text`, as (value, had_percent) pairs.

    A LIST, NOT A SET, and the percent flag is kept: 0.12 and 12% are different faces of the
    same quantity and the matcher needs to try both. Identifier-shaped numbers (years, and
    anything caught mid-token by the lookbehind) are excluded here so a report's "2026" or
    "engine_v1_8" does not create a spurious claim the answer then has to justify.

    `request_digits` (optional) is the set of digit runs the REQUESTER wrote (see
    _request_digit_runs). When a matched digit run equals one of those AND sits inside an
    identifier-like token in the answer -- a CJK/letter/underscore/#/path character glued to
    one side, as in "679かすれ" -- it is the requester's case NAME, not a computed result, so it
    is dropped. A run in a QUANTITY form ("679 件", "679%", "n = 679") is NEVER dropped this
    way: a stated quantity still has to be found in a computation output, even when the same
    digits occur in the goal.

    `policing` distinguishes the two callers. On the CLAIM side (policing=True, the answer being
    checked) a small whole count/order (0..10 integer, no decimal/percent/comma) is dropped --
    it is a count like "7 ロット" or an order like "上位 4 件", not a statistic a results file
    plants, and policing it costs honest answers (see _SMALL_COUNT_MAX). On the COMPUTED side
    (policing=False, the default, the computation outputs) nothing is dropped: a computed 7 must
    still be available to support a larger claim, so the computed list is unchanged.
    """
    if not isinstance(text, str):
        return []
    req = request_digits or frozenset()
    text = _mask_non_values(text)
    out = []
    for m in _NUM_RE.finditer(text):
        had_percent = bool(m.group(2))
        val = _to_float(m.group(1), had_percent)
        if val is None:
            continue
        # SMALL WHOLE COUNT / ORDER, claim side only. A 0..10 integer with no decimal, comma or
        # percent is a count or a rank, not a measured statistic; dropping it on the claim side
        # stops the dominant false rejection (37.5% of real flags) without touching the
        # computed side, where the same value may legitimately support a larger claim.
        if policing and _is_small_count(m.group(1), had_percent, val):
            continue
        # A bare integer in the year range with no decimal and no percent is almost always a
        # date, not a measured quantity. Dropping it is conservative in the SAFE direction:
        # the risk of a dropped claim is a transcription slipping through, but a year is not a
        # statistic anyone plants, and keeping it would reject honest answers that mention one.
        if (not had_percent and "." not in m.group(1) and "," not in m.group(1)
                and val == int(val) and _YEAR_RANGE[0] <= val <= _YEAR_RANGE[1]):
            continue
        # REQUESTER-SUPPLIED IDENTIFIER. The exact digit run the requester wrote (e.g. "679" of
        # "679かすれ調査"), appearing in the answer glued to a CJK/letter/underscore/#/path
        # character, is the case NAME, not a statistic -- unless it wears a quantity form
        # (percent or a counter), which a stated measurement always has to justify.
        if req and not had_percent:
            digit_run = re.sub(r"\D", "", m.group(1))
            if digit_run in req and _is_requester_identifier(text, m):
                continue
        out.append((val, had_percent))
    return out


def _is_requester_identifier(text: str, m) -> bool:
    """Whether the matched digit run sits inside an identifier-like token rather than standing
    alone as a stated quantity.

    TRUE only when a letter/underscore/#/path char or a CJK character is glued to one side of
    the run AND the run is not in a quantity form. A quantity form -- a trailing percent or a
    Japanese counter/magnitude word (件, 倍, ...) -- returns FALSE so the number stays policed.
    `text` is the already-masked answer; positions from `m` are valid against it.
    """
    start, end = m.start(1), m.end(1)
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    # A trailing percent was captured by the regex itself (group 2); a quantity suffix
    # (件/倍/...) immediately after the digits means this is a stated amount, not a name.
    if m.group(2):
        return False
    if _QUANTITY_SUFFIX_RE.match(text[end:]):
        return False
    adjacent = bool(_IDENT_ADJACENT_RE.match(before)) or bool(_IDENT_ADJACENT_RE.match(after))
    return adjacent


def _close(a: float, b: float) -> bool:
    """Whether two numbers are the same up to rounding. Relative above the floor, absolute
    below it, so both a p-value near zero and a count in the tens of thousands compare sanely."""
    if a == b:
        return True
    diff = abs(a - b)
    if diff <= ABS_FLOOR:
        return True
    scale = max(abs(a), abs(b))
    return scale > 0 and diff / scale <= REL_TOL


def _match_one(value: float, had_percent: bool, computed: list) -> bool:
    """Is this one claimed value present among the computed values, allowing rounding, a sign
    we do not insist on, and percent-versus-fraction?

    THREE FACES ARE TRIED, each a legitimate way the same quantity appears on either side:
      * the value as written;
      * percent<->fraction (12.5% vs 0.125, and 0.125 vs 12.5 the other way), because which
        form a computation emits and which a human writes are independent choices;
      * absolute value, because a correlation reported as 0.42 may be computed as -0.42 and the
        sign is a direction, not a different measurement. This is the one liberty that could
        hide a real disagreement, and it is taken deliberately: a gate against TRANSCRIPTION
        must not fail an answer whose only difference from the computation is a sign
        convention, and a sign error that is a real error is the judge's job, not this gate's.
    """
    faces = {value, abs(value)}
    if had_percent:
        faces.add(value / 100.0)
        faces.add(abs(value) / 100.0)
    else:
        # The answer wrote a fraction; a computation may have emitted a percent, or vice versa.
        faces.add(value * 100.0)
        faces.add(abs(value) * 100.0)
    for face in faces:
        for c in computed:
            # Compare against the computed value AND its magnitude: a correlation computed as
            # -0.42 and reported as 0.42 is the same measurement with a sign convention, and
            # the sign liberty has to be taken on BOTH sides or it only works when the answer,
            # not the computation, dropped the sign.
            if _close(face, c) or _close(face, abs(c)):
                return True
    return False


def _computed_values(outputs) -> list:
    """Flatten this run's computation outputs into a list of numeric values.

    `outputs` is an iterable of strings (the text of each compute tool's result). Percent is
    not tracked on the computed side -- the matcher already tries both faces of each CLAIMED
    number, so a computed "12.5" matches a claimed "0.125" regardless of which side wore the
    sign.
    """
    values = []
    for text in outputs or []:
        for val, _pct in extract_numbers(text if isinstance(text, str) else str(text)):
            values.append(val)
    return values


class ProvenanceResult(dict):
    """The outcome of the check. A dict so it travels into the judge input and the footer
    unchanged, with named keys read by the caller:

      ok            -- True when every claimed number is accounted for AND (for an analysis
                       with numbers) at least one computation ran.
      had_compute   -- whether any compute tool output was supplied at all.
      unsupported   -- the claimed values present in the answer but in no computation output.
      checked       -- how many distinct claimed values were examined.
      reason        -- a short machine-stable tag for the record.
      correction    -- '' when ok, else the message naming what to do. Caller-facing.
    """


def check_answer_provenance(final_answer, compute_outputs, *, is_analysis: bool,
                            request_text="", run_file_outputs=None) -> ProvenanceResult:
    """Does every number in the answer trace to something this run actually produced -- a
    computation it ran, OR a file it created/changed/read in this run?

    `compute_outputs` is the list of result texts from THIS run's compute-tool calls (the
    caller collects them from the attributed ledger -- see relay_fleet). `is_analysis` is the
    gate's own applicability: when False the check is a no-op pass, because a non-analysis
    answer carries numbers (dates, counts, ids) this rule has no business policing.

    `run_file_outputs` (optional) is the list of BODY TEXTS of files this run touched within
    its own lifetime: files it created or changed (the pptx/md/json/csv/txt deliverables of
    this job -- their text, python-pptx slide text included) and files it READ (an input table
    or a prior note it actually opened this run). The caller (relay_fleet) builds this from the
    attributed ledger, keeping only files whose modified-time is at or after this run's start
    (for writes) or that this run read at all, so that a stale results file the folder already
    held and this run NEVER read is excluded -- transcribing its numbers is still caught.
    A number that appears in one of these texts is as produced-by-this-run as a computed one;
    both lists are legitimate sources and are checked together. When run_file_outputs is empty
    the check degrades to its prior behaviour exactly (compute outputs only).

    `request_text` (optional) is the requester's own words -- the goal text and/or the resolved
    request text. Digit runs that appear THERE (a case name like "679かすれ調査", an id like
    "e679_v26") are treated as identifiers, NOT claims, when they appear in the answer glued to
    a letter/underscore/#/path/CJK character rather than standing alone as a stated quantity.
    A quantity form ("679 件", "679%", "n = 679") is still policed even if the same digits are
    in the goal: a stated measurement must always be found in a computation output. When
    request_text is empty the check degrades to its prior behaviour exactly.

    CONSERVATIVE BY CONSTRUCTION. An answer with no numbers passes. A non-analysis goal passes.
    The only two ways to fail are the two the owner named: a number that is in the answer but
    in no computation output, and an analysis answer that cites numbers with no computation at
    all behind them.
    """
    request_digits = _request_digit_runs(request_text)
    claimed = (extract_numbers(final_answer, request_digits, policing=True)
               if is_analysis else [])
    had_compute = bool([t for t in (compute_outputs or []) if str(t or "").strip()])
    had_file = bool([t for t in (run_file_outputs or []) if str(t or "").strip()])
    # The produced-in-this-run number pool: computation outputs AND the body text of files this
    # run created/changed/read. Both are flattened the same way; the computed side keeps its
    # default (policing=False) so small counts stay available as support.
    computed = _computed_values(compute_outputs) if had_compute else []
    if had_file:
        computed = computed + _computed_values(run_file_outputs)
    # 'A computation ran' for RULE 3 is satisfied by EITHER an executed computation OR a file
    # this run produced/read: both are evidence the run did real work and did not merely read a
    # planted results file back out. (A run that only read a stale file it did not touch has
    # neither, and still fails RULE 3.)
    had_source = had_compute or had_file

    if not is_analysis:
        return ProvenanceResult(ok=True, had_compute=had_compute, unsupported=[],
                                checked=0, reason="not an analysis answer", correction="")
    if not claimed:
        # An analysis answer with no numbers at all is not making a numerical claim to police.
        return ProvenanceResult(ok=True, had_compute=had_compute, unsupported=[],
                                checked=0, reason="no numeric claims in the answer",
                                correction="")

    # RULE 3: numbers but zero source. The answer asserts quantities with nothing in this run
    # that could have produced them -- neither a computation NOR a file this run made or read --
    # the exact shape of reading a results file back out.
    if not had_source:
        return ProvenanceResult(
            ok=False, had_compute=False, unsupported=[v for v, _ in claimed],
            checked=len({v for v, _ in claimed}),
            reason="analysis answer with zero executed computations",
            correction=(
                "この回答は数値を述べていますが、この実行では生データに対する計算が一つも"
                "行われていません。フォルダ内の結果ファイルやレポートの数値をそのまま答えに"
                "することはできません(それらは検証すべき主張であって答えではない)。生の入力"
                "テーブルから run_python 等で実際に再計算し、その出力に現れた数値だけを"
                "報告してください。"))

    # RULE 2: each claimed number must appear among the produced values (computation + files).
    seen = set()
    unsupported = []
    for val, had_percent in claimed:
        if val in seen:
            continue
        seen.add(val)
        if not _match_one(val, had_percent, computed):
            unsupported.append(val)

    if unsupported:
        shown = ", ".join(("%g" % v) for v in unsupported[:12])
        return ProvenanceResult(
            ok=False, had_compute=had_compute, unsupported=unsupported, checked=len(seen),
            reason="claimed numbers not found in this run's computation outputs",
            correction=(
                "次の数値は回答に現れますが、この実行の計算出力にも、この実行が作成・変更・"
                "読み取りしたファイルの本文にも現れません: %s。フォルダ内の既存の結果ファイル"
                "(この実行が触れていないもの)から転記した数値である可能性があります。生の"
                "入力テーブルから run_python 等で再計算し、その出力に現れた数値のみを報告して"
                "ください(既存の結果ファイルは検証すべき主張として扱う)。" % shown))

    return ProvenanceResult(ok=True, had_compute=had_compute, unsupported=[], checked=len(seen),
                            reason="every claimed number traces to a computation in this run",
                            correction="")


def provenance_note(result) -> str:
    """One line for the judge footer / input, stating what the gate saw. Never raises.

    Carries the PROVENANCE into the judge input (owner rule 4): how many numbers were checked,
    whether a computation ran, and which numbers -- if any -- had no computation behind them,
    so the final-step judge sees the basis of the pass or the reason for the fail.
    """
    try:
        r = result or {}
        if r.get("ok"):
            if not r.get("checked"):
                return "provenance: n/a (%s)" % r.get("reason", "")
            return ("provenance: ok (%d numeric claim(s) recomputed in this run)"
                    % int(r.get("checked") or 0))
        uns = r.get("unsupported") or []
        tag = "no computation" if not r.get("had_compute") else (
            "%d unsupported: %s" % (len(uns), ", ".join("%g" % v for v in uns[:12])))
        return "provenance: FAIL (%s)" % tag
    except Exception:
        return "provenance: unknown"


# --- the operator switch -----------------------------------------------------------------
#
# analysis_provenance_gate in the settings registry (tools/settings_keys.py), default ON. The
# owner can turn the gate off from the settings screen without a code change. Read fresh on
# each completion (EACH_GATE), mtime-cached like the other per-gate keys (cf.
# relay/effort_policy.py:_settings_value): one os.stat per call, a re-read only when the file
# changed, so a change made in the cockpit is seen by the NEXT completion in a running process
# with no restart. Absent, unreadable, or anything but "off" reads as ON (the conservative
# default -- a gate that protects against planted results should not silently turn itself off).
_GATE_KEY = "analysis_provenance_gate"
_settings_cache = {}  #: path -> ((path, mtime_ns, size), value)


def _settings_value(env=None):
    """The `analysis_provenance_gate=` value from settings.txt, lower-cased, or None when the
    line is absent/unreadable. With the real environment (env is None) the repository's one
    resolver (tools.settings_path.settings_file); with an injected env dict, ONLY the file named
    by its MCP_ANALYSIS_PROVENANCE_GATE_SETTINGS entry, so a test can never be steered by the
    developer's real .config. A BOM is tolerated. Never raises.
    """
    try:
        if env is None:
            from tools.settings_path import settings_file
            path = settings_file()
        else:
            path = str(env.get("MCP_ANALYSIS_PROVENANCE_GATE_SETTINGS", "") or "")
        if not path:
            return None
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        hit = _settings_cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = None
        with open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(_GATE_KEY + "="):
                    val = ln.split("=", 1)[1].strip().lower()
                    break
        _settings_cache[path] = (key, val)
        return val
    except Exception:
        return None


def gate_enabled(env=None) -> bool:
    """Whether the analysis provenance gate is ON. Default ON; only an explicit `off` disables
    it. An MCP_ANALYSIS_PROVENANCE_GATE environment variable, when set to off, beats the file
    (symmetry with the other gates' env override). Never raises -- on any error the gate stays
    ON, because the failure it prevents (reporting a planted results file as the answer) is
    worse than one extra engine check.
    """
    try:
        _env = os.environ if env is None else env
        ev = str(_env.get("MCP_ANALYSIS_PROVENANCE_GATE", "") or "").strip().lower()
        if ev == "off":
            return False
        if ev == "on":
            return True
        return _settings_value(env) != "off"
    except Exception:
        return True
