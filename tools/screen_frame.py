# -*- coding: utf-8 -*-
"""What a pixel in a captured image means on the physical desktop.

A model that is shown a screenshot answers in the screenshot's pixels. Every
click that follows is made in the desktop's pixels. Those two are not the same
number, and nothing in the image says so. This module is the one place that
holds the conversion, so that a wrong click can be attributed to the model
rather than quietly blamed on it.

Three things make them differ, and all three are invisible in the image:

* THE ORIGIN. With more than one monitor the virtual desktop's top-left is not
  (0, 0). A second screen placed to the left of the primary gives a negative
  SM_XVIRTUALSCREEN, so image pixel (0, 0) is desktop (-1920, 0). Treating the
  image's origin as the desktop's origin puts every click one screen to the
  right.

* THE SCALE. tools/screenshot_ops.py downscales anything wider than 1920 before
  it writes the file, and returns only a path. A model reading that image of a
  3840-wide desktop reports coordinates in the halved frame; passing them
  through unchanged lands every click at half the intended distance from the
  top-left corner -- an error that grows across the screen and so looks exactly
  like a model that is bad at coordinates. A Frame travels with the image
  specifically so that this cannot be lost.

* DPI VIRTUALISATION. A process that is not per-monitor-DPI aware is lied to by
  Windows: GetSystemMetrics and GetCursorPos report logical pixels, while the
  captured bitmap is physical. On a 150% display that is a 1.5x disagreement
  between the numbers we compute with and the numbers we clicked at. See
  tools/screen_capture.py::make_process_dpi_aware, which must run before any of
  this is read.

The algebra here is pure and has no import of ctypes, so it can be tested
without a desktop: see tools/test_a_pixel_means_the_same_place_at_both_ends.py.
The Windows queries that fill a Frame in live at tools/screen_capture.py.
"""
from __future__ import annotations

import math
from typing import NamedTuple, Tuple


class FrameError(ValueError):
    """A frame that cannot describe a real capture.

    Raised rather than tolerated. A frame is read once and then used for every
    click of a run; a frame that is quietly accepted while slightly wrong
    produces a drift that is indistinguishable from model error.
    """


class Frame(NamedTuple):
    """The mapping between one captured image and the physical desktop.

    origin_x/origin_y: the desktop coordinate of image pixel (0, 0). These are
        SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN and may be negative.
    width/height: the virtual desktop's size in physical pixels.
    image_width/image_height: the captured image's size in its own pixels.

    The scale is derived, never passed in, so there is no way for a stored
    scale to disagree with the image that was actually saved.
    """

    origin_x: int
    origin_y: int
    width: int
    height: int
    image_width: int
    image_height: int

    def validate(self) -> "Frame":
        """Return self, or raise FrameError saying which part cannot be true.

        The aspect check has to tell a STRETCH from a ROUNDING, and the difference
        is not a tolerance somebody picks -- it follows from the resize. A uniform
        resize computes each output dimension and rounds it to a whole pixel, so
        each axis can be off by up to half an image pixel, which is up to `scale`
        desktop pixels. Capturing this machine's 3840x2173 desktop at 1920 wide
        gives 1086.5 -> 1086, exactly that: legitimate, and a fixed 1-pixel
        allowance rejected it. Anything beyond what rounding can explain is a real
        stretch, and a stretch skews y against x in a way one scale cannot undo.
        """
        if self.width <= 0 or self.height <= 0:
            raise FrameError(
                "desktop has no area: %dx%d" % (self.width, self.height))
        if self.image_width <= 0 or self.image_height <= 0:
            raise FrameError(
                "image has no area: %dx%d" % (self.image_width, self.image_height))
        sx = self.width / float(self.image_width)
        sy = self.height / float(self.image_height)
        # Where the far edge lands if the x scale is used for both axes, and the
        # mirror of that -- in desktop pixels, which is the unit a click misses by.
        drift = max(abs(sx * self.image_height - self.height),
                    abs(sy * self.image_width - self.width))
        allowed = max(1.0, max(sx, sy))
        if drift > allowed + 1e-9:
            raise FrameError(
                "image is stretched, not scaled: desktop %dx%d captured as %dx%d "
                "(the far edge misses by %.1f desktop px; rounding a uniform resize "
                "could only explain %.1f)"
                % (self.width, self.height, self.image_width, self.image_height,
                   drift, allowed))
        return self

    @property
    def scale(self) -> float:
        """Desktop pixels per image pixel. 1.0 when the capture is full size."""
        return self.width / float(self.image_width)

    @property
    def is_downscaled(self) -> bool:
        return abs(self.scale - 1.0) > 1e-9

    def to_image(self, screen_x: float, screen_y: float) -> Tuple[float, float]:
        """Desktop coordinate -> pixel in this image."""
        return ((screen_x - self.origin_x) / self.scale,
                (screen_y - self.origin_y) / self.scale)

    def to_screen(self, image_x: float, image_y: float) -> Tuple[int, int]:
        """Pixel in this image -> desktop coordinate to aim at.

        When the image is downscaled, one image pixel stands for a whole block of
        desktop pixels, and the click has to go somewhere inside it. It goes to the
        middle: aiming at the block's top-left corner instead would put every click
        half a block up and to the left of where the model meant -- on a 1:4 capture
        that is two pixels, enough to fall off a small control, and a bias that
        would read as the model consistently aiming high and left.

        When the block has an even number of pixels the middle falls between two of
        them and the lower is taken. Which one hardly matters; that it is DECIDED
        does, because round-half-to-even would otherwise pick a different side of
        the block depending on where in the screen the block is.
        """
        s = self.scale
        return (int(math.floor(self.origin_x + image_x * s + (s - 1) / 2.0)),
                int(math.floor(self.origin_y + image_y * s + (s - 1) / 2.0)))

    def contains_screen(self, screen_x: float, screen_y: float) -> bool:
        """Is this desktop coordinate inside the captured area at all?

        A click computed from an image can only be checked against the image's
        own bounds; a point outside them was never visible to the model and a
        run that produces one has a defect upstream, not a bad guess.
        """
        return (self.origin_x <= screen_x < self.origin_x + self.width
                and self.origin_y <= screen_y < self.origin_y + self.height)

    def describe(self) -> str:
        note = "" if not self.is_downscaled else (
            "  DOWNSCALED 1:%.3f -- image pixels are not desktop pixels" % self.scale)
        return ("desktop %dx%d at (%d, %d), image %dx%d%s"
                % (self.width, self.height, self.origin_x, self.origin_y,
                   self.image_width, self.image_height, note))


def full_size(origin_x: int, origin_y: int, width: int, height: int) -> Frame:
    """A frame for a capture that was not resized."""
    return Frame(origin_x, origin_y, width, height, width, height).validate()


def round_trip_error(frame: Frame, screen_x: int, screen_y: int) -> float:
    """How far a desktop point moves by going to the image and back, in px.

    THE ROUNDING TO A WHOLE IMAGE PIXEL IS THE POINT, NOT AN IMPLEMENTATION
    DETAIL. A model looking at a picture names a pixel; it cannot name pixel
    2.5. Carrying the fractional coordinate through and back makes the round
    trip nearly exact and reports that a downscaled capture costs nothing --
    which is the precise false comfort this function exists to deny. So the
    image coordinate is quantised the way the model's answer is quantised, and
    what comes back is the real floor under any grounding accuracy measured
    through that image: no model can be more precise than the frame it saw.

    At scale 1 this is zero.
    """
    ix, iy = frame.to_image(screen_x, screen_y)
    bx, by = frame.to_screen(int(round(ix)), int(round(iy)))
    return max(abs(bx - screen_x), abs(by - screen_y))
