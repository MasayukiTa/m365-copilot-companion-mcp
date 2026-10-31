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

* :func:`parse_channel_events` reads the ts-stamped log that
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
import time

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
        with open(jsonl_path, "a", encoding="utf-8") as out:
            for rec in parse_channel_events(chunk):
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                appended += 1
    with open(offset_path, "w", encoding="utf-8") as fh:
        fh.write(str(new_offset))
    return appended


def health_counts(jsonl_path, now=None, window_s=600):
    """Counts for the last ``window_s`` seconds, for the /health route.

    Returns a dict with ``tunnel_channels``, ``tunnel_no_response`` and
    ``tunnel_remote_closed_early``. When the jsonl file is absent, every value is -1
    (what /health shows then). Records without a usable time are counted as current so a
    sample with no stamp is not silently dropped.
    """
    keys = ("tunnel_channels", "tunnel_no_response", "tunnel_remote_closed_early")
    if not os.path.exists(jsonl_path):
        return {k: -1 for k in keys}
    if now is None:
        now = time.time()
    cutoff = now - window_s
    total = no_resp = remote_early = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                t = rec.get("time")
                if t is not None and t < cutoff:
                    continue
                total += 1
                if rec.get("no_response"):
                    no_resp += 1
                if rec.get("remote_closed_early"):
                    remote_early += 1
    except OSError:
        return {k: -1 for k in keys}
    return {
        "tunnel_channels": total,
        "tunnel_no_response": no_resp,
        "tunnel_remote_closed_early": remote_early,
    }
