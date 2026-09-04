"""web_fetch handed workers mojibake, and could not tell "blocked" from "broken".

MEASURED 2026-09-04, on a real 290-cinema survey run across the fleet:

  * TOHO's site is Shift_JIS and declares it in a meta tag while sending no charset in the HTTP
    header. httpx's .text therefore decoded it as UTF-8 and produced mojibake. Nine workers hit
    the same wall; ONE worked around it by decoding cp932 by hand, and the rest recorded the
    cinemas as undetermined. A per-worker workaround for a tool-level defect is a defect that
    gets solved once per worker, badly.
  * x.com and every no-auth mirror of it answer 503 with a corporate filter's "Web Page Blocked"
    page, category social-networking. Workers read that as an outage, retried, and recorded "no
    information" -- so whole regions came back empty for a reason nobody could see afterwards.
    "I was not allowed to look" and "I looked and found nothing" are different facts.

These run the decoder against real byte sequences rather than asserting on its source.
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import web_ops as W  # noqa: E402


# -- the encoding the page declares about itself ----------------------------------------------

def test_a_shift_jis_page_that_only_declares_it_in_html_is_read_correctly():
    """THE MEASURED CASE. No charset in the header, declared in a meta tag."""
    text = "劇場版 まどか☆マギカ 配布終了"
    html = ('<html><head><meta http-equiv="Content-Type" '
            'content="text/html; charset=Shift_JIS"></head><body>' + text +
            "</body></html>").encode("cp932")
    got, used, how = W._decode_body(html, "text/html", None)
    assert text in got
    assert used == "cp932" and how == "meta tag"


def test_vendor_characters_survive_because_shift_jis_is_read_as_cp932():
    """A page saying Shift_JIS still uses ㈱ and ①, which shift_jis proper cannot decode --
    so honouring the declared name literally fails on the characters these pages most use."""
    text = "㈱東宝 ①番スクリーン"
    html = ('<meta charset="shift_jis">' + text).encode("cp932")
    got, used, _how = W._decode_body(html, "", None)
    assert text in got and used == "cp932"


def test_the_http_header_wins_over_the_page():
    body = "テスト".encode("euc_jp")
    got, used, how = W._decode_body(body, "text/html; charset=euc-jp", None)
    assert got == "テスト" and used == "euc_jp" and how == "HTTP header"


def test_an_explicit_argument_wins_over_a_page_that_lies():
    """The escape hatch: a site declaring UTF-8 while serving cp932 is still readable."""
    body = "配布終了".encode("cp932")
    lying = b'<meta charset="utf-8">' + body
    got, used, how = W._decode_body(lying, "", "cp932")
    assert "配布終了" in got and used == "cp932" and how == "argument"


def test_ordinary_utf8_still_reads_as_utf8():
    got, used, how = W._decode_body("ふつうのページ".encode("utf-8"), "text/html", None)
    assert got == "ふつうのページ" and used == "utf-8" and how == "default"


def test_undecodable_bytes_are_labelled_rather_than_passed_off_as_content():
    """Replacement characters are an honest answer, but the caller has to know they are ours
    and not the site's."""
    got, used, how = W._decode_body(b"\xff\xfe\xff\xfe", "", None)
    assert "replacements" in how and used == "utf-8" and got


def test_a_declared_encoding_that_does_not_exist_falls_through_instead_of_raising():
    got, _used, _how = W._decode_body("ok".encode("utf-8"), "text/html; charset=nonsense-9", None)
    assert got == "ok"


@pytest.mark.parametrize("declared,expected", [
    ("Shift_JIS", "cp932"), ("shift-jis", "cp932"), ("SJIS", "cp932"),
    ("windows-31j", "cp932"), ("EUC-JP", "euc_jp"), ("utf-8", "utf-8"),
])
def test_the_names_japanese_sites_actually_use_all_resolve(declared, expected):
    assert W._normalise_encoding(declared) == expected


# -- blocked is not broken ---------------------------------------------------------------------

def test_the_corporate_block_page_is_recognised():
    """Verbatim shape of the page measured on 2026-09-04."""
    page = ("<html><body><h1>Web Page Blocked</h1>Access to the web page you were trying to "
            "visit has been blocked in accordance with company policy. "
            "Category: social-networking</body></html>")
    assert W._blocked_by_policy(page)


def test_an_ordinary_page_is_not_mistaken_for_a_block():
    """A false positive here tells a worker to stop looking at a site that was fine."""
    assert not W._blocked_by_policy(
        "<html><body>本日の上映スケジュール。特典の配布は終了しました。</body></html>")


def test_a_page_merely_discussing_blocking_is_not_a_block():
    assert not W._blocked_by_policy("How to unblock a web page when your company policy is strict")
