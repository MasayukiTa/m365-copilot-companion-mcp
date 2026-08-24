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


def _known_body():
    body = SOURCE[SOURCE.index("def _known_conv_guids"):]
    return body[:body.index("\ndef ")]


def test_a_blind_scraper_says_so_instead_of_looking_quiet():
    """"No new conversation appeared" and "I cannot see any conversation at all" both ended as
    an empty capture and the same mild log line, so a scraper that had stopped matching the
    page read as an ordinary quiet result -- for six weeks."""
    body = _known_body()
    assert "if elements_with_id == 0:" in body
    assert "BLIND" in body
    assert "cannot be resumed" in body


def test_the_count_is_of_elements_and_not_of_matches():
    """The first attempt at this counted the list the JS returns -- which the JS has ALREADY
    filtered to guid-shaped ids. So a signed-in page with no conversations yet would have been
    reported as broken markup: the warning added to catch six weeks of blindness would have
    fired on the one case its own commit message promised to exclude."""
    js = _js("_ALL_ROW_GUIDS_JS")
    assert "elementsWithId: rows.length" in js, "the raw count has to come from the page"
    assert "guids: out" in js
    body = _known_body()
    assert 'res.get("elementsWithId")' in body
    assert 'res.get("guids")' in body


def test_ids_present_but_no_guid_among_them_is_not_called_blindness():
    """It is either an account with no conversations or row ids that stopped being guids, and
    this cannot tell them apart. Saying so beats picking one."""
    body = _known_body()
    assert "elif elements_with_id > 0 and not guids:" in body
    assert "logger.info" in body
    assert "no conversations yet" in body


def test_a_page_that_cannot_be_asked_is_a_third_case():
    """evaluate() raising is not the same as the page answering zero, and reporting it as
    changed markup would name the wrong cause."""
    body = _known_body()
    assert "elements_with_id = -1" in body
