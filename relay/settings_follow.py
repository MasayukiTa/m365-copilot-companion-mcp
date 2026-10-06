# -*- coding: utf-8 -*-
"""Keep a running fleet's live knobs in step with the settings file, not with the
moment it happened to start.

WHAT WENT WRONG. Every operator-settable number the coordinator uses -- the disk
floor, the RAM floor, the tab cap, the effort mode -- was read once, at launch,
into a one-element list, and after that the file was never opened again. The only
way a change could reach a running fleet was a live push from a cockpit that
happened to be alive at that instant and whose command file happened to be read
before the run ended.

So on 2026-09-15 the coordinator started at 15:08:26, the operator's settings were
saved at 15:15, and the run spent its whole life reserving 4 GB of disk and 1024 MB
of RAM while the panel the operator was looking at said 1 GB and 512 MB. Nothing
was broken in the resolution chain -- CLI beats settings beats env is exactly what
the code does, and the autostart path had already been fixed to stop passing a flag
that overrode the file. The chain was simply evaluated once, and a value read once
from a file a person keeps editing is a value that starts drifting immediately.

That the disk floor and the RAM floor were BOTH wrong, by different amounts, is the
proof that this is the mechanism and not a bad number: two knobs cannot disagree
with the same file in the same direction by accident.

WHAT THIS DOES. On every sweep, re-read the file and compare it with what the file
said last time. When the FILE's value changed, the operator moved the knob, so the
new value is adopted. When the file's value did not change, the live value is left
exactly as it is.

That second half is what keeps this from being a blunt overwrite. The cockpit's
強制開始 drops the disk floor to 0 live so a run can start on a full disk; if this
followed the file unconditionally it would put the floor back on the next sweep and
undo the operator's decision a second later. Following *changes* rather than
*values* means a live override survives until the operator next touches that knob --
at which point they have plainly said what they want, and they win.

The other property worth naming: a follower that only acts on change is INERT for
any run where nobody edits the file. It cannot alter the behaviour of a run that
does not involve the operator changing their mind, which is what makes it safe to
put in the sweep of a coordinator that is driving real work.
"""
from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional, Tuple


class _FileCache:
    """Parsed settings, re-read only when the file itself changed.

    The sweep runs often and the file is tiny, but 'tiny' is not 'free' and this
    also means a torn read (the cockpit rewrites the whole file) is retried on the
    next sweep rather than latched -- the same mistake this module exists to undo.
    """

    def __init__(self, path_fn: Callable[[], str]):
        self._path_fn = path_fn
        self._stamp: Optional[Tuple[float, int]] = None
        self._values: Dict[str, str] = {}

    def values(self) -> Dict[str, str]:
        try:
            path = self._path_fn()
            st = os.stat(path)
        except Exception:
            # No file (or no APPDATA): the file says nothing. Deliberately NOT an
            # empty dict-of-changes -- see Follower.poll, which treats "the file
            # does not mention this key" as "do not touch the live value".
            return self._values
        stamp = (st.st_mtime, st.st_size)
        if stamp == self._stamp:
            return self._values
        parsed: Dict[str, str] = {}
        try:
            with open(path, encoding="utf-8-sig") as fh:      # the C# cockpit may write a BOM
                for ln in fh.read().splitlines():
                    if "=" in ln:
                        k, v = ln.split("=", 1)
                        parsed[k.strip()] = v.strip()
        except Exception:
            return self._values                                # keep the last good parse
        self._stamp = stamp
        self._values = parsed
        return self._values


class Follower:
    """Adopts settings-file changes into live values, one key at a time."""

    def __init__(self, path_fn: Callable[[], str]):
        self._cache = _FileCache(path_fn)
        self._watched: List[Tuple[str, Callable[[float], None], Callable[[str], float]]] = []
        self._last_seen: Dict[str, str] = {}
        self._primed = False

    def watch(self, key: str, apply: Callable[[float], None],
              coerce: Callable[[str], float] = float) -> "Follower":
        """Follow `key`; call `apply(value)` when the file's value for it changes.

        `apply` rather than a box index because the same key does not always mean
        the same live value: `maxtabs` is the fixed cap when autoscale is off and
        the ceiling when it is on, and the cockpit's own command handler already
        makes that distinction. Two places deciding it differently is the drift
        this module is here to prevent.
        """
        self._watched.append((key, apply, coerce))
        return self

    def prime(self) -> "Follower":
        """Record what the file says now WITHOUT applying anything.

        Called once after launch, because launch has already read the file. Without
        this, the first sweep would see every key as 'changed' and re-apply values
        that are equal anyway -- harmless for the floors, but it would also stamp on
        a CLI flag that deliberately overrode the file.
        """
        self._last_seen = dict(self._cache.values())
        self._primed = True
        return self

    def poll(self) -> List[Tuple[str, float]]:
        """Apply any settings the operator changed. Returns what was adopted.

        Never raises: this runs inside a coordinator sweep that is driving real
        work, and a malformed line someone typed into a settings file must not be
        able to end a run. A value that will not parse is left for the next sweep,
        by which time the operator has usually finished typing.
        """
        if not self._primed:
            self.prime()
            return []
        adopted: List[Tuple[str, float]] = []
        try:
            now = self._cache.values()
        except Exception:
            return adopted
        for key, apply, coerce in self._watched:
            raw = now.get(key)
            if raw is None:
                # The file does not mention this key at all, so it expresses no
                # opinion. Not the same as "the operator set it to the default":
                # treating silence as a value would overwrite a live override with
                # a number nobody chose.
                continue
            if raw == self._last_seen.get(key):
                continue
            try:
                value = coerce(raw)
            except Exception:
                continue                    # half-typed; do not record it as seen
            self._last_seen[key] = raw
            try:
                apply(value)
            except Exception:
                continue
            adopted.append((key, value))
        return adopted
