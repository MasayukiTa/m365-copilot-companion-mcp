# -*- coding: utf-8 -*-
"""The host-log analyzer turns verbose devtunnel lines into per-channel records.

These feed a small recorded sample -- built from the exact lines the task quoted,
including BOTH close directions -- through relay.tunnel_channels and check:

* a locally-closed channel and a remotely-closed channel are told apart,
* sent/received bytes, open time and duration are read off correctly,
* the no_response and remote_closed_early flags fire exactly on their thresholds,
* append_new_records tails a growing log without re-emitting old records, and
  re-reads from the top when the log is rotated (shrinks),
* health_counts returns the three /health numbers for the last 10 minutes, and -1
  for each when the jsonl file does not exist.

Nothing here imports anything Windows-only, so it runs on the Linux CI runner.
"""
from __future__ import annotations

import json
import os
import time

from relay import tunnel_channels as tc

# A recorded sample. Two forwarded channels open; id 5 is closed by this PC (locally) after
# a normal exchange; id 7 is closed by the remote side first (a received ChannelCloseMessage,
# then a "closed remotely" summary) having sent us real bytes while we answered with none --
# the no_response / remote_closed_early shape. A session channel is present to prove non
# forwarded-tcpip channels are ignored.
SAMPLE = """\
2026-10-31 21:40:00.100 Connected to tunnel relay.
2026-10-31 21:40:01.000 SshChannel(Type: forwarded-tcpip, Id: 5, RemoteId: 5) opened.
2026-10-31 21:40:01.050 SshChannel(Type: session, Id: 1, RemoteId: 1) opened.
2026-10-31 21:40:03.500 SshChannel(Type: forwarded-tcpip, Id: 5, RemoteId: 5) closed locally. (S: 1335, R: 746)
2026-10-31 21:40:04.000 SshChannel(Type: forwarded-tcpip, Id: 7, RemoteId: 7) opened.
2026-10-31 21:40:05.200 Receiving SshChannel(Type: forwarded-tcpip, Id: 7, RemoteId: 7) ChannelCloseMessage
2026-10-31 21:40:05.300 SshChannel(Type: forwarded-tcpip, Id: 7, RemoteId: 7) closed remotely. (S: 0, R: 512)
"""


def _records():
    return list(tc.parse_channel_events(SAMPLE.splitlines()))


def test_both_close_directions_are_distinguished():
    recs = _records()
    assert [r["channel_id"] for r in recs] == [5, 7]  # session channel ignored
    by_id = {r["channel_id"]: r for r in recs}
    assert by_id[5]["closed_first"] == "host"
    assert by_id[7]["closed_first"] == "remote"


def test_bytes_and_duration_are_read_off():
    by_id = {r["channel_id"]: r for r in _records()}
    assert by_id[5]["sent"] == 1335 and by_id[5]["received"] == 746
    assert by_id[7]["sent"] == 0 and by_id[7]["received"] == 512
    # id 5 opened at 21:40:01.000, closed at 21:40:03.500 -> 2.5 s
    assert abs(by_id[5]["duration_s"] - 2.5) < 0.01


def test_flags_fire_on_the_recorded_sample():
    by_id = {r["channel_id"]: r for r in _records()}
    # id 5: we answered (sent 1335), host closed -> neither flag.
    assert by_id[5]["no_response"] is False
    assert by_id[5]["remote_closed_early"] is False
    # id 7: client sent 512 (>300), we sent 0 -> no_response; remote closed first with
    # sent < 100 -> remote_closed_early.
    assert by_id[7]["no_response"] is True
    assert by_id[7]["remote_closed_early"] is True


def test_classify_flags_thresholds_directly():
    # no_response needs received > 300 AND sent == 0.
    assert tc.classify_flags(0, 301, 10.0, False) == (True, False)
    assert tc.classify_flags(0, 300, 10.0, False)[0] is False   # 300 is not > 300
    assert tc.classify_flags(1, 500, 10.0, False)[0] is False   # sent not 0
    # remote_closed_early: remote first AND (sent < 100 OR (dur < 5 and received > 300)).
    assert tc.classify_flags(50, 10, 100.0, True)[1] is True    # sent < 100
    assert tc.classify_flags(150, 400, 2.0, True)[1] is True    # short & received > 300
    assert tc.classify_flags(150, 400, 2.0, False)[1] is False  # not remote-first
    assert tc.classify_flags(150, 400, 9.0, True)[1] is False   # long & sent >= 100


def test_append_tails_and_survives_rotation(tmp_path):
    log = tmp_path / "devtunnel-host-ts.log"
    jsonl = tmp_path / "tunnel_channels.jsonl"
    offset = tmp_path / "off"
    lines = SAMPLE.splitlines(keepends=True)
    # First half: only id 5's open+close is present.
    log.write_text("".join(lines[:4]), encoding="utf-8")
    n1 = tc.append_new_records(str(log), str(jsonl), str(offset))
    assert n1 == 1
    # Append the rest: only id 7 is new -- id 5 must not be re-emitted.
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("".join(lines[4:]))
    n2 = tc.append_new_records(str(log), str(jsonl), str(offset))
    assert n2 == 1
    got = [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [r["channel_id"] for r in got] == [5, 7]
    # Rotation: the live file shrinks (replaced by a fresh, shorter one). The tailer must
    # notice its offset is past the end and re-read from the top rather than skip everything.
    log.write_text(lines[1] + lines[3], encoding="utf-8")  # just id 5 open+close
    n3 = tc.append_new_records(str(log), str(jsonl), str(offset))
    assert n3 == 1


def test_health_counts_last_10_minutes_and_absent_file(tmp_path):
    jsonl = tmp_path / "tunnel_channels.jsonl"
    # Absent file -> -1 for each field.
    absent = tc.health_counts(str(jsonl))
    assert absent == {"tunnel_channels": -1, "tunnel_no_response": -1,
                      "tunnel_remote_closed_early": -1}
    now = time.time()
    rows = [
        {"time": now - 60, "no_response": False, "remote_closed_early": False},   # recent, clean
        {"time": now - 120, "no_response": True, "remote_closed_early": True},    # recent, both
        {"time": now - 3000, "no_response": True, "remote_closed_early": True},   # 50 min ago: out
    ]
    jsonl.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    counts = tc.health_counts(str(jsonl), now=now)
    assert counts["tunnel_channels"] == 2          # the two within 10 min
    assert counts["tunnel_no_response"] == 1
    assert counts["tunnel_remote_closed_early"] == 1


def test_a_close_without_an_open_still_yields_a_record():
    # A rotation can carry away the open line; a lone close is still a real closed channel.
    line = ("2026-10-31 22:00:00.000 SshChannel(Type: forwarded-tcpip, Id: 9, RemoteId: 9) "
            "closed locally. (S: 10, R: 20)")
    recs = list(tc.parse_channel_events([line]))
    assert len(recs) == 1
    assert recs[0]["channel_id"] == 9 and recs[0]["open_time"] is None
    assert recs[0]["duration_s"] is None
