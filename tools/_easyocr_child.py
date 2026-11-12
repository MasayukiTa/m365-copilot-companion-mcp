"""Run EasyOCR in a throwaway child process, isolated from the gateway.

WHY THIS FILE EXISTS. EasyOCR runs on PyTorch, whose native layer (c10.dll) has
crashed the gateway's python.exe with an access violation (0xc0000005) -- three
times on 2026-11-12, each taking the public endpoint down for 1-4 minutes. A
native crash cannot be caught by a Python ``try``: it takes the whole process
with it. Moving the EasyOCR call into a separate, short-lived child process means
a crash kills only the child; the parent reads a non-zero exit code and falls
back (to Tesseract), and the server stays up.

CONTRACT. Invoked as::

    python -m tools._easyocr_child <image_path> <lang1,lang2,...>

(or run as a bare script with the same argv). Reads the image at ``image_path``,
runs EasyOCR for the given EasyOCR language codes, and writes exactly ONE line of
JSON to stdout: ``{"ok": true, "text": "..."}`` on success (text may be empty),
or ``{"ok": false, "error": "..."}`` when EasyOCR is unavailable or failed. The
parent treats a missing/invalid line, a non-zero exit, or a crash all the same
way: no usable text, fall back. Nothing but that one JSON line is written to
stdout; diagnostics go to stderr.

The heavy imports (easyocr, torch, numpy, PIL) happen HERE, in the child, so the
gateway process never imports torch at all.
"""
import json
import sys


def _run(image_path: str, langs: list) -> dict:
    try:
        from PIL import Image as PILImage
    except Exception as exc:  # pragma: no cover - environment-dependent
        return {"ok": False, "error": "pillow unavailable: %s" % exc}
    try:
        import numpy as np
    except Exception as exc:  # pragma: no cover - environment-dependent
        return {"ok": False, "error": "numpy unavailable: %s" % exc}
    try:
        import easyocr
    except Exception as exc:  # pragma: no cover - environment-dependent
        return {"ok": False, "error": "easyocr unavailable: %s" % exc}

    try:
        with PILImage.open(image_path) as im:
            im.load()
            arr = np.array(im.convert("RGB"))
    except Exception as exc:
        return {"ok": False, "error": "image load failed: %s" % exc}

    try:
        reader = easyocr.Reader(langs, gpu=False, verbose=False)
        parts = reader.readtext(arr, detail=0)
        text = "\n".join(s for s in (str(x).strip() for x in parts) if s)
        return {"ok": True, "text": text}
    except Exception as exc:
        return {"ok": False, "error": "easyocr failed: %s" % exc}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        sys.stdout.write(json.dumps(
            {"ok": False, "error": "usage: _easyocr_child <image_path> <langs-csv>"}))
        sys.stdout.write("\n")
        return 2
    image_path = argv[0]
    langs = [p.strip() for p in argv[1].split(",") if p.strip()] or ["en"]
    result = _run(image_path, langs)
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
