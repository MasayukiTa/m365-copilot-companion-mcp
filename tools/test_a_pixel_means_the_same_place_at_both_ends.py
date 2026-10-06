# -*- coding: utf-8 -*-
"""A coordinate read off a screenshot must name the place it was read from.

A computer-use run has two independent ways to click the wrong thing: the model can
read the wrong place off the picture, or the picture's pixels can be converted to
desktop coordinates wrongly. Measured together they are indistinguishable, and the
second is a constant bias -- so it would be recorded as "the model is bad at
coordinates", a conclusion that then survives every later experiment because
nothing afterwards ever isolates it.

These tests pin the conversion. They touch no screen: the algebra lives in
tools/screen_frame.py precisely so it can be checked without one, and the live
sweep against the real desktop is scripts/calibrate_executor.py.

Measured on this machine 2026-09-15, which is why the origin cases are not
hypothetical: the virtual desktop reports 3840x2173 at (-985, -1093). Treating the
image's top-left as the desktop's would put every click about a thousand pixels
off on both axes.
"""
import pytest

from tools.screen_frame import Frame, FrameError, full_size, round_trip_error


def test_a_full_size_capture_changes_nothing():
    f = full_size(0, 0, 1920, 1080)
    assert f.scale == 1.0
    assert not f.is_downscaled
    for x, y in ((0, 0), (1, 1), (960, 540), (1919, 1079)):
        assert f.to_image(x, y) == (x, y)
        assert f.to_screen(x, y) == (x, y)


def test_a_desktop_that_starts_left_of_zero():
    """The measured layout on this machine. A monitor above and to the left of the
    primary one gives a negative origin, and the image's (0, 0) is not the
    desktop's."""
    f = full_size(-985, -1093, 3840, 2173)
    assert f.to_image(-985, -1093) == (0, 0)
    assert f.to_screen(0, 0) == (-985, -1093)
    # The primary monitor's own origin is somewhere in the middle of the image.
    assert f.to_image(0, 0) == (985, 1093)
    assert f.to_screen(985, 1093) == (0, 0)


def test_a_point_outside_the_capture_is_named_as_such():
    f = full_size(-985, -1093, 3840, 2173)
    assert f.contains_screen(-985, -1093)
    assert f.contains_screen(2854, 1079)
    assert not f.contains_screen(-986, -1093)
    assert not f.contains_screen(2855, 1080)


def test_a_downscaled_image_aims_at_the_middle_of_the_block_it_stands_for():
    """Each image pixel covers several desktop pixels; aim at the centre of them.

    Aiming at the block's top-left corner instead puts every click half a block up
    and to the left of where the model meant, which on a 1:4 capture is two pixels
    -- enough to fall off a small control, and a bias that would be read as the
    model aiming high and left.
    """
    f = Frame(0, 0, 4000, 4000, 1000, 1000).validate()
    assert f.scale == 4.0
    assert f.to_screen(0, 0) == (1, 1)         # centre of the 0..3 block
    assert f.to_screen(1, 1) == (5, 5)
    assert f.to_screen(999, 999) == (3997, 3997)


def test_the_precision_a_downscale_costs_is_reported_not_hidden():
    """No model reading a reduced image can be more precise than the reduction.

    This is the floor under any grounding accuracy measured through that image, so
    it has to be a number the measurement can print, not an assumption.
    """
    full = full_size(0, 0, 3840, 2173)
    for x, y in ((0, 0), (1000, 1000), (3839, 2172)):
        assert round_trip_error(full, x, y) == 0

    half = Frame(0, 0, 3840, 2173, 1920, 1086).validate()
    worst = max(round_trip_error(half, x, y)
                for x in range(0, 3840, 97) for y in range(0, 2173, 89))
    assert worst <= 2, worst
    assert worst >= 1, "a 1:2 capture cannot be lossless; the check is not measuring"


def test_a_stretched_image_is_refused_and_a_rounded_one_is_not():
    """The difference follows from the resize, it is not a tolerance somebody picked.

    A uniform resize rounds each output dimension to a whole pixel, so each axis can
    be off by half an image pixel. Capturing this machine's 3840x2173 desktop at
    1920 wide gives 1086.5 -> 1086: legitimate, and an earlier fixed one-pixel
    allowance rejected it. Anything rounding cannot explain is a real stretch, and a
    stretch skews y against x in a way one scale cannot undo.
    """
    Frame(0, 0, 3840, 2173, 1920, 1086).validate()       # rounding: accepted
    Frame(0, 0, 3840, 2173, 1280, 724).validate()        # 724.33 -> 724: accepted

    with pytest.raises(FrameError) as e:
        Frame(0, 0, 3840, 2160, 1920, 800).validate()    # 4:3 from a 16:9 desktop
    assert "stretched" in str(e.value)


def test_a_frame_with_no_area_is_refused_rather_than_divided_by():
    for bad in (Frame(0, 0, 0, 1080, 1920, 1080),
                Frame(0, 0, 1920, 0, 1920, 1080),
                Frame(0, 0, 1920, 1080, 0, 1080),
                Frame(0, 0, 1920, 1080, 1920, 0)):
        with pytest.raises(FrameError):
            bad.validate()


def test_a_downscaled_frame_says_so_where_a_reader_will_see_it():
    """tools/screenshot_ops.py downscales anything over 1920 and returns only a path.

    That is right for a picture a person looks at and wrong for one something acts
    on, which is why capture() carries a Frame. The description is what a run
    records, so the fact has to be in it rather than derivable from it.
    """
    assert "DOWNSCALED" in Frame(0, 0, 3840, 2173, 1920, 1086).validate().describe()
    assert "DOWNSCALED" not in full_size(0, 0, 1920, 1080).describe()
