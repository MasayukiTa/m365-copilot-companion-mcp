# -*- coding: utf-8 -*-
"""Nothing proposed may vanish between the count and the disk.

THREE TIMES IN ONE SITTING, the same shape: work written to a filename something else also
wanted, and gone without a word.

1. Two candidate groups collapsed to one instruction once our own fan-out composition was
   stripped, and since the bundle name is a digest OF the instruction they competed for one
   directory. Ten amendments proposed; five files on disk.
2. Amendments were then named after the SKILL they target -- correct, and it made several of
   them share a filename. Five proposed; two files.
3. The same shape outside this module entirely: the OGF snapshot archives, each written into
   the folder it was archiving, each swallowing the last.

A generator that can silently drop its own output is not reporting what it did, and every count
it prints is a claim nobody can check. So the invariant is not "the merge works" or "the naming
works" -- it is **the number of things written equals the number of things claimed**, which
holds however the naming changes next.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import skill_draft  # noqa: E402


def _proposal(name, body="body", existing=None):
    p = {"name": name, "runs": 3, "done": 2, "goal": "g", "paths": [], "contracts": [],
         "lessons": [], "body": body}
    if existing:
        p["existing"] = existing
    return p


def test_every_new_proposal_reaches_disk(tmp_path):
    result = {"new": [_proposal("work-%02d" % i, "body %d" % i) for i in range(6)],
              "amend": [], "refused": []}
    written = skill_draft.write(result, directory=str(tmp_path))
    files = [p for p in written if p.endswith("SKILL.md")]
    assert len(files) == 6, "%d proposals, %d files" % (6, len(files))
    assert len(set(files)) == 6, "two proposals wrote to one path"


def test_amendments_for_one_skill_all_survive_in_its_file(tmp_path):
    """The bug: naming an amendment after its target made four of them one file of one."""
    result = {"new": [],
              "amend": [_proposal("work-%02d" % i, "amendment number %d" % i,
                                  existing="mail-lookup") for i in range(4)],
              "refused": []}
    skill_draft.write(result, directory=str(tmp_path))
    path = tmp_path / "mail-lookup.amend.md"
    assert path.exists(), "the amendment file is not named after the skill it is for"
    text = path.read_text(encoding="utf-8")
    for i in range(4):
        assert "amendment number %d" % i in text, "amendment %d was overwritten" % i


def test_an_amendment_names_the_skill_it_belongs_to_not_its_own_digest(tmp_path):
    """Where it goes is the one thing an amendment has to get right."""
    result = {"new": [], "amend": [_proposal("work-22fcaee663", existing="mail-lookup")],
              "refused": []}
    skill_draft.write(result, directory=str(tmp_path))
    text = (tmp_path / "mail-lookup.amend.md").read_text(encoding="utf-8")
    assert "skills/mail-lookup/SKILL.md" in text
    assert "skills/work-22fcaee663" not in text, (
        "the reader is sent to a directory that does not exist")


def test_amendments_for_different_skills_stay_apart(tmp_path):
    result = {"new": [],
              "amend": [_proposal("work-a", "for mail", existing="mail-lookup"),
                        _proposal("work-b", "for desktop", existing="desktop-md-inventory")],
              "refused": []}
    skill_draft.write(result, directory=str(tmp_path))
    assert "for mail" in (tmp_path / "mail-lookup.amend.md").read_text(encoding="utf-8")
    assert "for desktop" in (tmp_path / "desktop-md-inventory.amend.md").read_text(
        encoding="utf-8")


def test_the_run_that_merges_two_groups_says_it_merged_them():
    """A count that quietly shrinks is the thing this file exists to prevent, so the shrinking
    is reported rather than inferred from a smaller number than last time."""
    result = skill_draft.propose()
    assert "merged" in result, "propose() does not report how many groups it combined"
    assert isinstance(result["merged"], int)
