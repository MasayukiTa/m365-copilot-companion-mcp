# -*- coding: utf-8 -*-
"""Cropping to one window must not cost the ability to click in it.

A picture is the most expensive thing that can enter a conversation, and it is what has been
killing computer-use runs. Measured 2026-09-15 across one afternoon's transcripts: workers
whose task was to look at the screen hit the conversation's token limit in 9 of 11 runs,
against 4 of 24 for workers that looked at nothing -- and they hit it after two to seven
turns. Cutting our own first turn from 10,081 characters to 2,231 changed the rate not at
all, because the turn was never the large thing in the room.

Capturing one window instead of the whole desktop measured 7x smaller on this machine
(689,101 bytes for 1600x900 of desktop; 98,349 for a 400x516 window). But a saving that
breaks the coordinates is worthless: the whole point of the Frame is that a pixel named in
an image converts back to the right place on the desktop, and a crop moves the origin. These
tests pin that the origin moves WITH the crop, so a click computed from a cropped picture
lands where the model meant.

The algebra is exercised directly rather than through a capture, so this runs anywhere: the
live sweep against a real desktop is scripts/calibrate_executor.py.
"""
import pytest

from tools.screen_frame import Frame, FrameError


def _cropped(desktop, region, image=None):
    """The Frame a crop produces: origin at the crop's corner, size of the crop."""
    left, top, right, bottom = region
    w, h = right - left, bottom - top
    iw, ih = image or (w, h)
    return Frame(left, top, w, h, iw, ih).validate()


def test_a_crop_moves_the_origin_with_it():
    """The crop's top-left is the image's (0, 0), not the desktop's."""
    f = _cropped((0, 0, 1920, 1080), (400, 300, 1200, 800))
    assert f.to_screen(0, 0) == (400, 300)
    assert f.to_image(400, 300) == (0, 0)
    # A point in the middle of the crop is that point on the desktop.
    assert f.to_screen(400, 250) == (800, 550)
    assert f.to_image(800, 550) == (400, 250)


def test_a_crop_on_a_desktop_that_starts_left_of_zero():
    """Both offsets compose: the desktop's own origin and the crop's.

    Measured on this machine earlier the same day: the virtual desktop reported
    3840x2173 at (-985, -1093) with a second monitor above and to the left. Getting one of
    the two offsets right and the other wrong is the failure this pins.
    """
    f = _cropped((-985, -1093, 2855, 1080), (-500, -900, 300, -400))
    assert f.to_screen(0, 0) == (-500, -900)
    assert f.to_image(-500, -900) == (0, 0)
    assert f.to_screen(800, 500) == (300, -400)


def test_a_point_outside_the_crop_is_refused_even_though_it_is_on_the_desktop():
    """The model could not see it, so a click computed from it is not its answer.

    This is the case that appeared for real when two monitors were unplugged mid-session:
    147 probe points fell outside the captured area because windows still carried
    coordinates from the layout that no longer existed.
    """
    f = _cropped((0, 0, 1920, 1080), (400, 300, 1200, 800))
    assert f.contains_screen(400, 300)
    assert f.contains_screen(1199, 799)
    assert not f.contains_screen(399, 300)    # on the desktop, left of the crop
    assert not f.contains_screen(1200, 800)   # on the desktop, past the crop


def test_a_downscaled_crop_still_converts():
    """The two reductions compose: crop then resize, and a click still lands."""
    f = _cropped((0, 0, 1920, 1080), (400, 300, 1200, 800), image=(400, 250))
    assert f.scale == 2.0
    assert f.to_screen(0, 0) == (400, 300)     # centre of the block the pixel stands for
    back = f.to_screen(*[int(round(v)) for v in f.to_image(800, 550)])
    assert abs(back[0] - 800) <= 1 and abs(back[1] - 550) <= 1


def test_a_crop_of_no_area_is_refused_rather_than_divided_by():
    with pytest.raises(FrameError):
        Frame(400, 300, 0, 500, 0, 500).validate()


@pytest.mark.parametrize("region", [
    (0, 0, 1920, 1080),        # the whole thing
    (1919, 1079, 1920, 1080),  # one pixel in the far corner
    (0, 0, 1, 1),              # one pixel at the origin
])
def test_the_edges_of_the_desktop_are_croppable(region):
    """A dialog against the screen edge is a normal thing to want a picture of."""
    f = _cropped((0, 0, 1920, 1080), region)
    left, top = region[0], region[1]
    assert f.to_screen(0, 0) == (left, top)
    assert f.contains_screen(left, top)
