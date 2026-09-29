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
accept a row when url, the legacy latest transcript pointer, OR the ordered transcript lineage is
present. A transcript-only row is not degraded: the click handler reads disk transcripts before
it needs ConvUrl. Socket rows without a URL now use the same (source,name) registry identity,
and the legacy single Transcript pointer is derived from the newest lineage segment.

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
    assert 'if (string.IsNullOrEmpty(url) && string.IsNullOrEmpty(transcript) && regLineage.Count == 0) continue;' in body


def test_a_row_with_no_url_or_latest_pointer_is_still_accepted_when_lineage_exists():
    body = _sync_registry_body()
    assert 'var regLineage = RegistryTranscriptLineage(d)' in body
    assert 'string.IsNullOrEmpty(url) && string.IsNullOrEmpty(transcript) && regLineage.Count == 0' in body


def test_a_row_with_no_url_no_latest_pointer_and_no_lineage_is_still_skipped():
    """The widened gate still drops a registry row with no usable conversation identity."""
    body = _sync_registry_body()
    assert 'string.IsNullOrEmpty(url) && string.IsNullOrEmpty(transcript) && regLineage.Count == 0' in body


def test_dedup_falls_back_to_transcript_when_url_is_absent():
    """Without this, every transcript-only row added on one SyncRegistry pass would look
    "not yet present" on the next pass (since the old dedup compared ConvUrl, which is "" for
    all of them) and get re-inserted at position 0 every single poll -- the list would grow
    without bound and the same conversation would appear over and over, newest-first, forever."""
    body = _sync_registry_body()
    assert 'c.Transcript == transcript' in body


def test_the_latest_transcript_pointer_is_derived_from_the_new_conversations_lineage():
    """Transcript remains the compatibility/latest pointer; Transcripts is the full identity."""
    body = _sync_registry_body()
    assert 'c.Transcripts = regLineage;' in body
    assert 'c.Transcript = FleetConvIdentity.LatestTranscript(c.Transcripts, transcript);' in body


def test_url_only_dedup_is_unaffected_when_url_is_present():
    """The pre-existing behaviour for a genuine conv_url row (the ordinary, non-socket path)
    must be untouched: dedup by ConvUrl still applies whenever url is non-empty."""
    body = _sync_registry_body()
    assert "!string.IsNullOrEmpty(url) && c.ConvUrl == url" in body
