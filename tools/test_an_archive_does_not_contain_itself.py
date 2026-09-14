# -*- coding: utf-8 -*-
"""A backup written next to what it backs up must not swallow itself.

WHAT HAPPENED, 2026-09-14. Fleet workers were told to back the OGF folder up before editing it
and were not told where to put the backup, so they put it inside the folder. `zip_create` walks
its source with `rglob("*")`, which hands back the archive it is at that moment writing. Zipping
a file that grows because you are zipping it does not terminate: the read chases the write until
the disk is gone.

Six snapshots of one folder of about 150MB occupied **9.51GB**. Four of them -- 0.18, 1.46, 2.42
and 5.43GB -- had no end-of-central-directory record, so not one byte could be extracted from
any of them, and three contained an earlier snapshot nested inside. The workers reported a
successful backup and edited the originals. (The originals and their per-file `.bak` copies were
afterwards opened and CRC-checked, 13 of 13 intact -- the loss here was of the safety net, not
of the work, and that is luck rather than design.)

WHY THE FIX IS THIS NARROW. Skipping every `.zip` under the source would also silently drop
archives a caller legitimately wanted included, and a backup that quietly omits things is the
same class of defect wearing different clothes. The only file that can be proven wrong to
include is the output itself, so that is the only one excluded.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import archive_ops  # noqa: E402


@pytest.fixture
def unlocked(monkeypatch, tmp_path):
    """zip_create is a mutating tool behind the unlock gate; these tests are about its walk."""
    monkeypatch.setattr(archive_ops, "require_unlocked", lambda *a, **k: None)
    monkeypatch.setattr(archive_ops, "_validate_path", lambda p: __import__("pathlib").Path(p))
    return tmp_path


def _folder(tmp_path):
    src = tmp_path / "work"
    (src / "sub").mkdir(parents=True)
    for name, size in (("a.txt", 4000), ("sub/b.txt", 8000)):
        p = src / name
        with io.open(str(p), "w", encoding="utf-8") as fh:
            fh.write("x" * size)
    return src


def test_the_archive_is_not_written_into_itself(unlocked):
    """The defect, reproduced: the output lives inside the source."""
    src = _folder(unlocked)
    out = src / "_snapshots" / "snap.zip"
    result = archive_ops.zip_create(str(out), [str(src)])
    assert not result.startswith("["), result

    with zipfile.ZipFile(str(out)) as z:          # opens => it has a central directory
        names = [n.replace("\\", "/") for n in z.namelist()]
        assert z.testzip() is None
    assert not any(n.endswith("snap.zip") for n in names), (
        "the archive contains itself: %s" % names)
    assert any(n.endswith("a.txt") for n in names)
    assert any(n.endswith("b.txt") for n in names)


def test_it_stays_small_instead_of_chasing_its_own_tail(unlocked):
    """The measurable consequence. 12KB of content must not produce a large archive."""
    src = _folder(unlocked)
    out = src / "_snapshots" / "snap.zip"
    archive_ops.zip_create(str(out), [str(src)], compression="stored")
    assert out.stat().st_size < 200_000, (
        "archive is %d bytes for 12KB of input" % out.stat().st_size)


def test_an_archive_outside_the_source_still_takes_everything(unlocked):
    """The exclusion must not fire when it should not: nothing is dropped in the normal case."""
    src = _folder(unlocked)
    out = unlocked / "elsewhere" / "snap.zip"
    archive_ops.zip_create(str(out), [str(src)])
    with zipfile.ZipFile(str(out)) as z:
        names = [n.replace("\\", "/") for n in z.namelist()]
    assert sum(1 for n in names if n.endswith(".txt")) == 2


def test_an_unrelated_zip_inside_the_source_is_still_included(unlocked):
    """Only the output is excluded -- a backup that quietly omits archives is not a backup."""
    src = _folder(unlocked)
    keep = src / "attachment.zip"
    with zipfile.ZipFile(str(keep), "w") as z:
        z.writestr("inner.txt", "content")
    out = src / "_snapshots" / "snap.zip"
    archive_ops.zip_create(str(out), [str(src)])
    with zipfile.ZipFile(str(out)) as z:
        names = [n.replace("\\", "/") for n in z.namelist()]
    assert any(n.endswith("attachment.zip") for n in names), (
        "a zip the caller put in the source was dropped: %s" % names)
