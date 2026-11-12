"""Run EasyOCR in a throwaway child process, isolated from the gateway.

WHY THIS FILE EXISTS. EasyOCR runs on PyTorch, whose native layer (c10.dll) has
crashed the gateway's python.exe with an access violation (0xc0000005) -- three
times on 2026-11-12, each taking the public endpoint down for 1-4 minutes. A
native crash cannot be caught by a Python ``try``: it takes the whole process
with it. Moving the EasyOCR call into a separate, short-lived child process means
a crash kills only the child; the parent reads a non-zero exit code and falls
back (to Tesseract), and the server stays up.

CONTRACT. Two invocation shapes are accepted.

  New (batch), langs FIRST then one or more image paths::

      python -m tools._easyocr_child <lang1,lang2,...> <image1> [<image2> ...]

  Legacy (single image), image FIRST then langs::

      python -m tools._easyocr_child <image_path> <lang1,lang2,...>

(or run as a bare script with the same argv). The model (``easyocr.Reader``) is
built ONCE, then every image is recognised in order. For EACH image, exactly one
line of JSON is written to stdout, in input order: ``{"ok": true, "text": "..."}``
on success (text may be empty) or ``{"ok": false, "error": "..."}`` when that
image could not be read. One image failing does not stop the rest. If the Reader
itself cannot be built (easyocr/numpy/pillow missing, or Reader construction
fails), one ``{"ok": false, "error": ...}`` line is written PER image so the
parent still gets one line per input. The parent treats a missing/invalid line,
a non-zero exit, or a crash all the same way: no usable text, fall back. Nothing
but those JSON lines is written to stdout; diagnostics go to stderr.

The heavy imports (easyocr, torch, numpy, PIL) happen HERE, in the child, so the
gateway process never imports torch at all.
"""
import json
import sys

# EasyOCR language codes we may be handed. Used only to tell the two argv shapes
# apart when there are exactly two arguments (see _parse_argv): a token that is a
# language CSV cannot also be an image path.
_KNOWN_LANG_CODES = {
    "en", "ja", "ch_sim", "ch_tra", "ko", "fr", "de", "es", "it", "pt",
    "ru", "ar", "hi", "th", "vi", "nl", "pl", "tr", "id", "ms",
}


def _looks_like_langs(token: str) -> bool:
    """True when ``token`` looks like a language CSV rather than an image path.

    A langs token is a comma-separated list of short alpha(/underscore) codes.
    An image path has an extension, a separator, or a drive letter. We only need
    this to disambiguate the 2-argument case; 3+ args are unambiguously the new
    (langs-first) shape, and it is checked there.
    """
    t = (token or "").strip()
    if not t:
        return False
    parts = [p.strip() for p in t.split(",") if p.strip()]
    if not parts:
        return False
    for p in parts:
        pl = p.lower()
        if pl in _KNOWN_LANG_CODES:
            continue
        # Any code made only of ascii letters/underscore, length <= 6, with no path
        # marker, is plausibly a language code (e.g. a future EasyOCR code).
        if len(p) <= 6 and all(c.isalpha() or c == "_" for c in p):
            continue
        return False
    return True


def _parse_argv(argv: list):
    """Return (langs, image_paths) from argv, or (None, None) when unparseable.

    Accepts both shapes:
      * langs-first (new):  [langs_csv, img1, img2, ...]  (2+ images -> definitely this)
      * image-first (legacy): [img, langs_csv]            (exactly 2, 2nd is langs)

    When exactly two tokens are given, the shape is decided by which token looks
    like a language CSV. If the FIRST looks like langs, it is langs-first; else if
    the SECOND looks like langs, it is the legacy image-first shape; otherwise we
    assume langs-first (first token is langs) as the documented primary contract.
    """
    argv = list(argv)
    if len(argv) < 2:
        return None, None
    if len(argv) >= 3:
        # 3+ tokens: langs first, then images. (A legacy call only ever had 2.)
        langs = _split_langs(argv[0])
        return langs, list(argv[1:])
    # exactly two tokens
    first, second = argv[0], argv[1]
    if _looks_like_langs(first):
        return _split_langs(first), [second]
    if _looks_like_langs(second):
        # legacy image-first shape
        return _split_langs(second), [first]
    # Neither clearly looks like langs; follow the documented primary contract
    # (langs first).
    return _split_langs(first), [second]


def _split_langs(langs_csv: str) -> list:
    langs = [p.strip() for p in (langs_csv or "").split(",") if p.strip()]
    return langs or ["en"]


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _build_reader(langs: list):
    """Build the EasyOCR Reader once. Returns (reader, np_module) or raises."""
    import numpy as np  # noqa: F401 -- also validates numpy is importable
    import easyocr
    reader = easyocr.Reader(langs, gpu=False, verbose=False)
    return reader, np


def _read_one(reader, np, image_path: str) -> dict:
    """Recognise a single already-validated image path. Never raises."""
    try:
        from PIL import Image as PILImage
    except Exception as exc:  # pragma: no cover - environment-dependent
        return {"ok": False, "error": "pillow unavailable: %s" % exc}
    try:
        with PILImage.open(image_path) as im:
            im.load()
            arr = np.array(im.convert("RGB"))
    except Exception as exc:
        return {"ok": False, "error": "image load failed: %s" % exc}
    try:
        parts = reader.readtext(arr, detail=0)
        text = "\n".join(s for s in (str(x).strip() for x in parts) if s)
        return {"ok": True, "text": text}
    except Exception as exc:
        return {"ok": False, "error": "easyocr failed: %s" % exc}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    langs, image_paths = _parse_argv(argv)
    if not image_paths:
        _emit({"ok": False,
               "error": "usage: _easyocr_child <langs-csv> <image> [<image> ...]"})
        return 2

    # Build the Reader once. If that fails, every image gets one error line so the
    # parent still reads exactly one line per input image.
    try:
        reader, np = _build_reader(langs)
    except Exception as exc:
        for _ in image_paths:
            _emit({"ok": False, "error": "easyocr reader unavailable: %s" % exc})
        return 0

    for image_path in image_paths:
        _emit(_read_one(reader, np, image_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
