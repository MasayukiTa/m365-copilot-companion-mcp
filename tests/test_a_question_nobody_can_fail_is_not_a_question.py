# -*- coding: utf-8 -*-
"""The grounding harness scored a success that no answer could have missed.

On 2026-09-16 the first held-out grounding run came back INSIDE and the harness recorded it,
printed a lower bound beside it, and invited 298 more of the same. The target was a maximised
window: its rectangle was 0,0..1600,900, the whole picture. Every coordinate that exists was
inside it. A model that had never opened the image would have scored exactly the same.

Containment is still the right criterion -- it is what a click cares about. What was missing
was the only thing that makes containment mean anything: how often the answer could have been
wrong. These tests hold that in place at both ends of the harness, so a target nobody can miss
is never handed out, and a result whose difficulty is unknown is never counted.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.make_grounding_task import MAX_CHANCE, chance_of_a_blind_hit  # noqa: E402


def test_a_target_that_fills_the_picture_would_be_hit_by_any_answer():
    """The exact shape of the run that started this: the rectangle IS the image."""
    assert chance_of_a_blind_hit([0, 0, 1600, 900], 1600, 900) == pytest.approx(1.0)
    assert 1.0 > MAX_CHANCE, "a whole-screen target has to be over the ceiling, or nothing is"


def test_the_blind_hit_rate_is_the_share_of_the_picture_the_target_covers():
    # A quarter of each axis is a sixteenth of the area, and that is the number that matters:
    # a blind answer is a point, not a row or a column.
    assert chance_of_a_blind_hit([0, 0, 400, 225], 1600, 900) == pytest.approx(1 / 16.0)
    assert chance_of_a_blind_hit([800, 450, 1200, 675], 1600, 900) == pytest.approx(1 / 16.0)


def test_a_rectangle_off_the_edge_cannot_report_a_negative_chance():
    """Clamping happens upstream, but a negative width must never read as easier than zero."""
    assert chance_of_a_blind_hit([500, 500, 100, 100], 1600, 900) == 0.0


def _task(tmp_path: Path, chance) -> Path:
    """A task file with one target, whose recorded difficulty is the thing under test."""
    target = {"name": "T", "class": "C", "rect_image": [0, 0, 1600, 900],
              "rect_screen": [0, 0, 1600, 900]}
    if chance is not None:
        target["chance"] = chance
    path = tmp_path / "task.json"
    path.write_text(json.dumps({"split": "held-out", "targets": [target],
                                "image_pixel_covers_desktop_px": 1.0}),
                    encoding="utf-8")
    return path


def _score(task: Path, answer: str):
    return subprocess.run([sys.executable, "-m", "scripts.score_grounding", str(task),
                           "--target", "T", "--answer", answer],
                          cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def test_a_result_from_a_task_that_never_measured_difficulty_is_refused(tmp_path):
    """This is the run that happened. It must now announce that it proves nothing."""
    out = _score(_task(tmp_path, None), "415,560").stdout
    assert "INSIDE" in out, "containment is still reported; it is the criterion"
    assert "CANNOT BE USED" in out
    # And it must not be quietly folded into the accumulated claim.
    assert "held-out so far: 0 of 0" in out


def test_a_scored_result_says_what_a_blind_answer_would_have_done(tmp_path):
    out = _score(_task(tmp_path, 0.0625), "415,560").stdout
    assert "lands inside 6% of the time" in out
    assert "held-out so far: 1 of 1" in out
    # Four coin flips' worth, and the harness says so rather than leaving "1 of 1" to speak.
    assert "4.0 coin flips" in out
