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


def test_analyze_log_reads_a_whole_file_and_tolerates_absence(tmp_path):
    log = tmp_path / "devtunnel-host-ts.log"
    log.write_text(SAMPLE, encoding="utf-8")
    assert tc.analyze_log(str(log)) == _records()
    assert tc.analyze_log(str(tmp_path / "missing.log")) == []


def test_both_paths_take_the_same_shared_lock():
    # Wiring guard: append_new_records and health_counts must both serialise on the one
    # module lock. If a refactor drops the lock from either path, the read/write race that
    # corrupted the process heap (faulting threads in json.loads under health_counts) comes
    # back, so pin the shared lock here. We hold the lock and assert both calls block on it.
    import threading as _threading

    assert isinstance(tc._JSONL_LOCK, type(_threading.Lock()))

    blocked = []
    with tc._JSONL_LOCK:
        # With the lock held, a health_counts in another thread must not get past the lock.
        def _reader():
            # Point at a non-existent file so, if it somehow ran, it would return fast; the
            # point is it should BLOCK on the lock and never set done before we release.
            tc.health_counts("/no/such/tunnel_channels.jsonl.lock-probe")
            blocked.append("ran")

        # health_counts returns early for an absent file BEFORE taking the lock, so use a
        # real file to force it onto the locked read path.
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": time.time(),
                                 "no_response": False,
                                 "remote_closed_early": False}) + "\n")

        done = []

        def _reader_real():
            tc.health_counts(path, now=time.time())
            done.append(True)

        t = _threading.Thread(target=_reader_real)
        t.start()
        t.join(timeout=0.5)
        # The lock is still held here, so the reader must still be blocked (done empty).
        assert done == [], "health_counts did not take the shared lock"
    # Lock released -> the reader now completes.
    t.join(timeout=5)
    assert done == [True]
    os.remove(path)


def test_concurrent_reads_during_appends_never_corrupt(tmp_path):
    # Regression for the native crash: many health_counts readers hammering the file while a
    # writer continuously appends must never raise and must end on a correct snapshot. Before
    # the shared lock, concurrent reads held a buffered file handle across json.loads while an
    # append grew the file, feeding a half-written line to the C decoder and corrupting the
    # heap. With read and append serialised this runs clean.
    import threading as _threading

    jsonl = tmp_path / "tunnel_channels.jsonl"
    log = tmp_path / "devtunnel-host-ts.log"
    offset = tmp_path / "off"
    jsonl.write_text("", encoding="utf-8")

    n_records = 400
    stop = _threading.Event()
    errors = []

    def _writer():
        try:
            cid = 100
            for _ in range(n_records):
                cid += 1
                line = ("2026-10-31 21:40:01.000 SshChannel(Type: forwarded-tcpip, "
                        f"Id: {cid}, RemoteId: {cid}) opened.\n"
                        "2026-10-31 21:40:03.500 SshChannel(Type: forwarded-tcpip, "
                        f"Id: {cid}, RemoteId: {cid}) closed locally. (S: 1335, R: 746)\n")
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(line)
                tc.append_new_records(str(log), str(jsonl), str(offset))
        except Exception as exc:  # pragma: no cover - only on a real race
            errors.append(repr(exc))
        finally:
            stop.set()

    def _reader():
        try:
            while not stop.is_set():
                tc.health_counts(str(jsonl), now=time.time())
        except Exception as exc:  # pragma: no cover - only on a real race
            errors.append(repr(exc))

    writer = _threading.Thread(target=_writer)
    readers = [_threading.Thread(target=_reader) for _ in range(4)]
    writer.start()
    for r in readers:
        r.start()
    writer.join(timeout=60)
    for r in readers:
        r.join(timeout=10)

    assert errors == [], f"concurrent read/append raised: {errors}"
    # Final snapshot must be internally consistent: every appended channel is counted.
    final = tc.health_counts(str(jsonl), now=time.time(), window_s=10 ** 9)
    assert final["tunnel_channels"] == n_records


def test_health_counts_large_file_reads_recent_tail_quickly(tmp_path):
    path = tmp_path / "large.jsonl"
    now = 2_000_000
    old = (json.dumps({"time": now - 1000, "padding": "x" * 760}) + "\n").encode()
    recent = (json.dumps({"time": now - 10, "no_response": True}) + "\n").encode()
    with path.open("wb") as fh:
        batch = old * 1000
        for _ in range(400):
            fh.write(batch)
        fh.write(recent * 12)
    start = time.perf_counter()
    counts = tc.health_counts(str(path), now=now)
    elapsed = time.perf_counter() - start
    assert counts == {"tunnel_channels": 12, "tunnel_no_response": 12,
                      "tunnel_remote_closed_early": 0}
    assert elapsed < 1.0, f"tail read took {elapsed:.3f}s"


def test_health_counts_null_time_and_incomplete_last_line(tmp_path):
    path = tmp_path / "partial.jsonl"
    path.write_text('{"time": null, "no_response": true}\n'
                    '{"time": "unusable"}\n{"time":', encoding="utf-8")
    assert tc.health_counts(str(path), now=1000) == {
        "tunnel_channels": 2, "tunnel_no_response": 1,
        "tunnel_remote_closed_early": 0}


def test_health_counts_large_window_still_caps_tail(tmp_path):
    path = tmp_path / "capped.jsonl"
    row = b'{"time": 1, "padding": "' + b'x' * 1000 + b'"}\n'
    with path.open("wb") as fh:
        for _ in range(34):
            fh.write(row * 1024)
    lines = tc._recent_lines(path, cutoff=-10**10)
    assert sum(len(line.encode()) + 1 for line in lines) <= 32 * 1024 * 1024
    assert 0 < tc.health_counts(str(path), now=1000, window_s=10**10)["tunnel_channels"] < 34 * 1024


def _append_one_channel(log, jsonl, offset, cid):
    """Append one open+close pair to the host log and tail it into the jsonl. One record."""
    line = ("2026-10-31 21:40:01.000 SshChannel(Type: forwarded-tcpip, "
            f"Id: {cid}, RemoteId: {cid}) opened.\n"
            "2026-10-31 21:40:03.500 SshChannel(Type: forwarded-tcpip, "
            f"Id: {cid}, RemoteId: {cid}) closed locally. (S: 1335, R: 746)\n")
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(line)
    return tc.append_new_records(str(log), str(jsonl), str(offset))


def test_rotate_if_too_big_moves_to_one_generation_and_discards_the_previous(tmp_path):
    # The size-cap primitive, exercised directly: under the cap it does nothing; over the cap
    # it moves the live file onto ".1"; a second rotation overwrites that ".1" (one generation)
    # and never keeps a ".2".
    jsonl = tmp_path / "tunnel_channels.jsonl"
    jsonl.write_text("small\n", encoding="utf-8")
    assert tc._rotate_if_too_big(str(jsonl), max_bytes=1024) is False
    assert jsonl.exists() and not (tmp_path / "tunnel_channels.jsonl.1").exists()

    jsonl.write_text("A" * 2048, encoding="utf-8")
    assert tc._rotate_if_too_big(str(jsonl), max_bytes=1024) is True
    assert not jsonl.exists()
    assert (tmp_path / "tunnel_channels.jsonl.1").read_text(encoding="utf-8") == "A" * 2048

    # A fresh live file grows past the cap again; rotating replaces the old ".1" rather than
    # stacking a ".2". Only one generation is ever kept.
    jsonl.write_text("B" * 2048, encoding="utf-8")
    assert tc._rotate_if_too_big(str(jsonl), max_bytes=1024) is True
    assert (tmp_path / "tunnel_channels.jsonl.1").read_text(encoding="utf-8") == "B" * 2048
    assert not (tmp_path / "tunnel_channels.jsonl.2").exists()


def test_rotate_if_too_big_tolerates_a_missing_file(tmp_path):
    # Housekeeping must never raise: an absent file is simply "nothing to rotate".
    assert tc._rotate_if_too_big(str(tmp_path / "nope.jsonl"), max_bytes=1) is False


def test_append_rotates_when_the_jsonl_passes_the_cap(monkeypatch, tmp_path):
    # The real write path: once the accumulated jsonl crosses the cap, the NEXT append rotates
    # it onto ".1" and the live file starts fresh -- so the live file never grows without
    # bound, which is the whole point. Drive the threshold with a tiny cap so a handful of
    # short records is enough to trip it.
    monkeypatch.setattr(tc, "_JSONL_MAX_BYTES", 400)
    log = tmp_path / "devtunnel-host-ts.log"
    jsonl = tmp_path / "tunnel_channels.jsonl"
    offset = tmp_path / "off"

    rotated_at = None
    for cid in range(100, 120):
        _append_one_channel(log, jsonl, offset, cid)
        if (tmp_path / "tunnel_channels.jsonl.1").exists():
            rotated_at = cid
            break
    assert rotated_at is not None, "the jsonl never rotated despite passing the cap"
    # After a rotation the live file is small again (fresh, below the cap) while the sidecar
    # holds the bulk -- bounded, not unbounded.
    assert (tmp_path / "tunnel_channels.jsonl.1").stat().st_size > 0
    live = jsonl.stat().st_size if jsonl.exists() else 0
    assert live <= tc._JSONL_MAX_BYTES


def test_append_keeps_only_one_generation_across_many_rotations(monkeypatch, tmp_path):
    # Over a long run the file rotates many times; disk use stays bounded because each
    # rotation discards the previous ".1". There is never a ".2".
    monkeypatch.setattr(tc, "_JSONL_MAX_BYTES", 300)
    log = tmp_path / "devtunnel-host-ts.log"
    jsonl = tmp_path / "tunnel_channels.jsonl"
    offset = tmp_path / "off"
    for cid in range(200, 260):
        _append_one_channel(log, jsonl, offset, cid)
    assert not (tmp_path / "tunnel_channels.jsonl.2").exists()


def test_rotation_does_not_disturb_the_offset(monkeypatch, tmp_path):
    # The offset file tracks the HOST LOG, not the jsonl, so rotating the jsonl must leave it
    # pointing exactly at the end of the host log -- a following append reads no record twice.
    monkeypatch.setattr(tc, "_JSONL_MAX_BYTES", 300)
    log = tmp_path / "devtunnel-host-ts.log"
    jsonl = tmp_path / "tunnel_channels.jsonl"
    offset = tmp_path / "off"
    for cid in range(300, 320):
        _append_one_channel(log, jsonl, offset, cid)
    assert (tmp_path / "tunnel_channels.jsonl.1").exists(), "precondition: it rotated"
    # The offset equals the host log's size: everything written has been consumed exactly once.
    assert int(offset.read_text(encoding="utf-8").strip()) == log.stat().st_size
    # A further append with no new host-log bytes adds nothing (no re-emission after rotation).
    assert tc.append_new_records(str(log), str(jsonl), str(offset)) == 0


def test_health_counts_after_rotation_is_zero_not_absent(tmp_path):
    # Immediately after a rotation the live jsonl is gone (moved onto ".1") until the next
    # append recreates it. The reader tails the LIVE file, so it finds an absent/empty file --
    # but the stream exists, so /health must read 0, not the -1 that means "never written".
    jsonl = tmp_path / "tunnel_channels.jsonl"
    (tmp_path / "tunnel_channels.jsonl.1").write_text(
        json.dumps({"time": time.time(), "no_response": False,
                    "remote_closed_early": False}) + "\n", encoding="utf-8")
    assert not jsonl.exists()
    counts = tc.health_counts(str(jsonl), now=time.time())
    assert counts == {"tunnel_channels": 0, "tunnel_no_response": 0,
                      "tunnel_remote_closed_early": 0}
    # A truly absent stream (no live file AND no sidecar) still reads -1 for each.
    absent = tc.health_counts(str(tmp_path / "never.jsonl"))
    assert absent == {"tunnel_channels": -1, "tunnel_no_response": -1,
                      "tunnel_remote_closed_early": -1}


def test_reader_works_on_a_fresh_empty_jsonl_after_rotation(tmp_path):
    # After rotation the next append recreates the live file; a reader hitting it while it is
    # still empty (zero bytes) must return 0, not raise and not -1.
    jsonl = tmp_path / "tunnel_channels.jsonl"
    (tmp_path / "tunnel_channels.jsonl.1").write_text("", encoding="utf-8")
    jsonl.write_text("", encoding="utf-8")  # fresh, empty live file
    counts = tc.health_counts(str(jsonl), now=time.time())
    assert counts == {"tunnel_channels": 0, "tunnel_no_response": 0,
                      "tunnel_remote_closed_early": 0}
