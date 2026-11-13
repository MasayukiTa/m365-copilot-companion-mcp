# -*- coding: utf-8 -*-
"""Turn the Dev Tunnel host's verbose log into one JSON record per closed channel.

The host started with ``devtunnel -v host <name>`` prints, for every forwarded
channel, an open line and a close line. A close looks like::

    SshChannel(Type: forwarded-tcpip, Id: 5, RemoteId: 5) closed locally. (S: 1335, R: 746)

``S`` is the bytes this PC sent back to the client, ``R`` the bytes it received.
``closed locally`` means the host (this PC) sent the close first; a line that says
``closed remotely`` -- or a preceding ``Receiving ... ChannelCloseMessage`` for that
channel id -- means the remote (client) side closed first. That direction is the only
record of whether the PC end actually answered a request.

This module does two things, both pure enough to test without a tunnel:

* :func:`parse_channel_events` / :func:`analyze_log` read the ts-stamped log that
  :mod:`relay.tunnel_host_log` writes and yield one dict per *closed* channel:
  time, channel id, sent/received bytes, who closed first, open time and duration,
  plus the two flags ``no_response`` and ``remote_closed_early``.
* :func:`append_new_records` tails the log (remembering its offset) and appends those
  dicts, one JSON object per line, to ``.setup/logs/tunnel_channels.jsonl``.
* :func:`health_counts` reads that jsonl and returns, for the last N minutes, the
  counts the server /health route surfaces: total closed channels, ``no_response``
  and ``remote_closed_early``. Returns -1 for each when the file is absent, which is
  what /health shows then (see main.py).

Nothing here imports anything Windows-only, so the CI hermetic suite runs it on Linux.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

# One process-wide lock serialises every read of and append to the tunnel_channels.jsonl
# file. The /health route runs _tunnel_channels_summary -> health_counts on a dedicated
# ThreadPoolExecutor and is polled about once a second, so several health_counts reads can
# run at once while the tunnel-host-log tailer calls append_new_records to grow the same
# file. health_counts used to iterate the open file line by line, holding a buffered read
# across many json.loads calls, so an append that extended the file mid-read fed a partial
# byte sequence to the C json decoder and corrupted the process heap -- the faulthandler
# dumps show every faulting thread in json.loads under health_counts, and the crashes were
# access violations at varying offsets in python310.dll plus a 0xc0000374 heap corruption
# in ntdll. Serialising read against append, and reading the whole file in one shot under
# the lock (never holding a changing file handle across decodes), removes the data race.
# Each record is one short line, so the lock is held only briefly and /health stays fast.
_JSONL_LOCK = threading.Lock()

# The jsonl grows about 400 bytes a minute with no ceiling, so left alone it reached 257 MB
# -- big enough that the new BIGLOG watcher warned at 50 MB, and big enough that the old
# whole-file read made /health slow before PR 510 switched the reader to a bounded tail.
# When an append would push the file past this size, the current file is rotated to a single
# ".1" sidecar (the previous ".1" is discarded -- one generation only) and a fresh empty
# jsonl starts. The reader tails the live file, so after a rotation it simply sees an empty
# or absent file until the next append; health_counts treats that as zero, not as "absent".
# The rotation only moves the jsonl; the offset file tracks the HOST LOG, not the jsonl, so
# rotating the jsonl cannot disturb offset bookkeeping.
_JSONL_MAX_BYTES = 20 * 1024 * 1024

# A ts-stamped line written by relay.tunnel_host_log looks like:
#   2026-10-31 21:40:12.345 <the devtunnel line>
# The leading stamp is optional here: a raw devtunnel line (no stamp) still parses, so a
# recorded sample taken straight from the CLI works in tests without re-stamping.
_TS = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s+(?P<rest>.*)$"
)

# "SshChannel(Type: forwarded-tcpip, Id: 5, RemoteId: 5) closed locally. (S: 1335, R: 746)"
# or "... closed remotely. (S: 0, R: 512)". The "forwarded-tcpip" type is the one that
# carries a request; other channel types (session, direct-tcpip) are ignored.
_OPEN = re.compile(
    r"SshChannel\(Type:\s*forwarded-tcpip,\s*Id:\s*(?P<id>\d+)\b[^)]*\)\s*opened",
    re.IGNORECASE,
)
_CLOSE = re.compile(
    r"SshChannel\(Type:\s*forwarded-tcpip,\s*Id:\s*(?P<id>\d+)\b[^)]*\)\s*closed\s+"
    r"(?P<dir>locally|remotely)\.?\s*\(S:\s*(?P<s>\d+),\s*R:\s*(?P<r>\d+)\)",
    re.IGNORECASE,
)
# "Receiving SshChannel ... ChannelCloseMessage" for a channel id tells us the remote side
# sent the close first, even when the summarising "closed" line later says "locally" because
# this end completed the teardown. Captured per id and consulted when the close is seen.
_RECV_CLOSE = re.compile(
    r"Receiving\b.*?\bId:\s*(?P<id>\d+)\b.*?ChannelCloseMessage",
    re.IGNORECASE,
)

def _parse_ts(text):
    """Epoch seconds (with sub-second precision) for a stamp this module writes, or None.

    time.strptime builds a struct_time that has no fractional field, so time.mktime would
    truncate '...01.500' to whole seconds and a 2.5 s channel would read as 2.0. We parse
    the fractional part separately and add it back, so a duration keeps its milliseconds.
    """
    if not text:
        return None
    frac = 0.0
    core = text
    if "." in text:
        core, dot, rest = text.partition(".")
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            frac = int(digits) / (10 ** len(digits))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(core, fmt)) + frac
        except (ValueError, OverflowError):
            continue
    return None


def _split_stamp(line):
    """(epoch_or_None, rest_of_line). A line with no recognised stamp returns (None, line)."""
    m = _TS.match(line)
    if not m:
        return None, line
    return _parse_ts(m.group("ts")), m.group("rest")


def classify_flags(sent, received, duration, remote_first):
    """The two flags, kept pure so the thresholds are tested directly.

    * ``no_response``: the client sent us something (received > 300) and we sent nothing
      back (sent == 0) -- the PC end did not answer.
    * ``remote_closed_early``: the remote side closed first AND it looks abandoned --
      either we had sent almost nothing (sent < 100) or the whole channel was shorter
      than 5 s while the client had sent real bytes (received > 300).
    """
    no_response = received > 300 and sent == 0
    remote_closed_early = bool(remote_first) and (
        sent < 100 or (duration is not None and duration < 5.0 and received > 300)
    )
    return no_response, remote_closed_early


def parse_channel_events(lines):
    """Yield one record dict per *closed* forwarded-tcpip channel, in close order.

    ``lines`` is any iterable of log lines (stamped or raw). A close with no matching
    open still yields a record (open_time/duration None): the log may have rotated away
    the open, and a closed channel we can only partly describe still matters.
    """
    opened = {}          # id -> (epoch_or_None)
    remote_closed = {}   # id -> True once a ChannelCloseMessage was received for it
    for raw in lines:
        line = raw.rstrip("\n")
        epoch, rest = _split_stamp(line)

        mr = _RECV_CLOSE.search(rest)
        if mr:
            remote_closed[mr.group("id")] = True

        mo = _OPEN.search(rest)
        if mo:
            opened[mo.group("id")] = epoch
            continue

        mc = _CLOSE.search(rest)
        if not mc:
            continue
        cid = mc.group("id")
        sent = int(mc.group("s"))
        received = int(mc.group("r"))
        direction = mc.group("dir").lower()
        # "closed remotely" OR a prior received ChannelCloseMessage => remote closed first.
        remote_first = (direction == "remotely") or remote_closed.get(cid, False)
        open_epoch = opened.pop(cid, None)
        duration = None
        if open_epoch is not None and epoch is not None:
            duration = max(0.0, epoch - open_epoch)
        no_response, remote_early = classify_flags(sent, received, duration, remote_first)
        rec = {
            "time": epoch,
            "channel_id": int(cid),
            "sent": sent,
            "received": received,
            "closed_first": "remote" if remote_first else "host",
            "open_time": open_epoch,
            "duration_s": duration,
            "no_response": no_response,
            "remote_closed_early": remote_early,
        }
        remote_closed.pop(cid, None)
        yield rec


def analyze_log(path):
    """Read a whole ts log file and return the list of close records. [] if absent."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return list(parse_channel_events(fh))
    except FileNotFoundError:
        return []


def append_new_records(log_path, jsonl_path, offset_path=None):
    """Tail ``log_path`` from a remembered byte offset, append new close records to
    ``jsonl_path`` (one JSON object per line), and persist the new offset.

    Returns the number of records appended. Safe to call repeatedly; it only reads the
    bytes added since last time. A log that shrank (rotation replaced it) is re-read from
    the start. Never raises on a missing log -- returns 0.
    """
    if offset_path is None:
        offset_path = jsonl_path + ".offset"
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return 0
    start = 0
    try:
        with open(offset_path, "r", encoding="utf-8") as fh:
            start = int((fh.read() or "0").strip() or "0")
    except (OSError, ValueError):
        start = 0
    if start > size:
        start = 0  # the file was rotated/truncated under us; re-read from the top.
    appended = 0
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(start)
        chunk = fh.readlines()
        new_offset = fh.tell()
    if chunk:
        # Serialise the append against every concurrent health_counts read of the same file.
        with _JSONL_LOCK:
            with open(jsonl_path, "a", encoding="utf-8") as out:
                for rec in parse_channel_events(chunk):
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    appended += 1
            # Rotate AFTER the append, under the same lock a reader takes, so a size check and
            # the os.replace that acts on it cannot straddle a concurrent read of the file.
            _rotate_if_too_big(jsonl_path)
    with open(offset_path, "w", encoding="utf-8") as fh:
        fh.write(str(new_offset))
    return appended


def _rotate_if_too_big(jsonl_path, max_bytes=None):
    """If ``jsonl_path`` is over ``max_bytes``, move it onto a single ``.1`` sidecar.

    ``max_bytes`` defaults to the module-level ``_JSONL_MAX_BYTES`` read AT CALL TIME, not
    bound into the signature -- so the threshold can be overridden (a test does this) and the
    override is actually seen. One generation only: a pre-existing ``.1`` is overwritten, so
    disk use is bounded at roughly two files of ``max_bytes`` rather than growing without
    limit. os.replace is atomic on both Windows and POSIX, so a reader taking _JSONL_LOCK
    either side of this sees the whole old file or a fresh (absent, then empty) one -- never a
    half-moved path. Call with _JSONL_LOCK held. Never raises on a missing file or a failed
    move: rotation is a housekeeping step and must not break the append that triggered it.
    """
    if max_bytes is None:
        max_bytes = _JSONL_MAX_BYTES
    try:
        if os.path.getsize(jsonl_path) <= max_bytes:
            return False
    except OSError:
        return False
    try:
        os.replace(jsonl_path, jsonl_path + ".1")
        return True
    except OSError:
        return False


def _recent_lines(path, cutoff, max_bytes=32 * 1024 * 1024):
    """Read a bounded snapshot backwards, assuming timestamped records are ordered.

    Even for arbitrarily large windows, the snapshot is capped at 32 MB; counts
    can therefore be incomplete when the requested window exceeds that tail.
    Call with _JSONL_LOCK held so the snapshot cannot overlap an append.
    """
    chunk = 1024 * 1024
    with open(path, "rb") as fh:
        size = fh.seek(0, 2)
        pos = size
        blocks = []
        while pos > 0 and size - pos < max_bytes:
            step = min(chunk, pos, max_bytes - (size - pos))
            pos -= step
            fh.seek(pos)
            block = fh.read(step)
            blocks.append(block)
            # The first complete line in this block bounds older ordered records.
            start = block.find(b"\n") + 1 if pos else 0
            end = block.find(b"\n", start)
            if end < 0:
                continue
            try:
                record = json.loads(block[start:end])
                timestamp = record.get("time")
                if isinstance(timestamp, (int, float)) and timestamp < cutoff:
                    break
            except (ValueError, AttributeError):
                continue
        data = b"".join(reversed(blocks))
        if pos:
            newline = data.find(b"\n")
            data = data[newline + 1:] if newline >= 0 else b""
    return data.decode("utf-8", errors="replace").splitlines()


def health_counts(jsonl_path, now=None, window_s=600):
    """Counts for the last ``window_s`` seconds, for the /health route.

    Returns a dict with ``tunnel_channels``, ``tunnel_no_response`` and
    ``tunnel_remote_closed_early``. When no jsonl has ever been written, every value is -1
    (what /health shows then). Immediately after a size rotation the live file is briefly
    gone -- moved onto its ``.1`` sidecar with no new file yet -- and that is NOT absence:
    it is an empty current window, so the counts are 0 (never -1) as long as the sidecar
    exists. Records without a usable time are counted as current so a sample with no stamp
    is not silently dropped.
    """
    keys = ("tunnel_channels", "tunnel_no_response", "tunnel_remote_closed_early")
    if not os.path.exists(jsonl_path):
        # A rotation moves the live file onto ".1" and the next append recreates it; between
        # the two the live path is missing but the stream exists, so report an empty window
        # (0) rather than absence (-1). Only when neither the live file nor its sidecar
        # exists has nothing ever been written.
        if os.path.exists(jsonl_path + ".1"):
            return {k: 0 for k in keys}
        return {k: -1 for k in keys}
    if now is None:
        now = time.time()
    cutoff = now - window_s
    total = no_resp = remote_early = 0
    # Take the bounded snapshot under the shared append lock; parse it after release. A
    # rotation between the exists() check and the lock leaves the live file gone; treat that
    # as an empty window when the sidecar is present, consistent with the absence check above.
    try:
        with _JSONL_LOCK:
            snapshot = _recent_lines(jsonl_path, cutoff)
    except OSError:
        if os.path.exists(jsonl_path + ".1"):
            return {k: 0 for k in keys}
        return {k: -1 for k in keys}
    for line in snapshot:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        t = rec.get("time")
        if isinstance(t, (int, float)) and t < cutoff:
            continue
        total += 1
        if rec.get("no_response"):
            no_resp += 1
        if rec.get("remote_closed_early"):
            remote_early += 1
    return {
        "tunnel_channels": total,
        "tunnel_no_response": no_resp,
        "tunnel_remote_closed_early": remote_early,
    }


def _main(argv=None):
    """One-shot analysis of a ts-stamped tunnel host log, for diagnostics.

    ``python -m relay.tunnel_channels <log-path>`` reads the whole log with
    :func:`analyze_log` and prints one JSON object per closed channel -- the same records the
    tailing path appends to tunnel_channels.jsonl. This is the off-line counterpart to the
    live /health path: once a run has ended there is no reader to tail, and the whole file is
    what you have. Returns 0 even on an absent log (prints nothing), so a wrapper can call it
    unconditionally.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="relay.tunnel_channels",
        description="Print one JSON record per closed channel from a tunnel host log.")
    parser.add_argument("log_path", help="path to the ts-stamped tunnel host log")
    args = parser.parse_args(argv)
    for rec in analyze_log(args.log_path):
        sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(_main())
