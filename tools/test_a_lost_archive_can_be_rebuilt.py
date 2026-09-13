# -*- coding: utf-8 -*-
"""`.fleet/history.json` went missing, and with it the ability to reopen any past conversation.

READ AS A BENCH LEDGER, history.json is "the outcome index" and its loss is a reporting gap.
That is the wrong reading. Each row carries `conv_url`, and `conv_url` is the field the chat
window keys its send target, its steer mode and its snapshot refresh on -- so with the file gone,
**every conversation the fleet had ever run became unaddressable**. Typing a follow-up answered
「この会話の送信先を特定できません」, advice that cannot work because reopening supplies nothing.

WHAT SURVIVED, MEASURED 2026-09-13:

    .fleet/socket_route.jsonl   4,280 rows; 3,469 `worker_done` carrying conv_client (the
                                Copilot conversationId), goal, outcome, turns, ts.
                                Span 2026-08-25 -> 2026-09-13. Append-only, never pruned.
    .fleet/transcripts/         1,557 files, each with its goal. Retention prunes these, which
                                is why the socket ledger has more conversations than there are
                                transcripts -- and why it, not they, is the spine.

Rebuilt: **3,469 rows**, every one carrying the conversation id, 1,453 with a transcript still
on disk.

AND THE ID IS DELIBERATELY NOT IN `conv_url`. Verifying the GUI rather than the data -- which is
the whole point of the exercise -- showed that BOTH of the cockpit's resume paths open a page:

    CopilotChat send, target has a ConvUrl  -> GET /switch?url=...
    bridge _do_switch                       -> release_socket_driver("/switch"); _goto_settled(url)
    bridge _do_resume -> _resume_to_ref     -> PAGE.goto(AGENT_URL); click the sidebar row

`/switch` RELEASES the websocket and navigates a tab. So filling `conv_url` would not have
restored resumption -- it would have turned 3,469 archived rows into 3,469 invitations to open
one. The id goes in `resume_guid`, which nothing routes on, and `--conv-url` is the opt-in for
after a socket path exists in the UI.

THE SOCKET RESUME ITSELF IS REAL AND MEASURED. `relay/socket_route.driver_for(conversation_id=)`
continues a conversation over the websocket, verified 2026-08-24 by planting a passphrase in one
process and recovering it verbatim in another that had only the id (the control arm, an unused
id, answered "I do not know"). What is missing is a GUI path that reaches it.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import rebuild_history as RH  # noqa: E402


def _route(tmp_path, rows):
    p = tmp_path / "socket_route.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                 encoding="utf-8")
    return str(p)


def _transcript(tmp_path, name, goal, ts=1000.0):
    d = tmp_path / "transcripts"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(json.dumps({"meta": True, "goal": goal, "ts": ts}, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    return str(d)


DONE_ROW = {"event": "worker_done", "worker": "w0", "goal": "ある用事",
            "conv_client": "11111111-2222-3333-4444-555555555555",
            "conv_server": "11111111-2222-3333-4444-555555555555",
            "ts": 1000.0, "turns": 3, "outcome": "DONE", "status": "done"}


# ── the field the loss was actually about ─────────────────────────────────────────────────

def test_every_row_carries_a_reference_that_can_be_reopened(tmp_path):
    """THE POINT. Not the outcome -- the way back to the conversation."""
    rows = RH.build(_route(tmp_path, [DONE_ROW]), str(tmp_path / "none"))
    assert len(rows) == 1
    # THE ID IS RECOVERED; conv_url STAYS EMPTY. Filling it routes the cockpit onto
    # /switch, which releases the socket and navigates a tab. See the comment at that key.
    assert rows[0]["resume_guid"] == "11111111-2222-3333-4444-555555555555"
    assert rows[0]["conv_url"] == ""
    optin = RH.build(_route(tmp_path, [DONE_ROW]), str(tmp_path / "none"), conv_url=True)
    assert optin[0]["conv_url"] == "sess:11111111-2222-3333-4444-555555555555"


def test_a_row_with_no_conversation_id_is_dropped(tmp_path):
    """A history row that cannot be reopened and carries no id is a line that does nothing."""
    bare = dict(DONE_ROW)
    bare.pop("conv_client")
    bare.pop("conv_server")
    assert RH.build(_route(tmp_path, [bare]), str(tmp_path / "none")) == []


def test_only_finished_workers_are_archived(tmp_path):
    """socket_route also logs retries, fallbacks and refusals. Those are events in a
    conversation's life, not conversations to file."""
    noise = [dict(DONE_ROW, event=e) for e in
             ("socket_retry", "fallback", "resend_refused", "route_closed")]
    assert RH.build(_route(tmp_path, noise + [DONE_ROW]), str(tmp_path / "none")) != []
    assert len(RH.build(_route(tmp_path, noise + [DONE_ROW]), str(tmp_path / "none"))) == 1


# ── the shape the cockpit reads ───────────────────────────────────────────────────────────

def test_the_row_has_the_fields_the_cockpit_loads(tmp_path):
    """LoadHistory reads `key` into _archivedKeys and the rest into the row. A missing field is
    a row that renders blank, which looks like a lost record rather than a rebuild gap."""
    r = RH.build(_route(tmp_path, [DONE_ROW]), str(tmp_path / "none"))[0]
    for field in ("key", "goal", "status", "conv_title", "outcome", "conv_url",
                  "transcript", "name", "turn", "seq", "ts", "phase_events", "run_started"):
        assert field in r, field
    assert r["name"] == "w0" and r["turn"] == 3 and r["outcome"] == "DONE"


def test_keys_are_unique_so_nothing_reads_as_already_archived(tmp_path):
    two = [DONE_ROW, dict(DONE_ROW, ts=1001.0, worker="w1")]
    rows = RH.build(_route(tmp_path, two), str(tmp_path / "none"))
    assert len({r["key"] for r in rows}) == len(rows) == 2


def test_rows_come_out_in_time_order_and_numbered(tmp_path):
    three = [dict(DONE_ROW, ts=t, worker="w%d" % i)
             for i, t in enumerate((3000.0, 1000.0, 2000.0))]
    rows = RH.build(_route(tmp_path, three), str(tmp_path / "none"))
    assert [r["ts"] for r in rows] == [1000.0, 2000.0, 3000.0]
    assert [r["seq"] for r in rows] == [0, 1, 2]


# ── the transcript link, which must be right or blank ─────────────────────────────────────

def test_a_transcript_is_linked_when_the_goal_and_worker_and_time_agree(tmp_path):
    d = _transcript(tmp_path, "rAAA_a0_w0.jsonl", "ある用事", ts=1000.0)
    r = RH.build(_route(tmp_path, [DONE_ROW]), d)[0]
    assert r["transcript"].endswith("rAAA_a0_w0.jsonl")


def test_another_workers_transcript_is_not_borrowed(tmp_path):
    """THE FIRST VERSION DID THIS. Keyed on the goal alone, a goal run by seven workers put the
    same file on all seven rows -- 2,077 rows claiming a transcript out of 1,557 in existence."""
    d = _transcript(tmp_path, "rAAA_a0_w3.jsonl", "ある用事", ts=1000.0)
    r = RH.build(_route(tmp_path, [DONE_ROW]), d)[0]
    assert r["transcript"] == "", r["transcript"]


def test_the_same_goal_rerun_much_later_is_not_linked(tmp_path):
    """A blank is honest; a path to another run's transcript is a false record."""
    d = _transcript(tmp_path, "rAAA_a0_w0.jsonl", "ある用事", ts=1000.0)
    late = dict(DONE_ROW, ts=1000.0 + RH.TRANSCRIPT_MATCH_WINDOW_S + 60)
    assert RH.build(_route(tmp_path, [late]), d)[0]["transcript"] == ""


# ── writing it ────────────────────────────────────────────────────────────────────────────

def test_it_refuses_to_clobber_an_existing_archive(tmp_path, capsys):
    out = tmp_path / "history.json"
    out.write_text("[]", encoding="utf-8")
    assert RH.main(["--out", str(out)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().out
    assert out.read_text(encoding="utf-8") == "[]"


def test_the_file_it_writes_has_no_bom(tmp_path):
    """The cockpit writes this with `new UTF8Encoding(false)` and reads it with a plain UTF-8
    decode. A BOM would become the first character of the first key."""
    out = tmp_path / "history.json"
    RH.main(["--out", str(out), "--force"])
    assert out.read_bytes()[:3] != b"\xef\xbb\xbf"
    assert isinstance(json.loads(out.read_text(encoding="utf-8")), list)


def test_the_live_archive_is_loadable_and_resumable():
    """Against the real file, because a rebuild that only works on fixtures is a fixture.

    TWO WRITERS, ONE FILE, AND THIS TEST KNEW ABOUT ONE. It was written when .fleet/history.json
    was entirely rebuild output, so it asserted the rebuild's field name -- `resume_guid` -- and
    that `conv_url` was empty on every row. The cockpit then archived 48 finished workers of its
    own, which carry the id in `conv_url` as `sess:<guid>` and have no `resume_guid` at all, and
    the test failed with "48 of 48 rows carry no conversation id" about a file in which every
    row was resumable. A spelling was being checked where a property was meant.

    THE PROPERTY: a row can be reopened, and reopening it does not open a tab. Both shapes
    satisfy the first. For the second the thing to exclude is a PAGE url, not a populated field:
    ui/CopilotChat.cs:4090 says in as many words that a `sess:<guid>` is not a url and must not
    go to /switch, and bridge/test_a_resume_does_not_open_a_page.py holds that path down.
    """
    p = os.path.join(REPO, ".fleet", "history.json")
    if not os.path.isfile(p):
        pytest.skip("no history.json on this machine")
    rows = json.load(io.open(p, encoding="utf-8"))
    assert isinstance(rows, list) and rows

    def _ref(r):
        return (r.get("resume_guid") or "").strip() or (r.get("conv_url") or "").strip()

    unreopenable = [r for r in rows if not _ref(r)]
    assert not unreopenable, "%d of %d rows carry no conversation id" % (
        len(unreopenable), len(rows))
    # AND NONE OF THEM ROUTES TO THE PAGE. A conv_url that is a real url sends the cockpit
    # through /switch, which releases the socket and opens a tab.
    routed = [r for r in rows if str(r.get("conv_url") or "").startswith("http")]
    assert not routed, "%d rows would resume through /switch (a page path)" % len(routed)
