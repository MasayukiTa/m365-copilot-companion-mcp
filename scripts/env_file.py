#!/usr/bin/env python3
# =============================================================================
# env_file.py -- the one place that edits .env, and it edits it ATOMICALLY.
#
# WHY THIS EXISTS (D28 in the new-PC install-path review, 2026-09-24). .env had eight writers
# and all but one of them rewrote it in place: open, truncate, write. A window closed or a
# machine switched off inside that write leaves a truncated .env, and the next bootstrap run
# reads "no MCP_API_KEY" and MINTS A NEW ONE -- silently, because the localhost auth check then
# passes with the new key while Copilot Studio keeps sending the old one and gets 401. The
# cure is the one heal_tunnel.ps1 already used: write a temporary file beside .env and swap it
# in with a rename, which is all-or-nothing on NTFS. Either the old file or the new one is
# there afterwards; never half of either.
#
# quickstart.bat needs the same guarantee and cmd cannot rename-over atomically, so the batch
# file calls this script instead of carrying its own one-line PowerShell writers.
#
# CLI (used by quickstart.bat):
#   python scripts/env_file.py set   KEY VALUE [--env PATH]
#   python scripts/env_file.py unset KEY       [--env PATH]
#
# Standard library only: this runs under whatever interpreter setup produced.
# ASCII / ENGLISH ONLY.
# =============================================================================
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def read_text(path: Path) -> str:
    """The file as text. utf-8-sig: a BOM left by PowerShell 5.1's Set-Content must not fold
    into the first key name."""
    # newline="" KEEPS "\r\n": the default text mode folds it to "\n", after which newline_of()
    # could never see what the file used and every edit rewrote a CRLF file as LF.
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def newline_of(text: str) -> str:
    """Keep the file's own line ending. Rewriting CRLF as LF is harmless to the parsers but
    turns every edit into a whole-file diff for anyone who keeps .env under local version
    control, and bootstrap/configure_env disagree on which one they write."""
    return "\r\n" if "\r\n" in text else "\n"


def atomic_write_text(path: Path, text: str, attempts: int = 10) -> None:
    """Write `text` to `path` as UTF-8 without a BOM, all-or-nothing.

    The temporary file lives in the SAME directory, so the final rename never crosses a volume
    (a cross-volume move is a copy, and a copy is not atomic). os.replace is retried briefly:
    on Windows the rename is refused while another process holds .env open without
    FILE_SHARE_DELETE -- a reader that opened it a millisecond ago -- and that is a wait, not
    an error. The temporary file is removed on every failure path so none is left behind.
    """
    path = Path(path)
    tmp = path.with_name("%s.tmp-%d" % (path.name, os.getpid()))
    data = text.encode("utf-8")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        last = None
        for i in range(max(1, attempts)):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:
                last = e
                time.sleep(0.1 * (i + 1))
        raise last  # type: ignore[misc]
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def active_keys(text: str) -> set:
    """Keys that have an ACTIVE (uncommented) assignment."""
    out = set()
    for line in text.splitlines():
        m = _KEY_RE.match(line)
        if m:
            out.add(m.group(1))
    return out


def commented_keys(text: str) -> set:
    """Keys that appear only as a commented-out assignment, e.g. `# MCP_TOOL_MAP=1`."""
    out = set()
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            m = _KEY_RE.match(s.lstrip("#"))
            if m:
                out.add(m.group(1))
    return out


def example_assignments(example_text: str) -> list:
    """Active KEY=VALUE lines of .env.example, in file order, as (key, line)."""
    out = []
    for line in example_text.splitlines():
        m = _KEY_RE.match(line)
        if m:
            out.append((m.group(1), line.strip()))
    return out


def append_lines(text: str, lines: list) -> str:
    """`text` with `lines` appended, each on its own line, in the file's own newline."""
    if not lines:
        return text
    nl = newline_of(text) if text else "\n"
    sep = "" if (not text or text.endswith(("\n", "\r"))) else nl
    return text + sep + nl.join(lines) + nl


def set_key(path: Path, key: str, value: str) -> None:
    """KEY=VALUE, replacing the FIRST active assignment and dropping any later duplicates
    (the readers here disagree on first-wins vs last-wins, so a duplicate is a value two
    programs read differently). Appended when absent."""
    text = read_text(path)
    nl = newline_of(text) if text else "\n"
    out, done = [], False
    for line in text.splitlines():
        m = _KEY_RE.match(line)
        if m and m.group(1) == key:
            if not done:
                out.append("%s=%s" % (key, value))
                done = True
            continue
        out.append(line)
    if not done:
        out.append("%s=%s" % (key, value))
    atomic_write_text(path, nl.join(out) + nl)


def unset_key(path: Path, key: str) -> bool:
    """Remove every active assignment of KEY. Returns whether anything was removed. A file
    without the key is left byte-for-byte alone (no rewrite at all)."""
    text = read_text(path)
    if not text:
        return False
    nl = newline_of(text)
    kept, removed = [], False
    for line in text.splitlines():
        m = _KEY_RE.match(line)
        if m and m.group(1) == key:
            removed = True
            continue
        kept.append(line)
    if removed:
        atomic_write_text(path, nl.join(kept) + nl)
    return removed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Atomic .env edits.")
    ap.add_argument("action", choices=("set", "unset"))
    ap.add_argument("key")
    ap.add_argument("value", nargs="?")
    ap.add_argument("--env", default=str(ROOT / ".env"))
    a = ap.parse_args(argv)
    if not _KEY_RE.match(a.key + "="):
        print("env_file.py: not a valid key name: %r" % a.key, file=sys.stderr)
        return 2
    env = Path(a.env)
    if a.action == "set":
        if a.value is None:
            print("env_file.py: 'set' needs a value", file=sys.stderr)
            return 2
        set_key(env, a.key, a.value)
        return 0
    removed = unset_key(env, a.key)
    print("removed" if removed else "absent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
