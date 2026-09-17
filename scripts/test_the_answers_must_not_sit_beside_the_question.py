# -*- coding: utf-8 -*-
"""A grounding task must not ship its own answer key next to the picture.

MEASURED 2026-09-17 10:37. A worker was asked where the OneNote window was in
`grounding-20260917-100507\\screen.png`. It answered 1443,614 -- the exact centre of the true
rectangle -- and said where that came from:

    "The OneNote window rect_image is [1286.7, 327.5, 1600.0, 900.0]. Center: x≈1443, y≈614"

That is task.json, which listed rect_image for every target and sat in the same directory as
screen.png. Naming the picture named the answers. Nothing about the answer distinguishes
reading the key from having looked, which makes the measurement worthless in BOTH directions:
a leak that produces a bad score is still a leak.

Challenged by the refuter, the worker then said it had cropped that region and confirmed the
OneNote interface visually. read_image returns a data URI as a plain string and FastMCP
serialises a string as text, so no picture was ever in front of it. That is a separate defect
and is recorded separately; this file pins the layout.

THE FIX IS SEPARATION, NOT SECRECY. A longer filename is found by the same listing that found
the short one, and removing the rectangles breaks the scorer, which needs them. A directory
holding exactly one file is a property that survives an agent with read access.
"""
from __future__ import annotations

import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts.make_grounding_task import task_paths          # noqa: E402


def test_the_answers_are_not_on_the_pictures_walk_up_path():
    out = os.path.join("C:", os.sep, "anywhere", "g")
    png, answers = task_paths(out)
    assert os.path.dirname(png) != os.path.dirname(answers), \
        "naming the picture to a worker names the answers"
    # AND NOT ONE LEVEL UP EITHER. A single directory of separation defeats a listing and
    # nothing else; any agent with read access can go up one. This is not secrecy -- an agent
    # that goes looking will find the file -- but the lazy path stops returning the answer,
    # and a worker that produces the exact rectangle centre now has to have searched for it,
    # which its transcript shows.
    one_up = os.path.dirname(os.path.dirname(png))
    assert os.path.dirname(answers) != one_up, "the answers are one level up from the picture"


def test_the_picture_is_alone_in_its_directory(tmp_path):
    """The property that actually holds against an agent with read access: there is nothing
    else in there to find."""
    out = str(tmp_path / "task")
    png, answers = task_paths(out)
    os.makedirs(os.path.dirname(png))
    os.makedirs(os.path.dirname(answers))
    io.open(png, "wb").write(b"\x89PNG\r\n")
    io.open(answers, "w", encoding="utf-8").write("{}")
    assert os.listdir(os.path.dirname(png)) == ["screen.png"]


def test_the_answers_are_not_reachable_by_listing_beside_the_picture(tmp_path):
    """A worker given the picture's path lists its directory. That listing must not contain a
    file with rect_image in it."""
    out = str(tmp_path / "task")
    png, answers = task_paths(out)
    os.makedirs(os.path.dirname(png))
    os.makedirs(os.path.dirname(answers))
    io.open(png, "wb").write(b"\x89PNG\r\n")
    io.open(answers, "w", encoding="utf-8").write('{"targets":[{"rect_image":[1,2,3,4]}]}')
    beside = os.path.dirname(png)
    for name in os.listdir(beside):
        body = io.open(os.path.join(beside, name), "rb").read()
        assert b"rect_image" not in body, "%s beside the picture carries the answers" % name


def test_the_generator_tells_the_operator_which_path_to_hand_over():
    """The leak was not a missing rule; nobody had written down which of the two paths is
    safe to give a worker. The generator says it, next to the paths themselves."""
    body = io.open(os.path.join(REPO, "scripts", "make_grounding_task.py"),
                   encoding="utf-8").read()
    assert "ASK ABOUT:" in body
    assert "it lists every answer" in body
