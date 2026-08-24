"""The conversation capture had stopped matching the page, and said nothing about it.

Measured on the live page: `button[id]` finds 36 elements and NONE of their ids is a guid,
while 19 elements whose id IS a bare guid are all anchors. The sidebar rows became `<a id>`.
Both DOM selectors were pinned to `button`, so both returned nothing, and the capture -- which
correctly refuses to guess -- left conv_url empty every time. 542 sessions on this machine,
11 with a conversation reference, none since 07-08.

The href also carries guids and is the wrong source: 24 of them, 5 unique, and they are agent
TitleIds rather than conversations. Reading those would have stored confident nonsense, which
is worse than storing nothing.

Source-level, like the sibling bridge suites: copilot_bridge.py imports Playwright at module
scope and cannot be imported on a runner.

Run: pytest -q bridge/test_conv_capture_selectors.py
"""
from pathlib import Path

SOURCE = (Path(__file__).with_name("copilot_bridge.py")).read_text(encoding="utf-8")


def _js(name):
    i = SOURCE.index(name + ' = r"""')
    j = SOURCE.index('"""', i + len(name) + 8)
    return SOURCE[i + len(name) + 8:j]


def test_the_selectors_no_longer_pin_the_tag():
    """The tag was never the thing being identified; the guid-shaped id is. Pinning it is
    what made this fail, and pinning a different one would fail the same way later."""
    assert "querySelectorAll('[id]')" in _js("_ALL_ROW_GUIDS_JS")
    assert "button[id]" not in _js("_ALL_ROW_GUIDS_JS")
    assert "querySelectorAll('[aria-current=\"page\"]')" in _js("_CURRENT_ROW_JS")
    assert "button[id]" not in _js("_CURRENT_ROW_JS")


def test_both_still_require_the_id_to_be_a_bare_guid():
    """Dropping the tag widens the net; the guid shape is what keeps it from catching
    everything else on the page."""
    for name in ("_ALL_ROW_GUIDS_JS", "_CURRENT_ROW_JS"):
        js = _js(name)
        assert "[0-9a-fA-F]{8}-" in js and "{12}$/" in js, name


def test_the_href_is_not_read_as_a_conversation_id():
    """It carries agent TitleIds. Measured: 24 guids in hrefs, 5 unique, all agents."""
    for name in ("_ALL_ROW_GUIDS_JS", "_CURRENT_ROW_JS"):
        assert "getAttribute('href')" not in _js(name), name
        assert ".href" not in _js(name), name


def test_a_blind_scraper_says_so_instead_of_looking_quiet():
    """"No new conversation appeared" and "I cannot see any conversation at all" both ended as
    an empty capture and the same mild log line, so a scraper that had stopped matching the
    page read as an ordinary quiet result -- for six weeks."""
    body = SOURCE[SOURCE.index("def _known_conv_guids"):]
    body = body[:body.index("\ndef ")]
    assert "if rows == 0:" in body
    assert "BLIND" in body
    assert "cannot be resumed" in body


def test_the_warning_counts_rows_and_not_matches():
    """Counting only guid-shaped matches would report blindness whenever a signed-in session
    simply had no conversations yet."""
    body = SOURCE[SOURCE.index("def _known_conv_guids"):]
    body = body[:body.index("\ndef ")]
    assert "rows += 1" in body
    i = body.index("rows += 1")
    j = body.index("if BARE_GUID_RE.match")
    assert i < j, "the row must be counted before it is filtered"
