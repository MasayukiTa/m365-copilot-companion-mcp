# -*- coding: utf-8 -*-
"""The screen as something an agent can act on: look, then click in what it saw.

THE MODEL IS NEVER TOLD THE SCALE, AND NEVER HAS TO BE. It looks at an image and
names a pixel in that image. `screen_click` converts, because it kept the frame the
image was captured in. Nothing in the agent's half of the conversation involves an
origin, a scale, or a monitor layout, so none of those can be got wrong there.

WHY THAT BOUNDARY AND NOT THE OBVIOUS ONE. The obvious design hands the agent a
screenshot and takes desktop coordinates back. Measured on this machine, that
design cannot work: `screenshot` saves a picture and reports only
"saved screenshot: <path> (1920x1086 px)" -- it does not say that the desktop is
3840x2173, that its top-left corner is at (-985, -1093) because a monitor sits
above and to the left of the primary one, or that anything wider than 1920 was
silently halved on the way to the file. `read_image` then reduces it again to 1600.
Two compounding scale factors and a thousand-pixel origin offset, none of them
stated anywhere the agent could read. Coordinates returned through that path are
not slightly wrong, they are wrong by more than the width of most windows -- and a
grounding accuracy measured through it would be measuring the losses rather than
the model.

So the frame travels with the image, in a sidecar file next to it. A file rather
than process memory because the agent and the executor are different processes, and
often different machines' worth of restarts apart.

EVERY ACTION RE-CHECKS THE DISPLAY LAYOUT (tools/desktop_input.py does the
checking) and refuses when it has changed since the capture. Displays on this
machine come and go; a coordinate read before one was unplugged names a place that
no longer exists, and nothing in the coordinate says so. Capturing again is cheap;
a click somewhere unintended is not.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path
from .security import require_unlocked

#: Written next to the image. Name chosen so it is obvious what it belongs to and
#: obvious that deleting it only costs the ability to click, not the picture.
FRAME_SUFFIX = ".frame.json"


def _frame_path(image_path: Path) -> Path:
    return image_path.with_name(image_path.name + FRAME_SUFFIX)


def _load_frame(image: str):
    from .screen_frame import Frame

    p = Path(_validate_path(image))
    side = _frame_path(p)
    if not side.is_file():
        raise FileNotFoundError(
            "no frame recorded for %s. A picture on its own does not say where on the "
            "desktop its pixels are; call screen_look to take one that does." % p.name)
    d = json.loads(side.read_text(encoding="utf-8"))
    return Frame(d["origin_x"], d["origin_y"], d["width"], d["height"],
                 d["image_width"], d["image_height"]).validate()


def screen_look(output_path: Optional[str] = None, max_dimension: int = 1600,
                window: str = "") -> str:
    """Capture the screen for the agent to act on, and record what its pixels mean.

    Use this instead of `screenshot` whenever something is going to CLICK on what it
    sees. `screenshot` is for a picture a person looks at; it reports no coordinate
    frame, so coordinates read off its output cannot be converted back.

    Report positions as pixels IN THE RETURNED IMAGE and pass them to screen_click /
    screen_scroll together with this image's path. Do not convert to desktop
    coordinates; the conversion is held here, with the frame this capture was taken
    in, and doing it twice is how a click ends up on the wrong monitor.

    Args:
        output_path: Where to save the PNG. Defaults to a timestamped file under
            ~/Desktop/screenshots/.
        max_dimension: Longest edge of the saved image. The reduction is recorded,
            so a click is still exact to within the block one image pixel stands
            for; that block size is stated in the reply.
        window: Capture ONE window instead of the whole desktop -- a substring of its
            title, matched against what is on screen. Prefer this. A picture is the
            most expensive thing that can enter a conversation, and this desktop is
            3840x2173; capturing all of it to look at one dialog spends the budget on
            wallpaper. Measured 2026-09-15: workers that looked at the screen ran out
            of conversation in 9 of 11 runs, after two to seven turns, against 4 of 24
            for workers that looked at nothing.

    Before reaching for a picture at all, consider screen_windows: it answers what is
    open, what is in front, and where each window is, in a few lines of text. Most
    questions of the form "did X open" are answered there for a hundredth of the cost.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        from .screen_capture import capture, capture_reports_what_it_did

        if output_path is None:
            output_path = str(Path.home() / "Desktop" / "screenshots"
                              / ("look-%s.png" % time.strftime("%Y%m%d-%H%M%S")))
        out = Path(_validate_path(output_path))
        if out.suffix.lower() != ".png":
            return "[screen_look error: output_path must be .png]"
        out.parent.mkdir(parents=True, exist_ok=True)

        region = None
        picked = ""
        warn = []
        want = (window or "").strip()
        if want:
            from . import window_probe as W

            # Front to back, so an ambiguous substring picks the one the operator is
            # actually looking at rather than whichever happens to be enumerated first.
            hits = [t for t in W.top_level_windows()
                    if want.lower() in (t.title or "").lower()]
            if not hits:
                return ("[screen_look: no window whose title contains %r. Call "
                        "screen_windows to see what is open -- it is text, and it "
                        "answers most questions without a picture at all.]" % want)
            t = hits[0]
            region = (t.left, t.top, t.right, t.bottom)
            picked = t.title or t.cls
            # AN AMBIGUOUS MATCH IS STILL A MATCH, AND THE CALLER IS NOW TOLD. Taking the
            # front-most is the right choice -- it is the one a person would mean -- but it
            # was taken in silence, so an agent asking for "設定" with two such windows open
            # got one of them and reported on it with no idea the other existed. astra named
            # this class directly: judging an operation by a window title alone misjudges it.
            # Naming the runners-up costs a line of text and removes the confident wrong
            # answer; it does not change which window is captured.
            if len(hits) > 1:
                warn.append("%r matched %d windows; captured the front-most (%s). The others: %s"
                            % (want, len(hits), picked,
                               ", ".join((h.title or h.cls or "?")[:40] for h in hits[1:4])))

            # WHAT THE CROP CANNOT SEE, SAID OUT LOUD.
            #
            # Cropping to one window buys the saving by looking away from the rest of the
            # screen, and the coordinate transform being correct says nothing about the
            # observation being sufficient. The case that matters is a dialog: it belongs
            # to a different window, it is usually what is actually blocking the work, and
            # a picture of the window underneath it shows a screen that looks fine.
            #
            # So the reply names anything sitting on top of the target and any dialog
            # anywhere, rather than leaving the reader to assume the picture is the whole
            # story. Cheap -- the same enumeration that found the window.
            in_front = []
            for other in W.top_level_windows():
                if other.hwnd == t.hwnd:
                    break                      # z-order: everything after this is behind
                if (other.left < t.right and other.right > t.left
                        and other.top < t.bottom and other.bottom > t.top):
                    in_front.append(other.title or other.cls)
            dialogs = [w.title or w.cls for w in W.top_level_windows(min_side=80)
                       if w.cls == "#32770" and w.hwnd != t.hwnd]
            if in_front:
                warn.append("%d window(s) overlap it and are in front: %s"
                            % (len(in_front), ", ".join(x[:24] for x in in_front[:3])))
            if dialogs:
                warn.append("a dialog is open elsewhere on screen: %s"
                            % ", ".join(x[:24] for x in dialogs[:3]))

        img, frame = capture(max_dimension=max(0, int(max_dimension)), region=region)
        img.save(out, optimize=True)
        _frame_path(out).write_text(json.dumps(frame._asdict()), encoding="utf-8")

        block = ("each image pixel covers %.2f desktop pixels, so a click is exact to "
                 "within that" % frame.scale) if frame.is_downscaled else \
                "image pixels are desktop pixels here"
        scope = ("window 「%s」" % picked[:40]) if picked else "the whole desktop"
        # Appended rather than folded into `scope`, so that a reader who skims the first
        # sentence still meets it, and a reader who does not skim cannot miss it.
        caution = ("  CAUTION: this picture shows only that window -- " + "; ".join(warn)
                   + ". Use screen_windows, or capture without `window`, before concluding "
                     "from it.") if warn else ""
        return ("saved %s -- %s (%dx%d px, %s bytes). Give positions as pixels in THIS "
                "image and pass them to screen_click with image='%s' -- %s. [%s]"
                % (out, scope, frame.image_width, frame.image_height,
                   format(out.stat().st_size, ","), out, block,
                   capture_reports_what_it_did()) + caution)
    except Exception as e:
        return _unavailable_or_error("screen_look", e)


def _unavailable_or_error(tool: str, exc: BaseException) -> str:
    """The failure text for a tool that needs a desktop, split by whether one was there.

    Two different words because they have two different readers. `error` is a defect and
    somebody should look at the code; `unavailable` is a machine nobody is sitting at, and
    the only fix is a person. Filing the second as the first is how a health indicator comes
    to report a working system as broken -- and every one of these tools was doing it.

    Only for the paths that genuinely cannot work without the desktop. screen_windows
    enumerates fine on a locked session, so a failure there is its own.
    """
    from . import desktop_input as DI

    try:
        reason = DI.unreachable_desktop_reason()
    except Exception:
        reason = ""          # the probe failing is not evidence about the desktop
    if reason:
        return "[%s unavailable: %s]" % (tool, reason)
    return "[%s error: %s: %s]" % (tool, type(exc).__name__, exc)


def screen_click(image: str, x: int, y: int, button: str = "left",
                 count: int = 1) -> str:
    """Click at a pixel of an image taken by screen_look.

    Args:
        image: The path screen_look returned.
        x, y: The position IN THAT IMAGE, not on the desktop.
        button: left, right or middle.
        count: 1 for a click, 2 for a double click.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        from . import desktop_input as DI
        from . import window_probe as W

        frame = _load_frame(image)
        sx, sy = frame.to_screen(int(x), int(y))
        before = W.window_at(sx, sy)
        DI.click(frame, sx, sy, button=button, count=int(count))
        # What was there is reported back because a click that landed somewhere
        # unexpected is only visible if somebody says where it landed. An agent that
        # is told "clicked" and nothing else will keep going on a wrong premise.
        return ("%s click at image (%d, %d) -> desktop (%d, %d); what was there: %s"
                % (button, x, y, sx, sy, before.label() if before else "nothing"))
    except Exception as e:
        return _unavailable_or_error("screen_click", e)


def screen_scroll(image: str, x: int, y: int, clicks: int = -3,
                  horizontal: bool = False) -> str:
    """Scroll at a pixel of a screen_look image. Negative clicks scroll down."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        from . import desktop_input as DI

        frame = _load_frame(image)
        sx, sy = frame.to_screen(int(x), int(y))
        DI.scroll(frame, sx, sy, int(clicks), horizontal=bool(horizontal))
        return ("scrolled %d notch(es) %s at image (%d, %d) -> desktop (%d, %d)"
                % (clicks, "horizontally" if horizontal else "vertically", x, y, sx, sy))
    except Exception as e:
        return _unavailable_or_error("screen_scroll", e)


def screen_type(text: str) -> str:
    """Type text into whatever has keyboard focus.

    The characters are sent as characters, not as key presses, so the result does
    not depend on the keyboard layout and the IME is not involved -- Japanese
    arrives as written. Click the field first; this types where focus already is.
    A newline in `text` is sent as the Enter key, and a tab as Tab.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        from . import desktop_input as DI

        n = DI.type_text(text or "")
        return "typed %d character(s)" % n
    except Exception as e:
        return _unavailable_or_error("screen_type", e)


def screen_press(keys: str) -> str:
    """Press a key or a combination: "enter", "ctrl s", "ctrl shift tab", "win d".

    The whole keyboard is available -- letters, digits, f1-f24, arrows, punctuation
    named by the character on the key, the numpad, and the modifiers. This used to be
    a short hand-picked list, which meant Win+M could not be pressed because nobody
    had needed `m` yet, and the caller could not tell that from a rule.

    Four combinations are refused and say so: win+l, alt+f4, ctrl+alt+delete and
    ctrl+shift+escape, each of which ends the operator's session or their unsaved
    work. To close something, use ctrl+w. Anything else that comes back "unknown key"
    is a gap, not a policy -- report it.

    Modifiers are released in reverse order, so nothing is left held down afterwards:
    a stuck Ctrl turns the operator's next keystroke into a shortcut, and nothing on
    screen says why.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        from . import desktop_input as DI

        parts = [p for p in str(keys or "").replace(",", " ").split() if p]
        if not parts:
            return "[screen_press error: no keys given]"
        DI.press(*parts)
        return "pressed %s" % "+".join(parts)
    except Exception as e:
        return _unavailable_or_error("screen_press", e)


def screen_windows(limit: int = 30) -> str:
    """List the visible windows with their position and size, front to back.

    Reading only. Useful before looking: it says what is open and which window is
    in front, which is often enough to decide what to do without a picture at all.
    """
    try:
        from . import window_probe as W

        rows = W.top_level_windows()
        fg = W.foreground_window()
        out = []
        for t in rows[:max(1, int(limit))]:
            out.append("%s%-40s %5dx%-5d at (%d, %d)"
                       % ("*" if fg and t.hwnd == fg.hwnd else " ",
                          (t.title or t.cls)[:40], t.width, t.height, t.left, t.top))
        head = "%d visible windows (* = in front, front to back):" % len(rows)
        return head + "\n" + "\n".join(out) if out else "no visible windows"
    except Exception as e:
        return "[screen_windows error: %s: %s]" % (type(e).__name__, e)
