# -*- coding: utf-8 -*-
"""One Copilot conversation may span many fleet worker transcript files.

A follow-up reuses the same conversation GUID.  The registry used to update its single
``transcript`` pointer to the newest worker, which made the pre-follow-up conversation disappear
from the main-chat navigation.  ``transcript`` remains the latest pointer for compatibility, but
``transcripts`` is the ordered lineage and is never destructive.
"""
from relay.fleet_runner import merge_conv_rows
from bridge.copilot_bridge import merge_fleet_conversations


def _fleet(tr, url="sess:guid-1"):
    return {"url": url, "source": "fleet", "name": "w0", "transcript": tr,
            "title": "task", "goal": "full goal", "ts": 1.0}


def test_fleet_registry_same_guid_keeps_old_and_new_transcripts():
    rows, changed = merge_conv_rows([_fleet("old.jsonl")], [_fleet("new.jsonl")], now=2.0)
    assert changed is True
    assert len(rows) == 1
    row = rows[0]
    assert row["transcript"] == "new.jsonl"
    assert row["transcripts"] == ["old.jsonl", "new.jsonl"]


def test_fleet_registry_chain_is_idempotent_and_extends_in_order():
    existing = [_fleet("new.jsonl")]
    existing[0]["transcripts"] = ["old.jsonl", "new.jsonl"]
    rows, changed = merge_conv_rows(existing, [_fleet("new.jsonl")], now=3.0)
    assert rows[0]["transcripts"] == ["old.jsonl", "new.jsonl"]
    assert changed is False

    rows, changed = merge_conv_rows(rows, [_fleet("third.jsonl")], now=4.0)
    assert rows[0]["transcripts"] == ["old.jsonl", "new.jsonl", "third.jsonl"]
    assert rows[0]["transcript"] == "third.jsonl"
    assert changed is True


def test_bridge_registry_merge_cannot_erase_a_fleet_lineage_on_same_url():
    existing = [_fleet("fleet-old.jsonl", url="https://m365/x/conversation/abc")]
    existing[0]["transcripts"] = ["fleet-older.jsonl", "fleet-old.jsonl"]
    incoming = [{"url": "https://m365/x/conversation/abc", "source": "chat", "name": "sid-1",
                 "transcript": "chat-latest.jsonl", "title": "same conversation", "ts": 5.0}]
    rows = merge_fleet_conversations(existing, incoming)
    assert len(rows) == 1
    assert rows[0]["transcript"] == "chat-latest.jsonl"
    assert rows[0]["transcripts"] == ["fleet-older.jsonl", "fleet-old.jsonl", "chat-latest.jsonl"]


def test_new_rows_start_a_lineage_even_before_the_second_segment_exists():
    rows, changed = merge_conv_rows([], [_fleet("first.jsonl")], now=1.0)
    assert changed is True
    assert rows[0]["transcripts"] == ["first.jsonl"]
    b = merge_fleet_conversations([], [{"url": "", "source": "chat", "name": "sid",
                                        "transcript": "chat.jsonl", "title": "t", "ts": 1.0}])
    assert b[0]["transcripts"] == ["chat.jsonl"]


def test_legacy_single_pointer_rows_migrate_once():
    legacy = _fleet("only.jsonl")
    rows, changed = merge_conv_rows([legacy], [_fleet("only.jsonl")], now=2.0)
    assert changed is True
    assert rows[0]["transcripts"] == ["only.jsonl"]
    rows2, changed2 = merge_conv_rows(rows, [_fleet("only.jsonl")], now=3.0)
    assert changed2 is False

    b = merge_fleet_conversations([legacy], [_fleet("only.jsonl")])
    assert b[0]["transcripts"] == ["only.jsonl"]
