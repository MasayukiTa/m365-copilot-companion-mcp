# -*- coding: utf-8 -*-
"""SyncRegistry() required a non-empty conv_url to add ANY fleet row -- but a socket-driven
worker's conv_url is permanently empty by construction (relay_fleet.py's _capture_url only
ever fires when the worker holds a browser `page`, and a socket worker's page is None;
MCP_FLEET_SOCKET has defaulted on since 2026-08-21). SyncRegistry is the ONLY live/periodic
update path for the fleet sidebar -- DiscoverTranscripts only runs once, at startup, capped at
80 entries -- so this silently froze the fleet section at whatever the last app launch found
and it never grew again for any conversation created afterward.

Measured against this machine's real .fleet/conversations.json (2026-09-09): 1064/1064 fleet
rows had an empty url; 718 of those had a transcript file still present on disk. The fix:
accept a row when EITHER url or transcript is present (a transcript-only row is not degraded --
the click handler already reads c.Transcript from disk BEFORE it ever needs ConvUrl, see the
"a registry/fleet conversation we haven't loaded yet" branch), with dedup keyed on whichever
identity the row actually has.

Source-level checks: CopilotChat.cs is C# and has no test harness here, matching
ui/test_copilot_chat_conv_lifecycle.py and ui/test_fleet_cockpit_approval_center.py.

Run: pytest -q ui/test_sync_registry_accepts_socket_workers.py
"""
from pathlib import Path

SOURCE = Path(__file__).with_name("CopilotChat.cs").read_text(encoding="utf-8")


def _sync_registry_body():
    start = SOURCE.index("void SyncRegistry()")
    body = SOURCE[start:]
    return body[:body.index("readonly JavaScriptSerializer _cjs")]


def test_a_row_with_no_url_but_a_transcript_is_no_longer_skipped():
    body = _sync_registry_body()
    # the old defect: any row with an empty url was `continue`d before ever looking at
    # transcript -- pin that this exact single-condition skip is gone.
    assert 'if (string.IsNullOrEmpty(url)) continue;' not in body
    assert 'if (string.IsNullOrEmpty(url) && string.IsNullOrEmpty(transcript)) continue;' in body


def test_a_row_with_neither_url_nor_transcript_is_still_skipped():
    """The fix widens the gate, it does not remove it -- a row with nothing to show or open
    must still be dropped, same as before."""
    body = _sync_registry_body()
    assert 'string.IsNullOrEmpty(url) && string.IsNullOrEmpty(transcript)' in body


def test_dedup_falls_back_to_transcript_when_url_is_absent():
    """Without this, every transcript-only row added on one SyncRegistry pass would look
    "not yet present" on the next pass (since the old dedup compared ConvUrl, which is "" for
    all of them) and get re-inserted at position 0 every single poll -- the list would grow
    without bound and the same conversation would appear over and over, newest-first, forever."""
    body = _sync_registry_body()
    assert 'c.Transcript == transcript' in body


def test_the_transcript_field_is_still_captured_on_the_new_conversation():
    """The existing "disk jsonl -> open from disk, no scrape" comment documents WHY Transcript
    matters; the fix must still set it, not just gate on it."""
    body = _sync_registry_body()
    assert 'c.Transcript = transcript;' in body
    assert 'disk jsonl -> open from disk, no scrape' in body


def test_url_only_dedup_is_unaffected_when_url_is_present():
    """The pre-existing behaviour for a genuine conv_url row (the ordinary, non-socket path)
    must be untouched: dedup by ConvUrl still applies whenever url is non-empty."""
    body = _sync_registry_body()
    assert "!string.IsNullOrEmpty(url) && c.ConvUrl == url" in body
