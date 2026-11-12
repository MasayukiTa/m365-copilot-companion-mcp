import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path
from ._untrusted import wrap_if_content

_log = logging.getLogger(__name__)

# EasyOCR reader cache. On the production path the Reader is built INSIDE a child
# process (see _run_easyocr_child), so this process no longer caches readers there.
# The dict is kept for backward compatibility: a caller (or a test) may pre-seed a
# reader here, in which case _easyocr_image_text runs it in-process on the single
# dedicated OCR thread -- the legacy, torch-free path that existing thread-leak
# regressions pin.
_EASYOCR_READERS: dict = {}

# Crash isolation (2026-11-12). EasyOCR runs on PyTorch, whose native layer (c10.dll)
# crashed the gateway's python.exe with an access violation (exception 0xc0000005,
# offset 0x8f5a4) three times on 2026-11-12 (17:58:59, 19:22:13, 19:32:03), each taking
# the public endpoint down for 1-4 minutes. A native crash cannot be caught by a Python
# try/except -- it takes the whole process with it, so the previous in-process fix (a
# single dedicated worker thread) could stop the THREAD leak but could not stop a crash
# from killing the server.
#
# Fix: run every real EasyOCR call in a short-lived CHILD process (tools/_easyocr_child.py).
# If torch crashes, only the child dies; the parent sees a non-zero exit code, logs it,
# returns None, and the caller falls back to Tesseract. The gateway process never imports
# torch at all.
#
# Concurrency: at most ONE child runs at a time (this machine is RAM-constrained, and the
# old design also funnelled all OCR through a single worker). A semaphore serialises child
# launches; a blocked caller waits rather than spawning a second torch process.
_OCR_CHILD_TIMEOUT_S = 120
_OCR_CHILD_SEM = threading.Semaphore(1)

# Legacy single-worker executor. Still used for the in-process reader path (pre-seeded
# _EASYOCR_READERS), which keeps the existing thread-leak regressions meaningful: when a
# reader is supplied in-process, its readtext must run on this one thread, never on the
# calling (anyio worker) thread. The production path no longer touches it (work runs in a
# child process).
_OCR_EXECUTOR: Optional[ThreadPoolExecutor] = None
_OCR_EXECUTOR_LOCK = threading.Lock()


def _ocr_executor() -> ThreadPoolExecutor:
    """The one worker thread that in-process EasyOCR/torch work runs on (see module note).

    Lazily created, single worker, so a pre-seeded in-process Reader builds its native
    pool once and reuses it instead of leaking one per anyio worker thread.
    """
    global _OCR_EXECUTOR
    if _OCR_EXECUTOR is None:
        with _OCR_EXECUTOR_LOCK:
            if _OCR_EXECUTOR is None:
                _OCR_EXECUTOR = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="ocr-easyocr"
                )
    return _OCR_EXECUTOR


# The child script that imports torch/easyocr and does the actual recognition.
_EASYOCR_CHILD = str(Path(__file__).with_name("_easyocr_child.py"))

# Windows: do not pop up a console window for the child. 0 elsewhere.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run_easyocr_child(image_path: str, langs: list,
                       timeout: float = _OCR_CHILD_TIMEOUT_S) -> Optional[str]:
    """Run EasyOCR on image_path in a child process and return its text, or None.

    Returns None (never raises) on every failure path so the caller can fall back to
    Tesseract: the child could not be launched, the child exited non-zero (including a
    native 0xc0000005 crash), it printed no parseable JSON line, it reported ok=false,
    or it ran past the timeout (in which case the child -- and only the child -- is
    killed). At most one child runs at a time (RAM-constrained host).
    """
    langs_csv = ",".join(langs)
    with _OCR_CHILD_SEM:
        try:
            proc = subprocess.Popen(
                [sys.executable, _EASYOCR_CHILD, image_path, langs_csv],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:
            _log.warning("easyocr child could not start: %s", exc)
            return None
        try:
            out, _err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=10)
            except Exception:
                pass
            _log.warning("easyocr child timed out after %ss; killed", timeout)
            return None
        except Exception as exc:
            try:
                proc.kill()
            except Exception:
                pass
            _log.warning("easyocr child read failed: %s", exc)
            return None

        rc = proc.returncode
        if rc != 0:
            # Non-zero includes native crashes. On Windows an access violation surfaces as
            # a large unsigned/negative code (0xC0000005); log it so the crash leaves a
            # one-line trace and the server keeps running.
            _log.warning("easyocr child exited with code %s (0x%08X); falling back",
                         rc, rc & 0xFFFFFFFF)
            return None

        lines = (out or b"").decode("utf-8", "replace").strip().splitlines()
        if not lines:
            _log.warning("easyocr child produced no output; falling back")
            return None
        try:
            data = json.loads(lines[-1])
        except Exception as exc:
            _log.warning("easyocr child output was not JSON: %s", exc)
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            if isinstance(data, dict) and data.get("error"):
                _log.warning("easyocr child reported: %s", data.get("error"))
            return None
        text = data.get("text") or ""
        return text or None


def _wants_japanese(lang: str) -> bool:
    return any(part.strip().lower() in ("jpn", "jp", "ja", "japanese")
               for part in str(lang).replace(",", "+").split("+"))


def _has_cjk(text: str) -> bool:
    """True when the text contains at least one Japanese/CJK character."""
    for ch in text or "":
        code = ord(ch)
        if (0x3040 <= code <= 0x30FF        # hiragana + katakana
                or 0x4E00 <= code <= 0x9FFF  # CJK ideographs
                or 0x3400 <= code <= 0x4DBF):
            return True
    return False


def _tesseract_failed_japanese(text: str, lang: str) -> bool:
    """True when Japanese was asked for but Tesseract clearly did not read it.

    Without the `jpn` language pack Tesseract does not fail and does not return
    empty -- it transliterates the glyphs into whatever Latin shapes fit, so a
    column reading the characters for good/good/caution comes back as "R/R/aa".
    That output is confidently wrong, which is worse than empty: it looks like a
    successful read, so nothing downstream questions it. Treat "Japanese requested
    but no CJK produced" as a failed read and let the fallback engine handle it.
    """
    return _wants_japanese(lang) and not _has_cjk(text)


def _easyocr_langs(lang: str) -> list:
    """Map a Tesseract language string ("jpn+eng") to EasyOCR codes (["ja", "en"])."""
    codes = {"jpn": "ja", "jp": "ja", "japanese": "ja",
             "eng": "en", "en": "en", "english": "en"}
    langs = [codes.get(part.strip().lower(), part.strip().lower())
             for part in str(lang).replace(",", "+").split("+") if part.strip()]
    langs = [l for i, l in enumerate(langs) if l and l not in langs[:i]] or ["en"]
    if "ja" in langs and "en" not in langs:
        langs.append("en")   # EasyOCR pairs Japanese with English
    return langs


def _easyocr_image_text(im, lang: str) -> Optional[str]:
    """OCR an already-open PIL image with EasyOCR. None on failure.

    Two paths, chosen by whether a Reader has been pre-seeded into _EASYOCR_READERS:

    * Pre-seeded (legacy / test path): the supplied Reader is run IN-PROCESS on the one
      dedicated OCR worker thread (_ocr_executor). This keeps the thread-leak
      regressions meaningful and lets callers inject a fake Reader without spawning a
      child. No torch is imported by this process on this path.

    * Not seeded (production path): EasyOCR/torch runs in a separate, short-lived CHILD
      process, never in the gateway process. A native torch crash (0xc0000005) kills
      only the child; this function then returns None and the caller falls back to
      Tesseract. At most one child runs at a time. The image is written to an ASCII-named
      temporary PNG under the system temp dir (EasyOCR reads paths through OpenCV, whose
      Windows imread cannot open non-ASCII paths) and the child is handed the PATH.
    """
    langs = _easyocr_langs(lang)
    key = "+".join(sorted(langs))

    reader = _EASYOCR_READERS.get(key)
    if reader is not None:
        # Legacy in-process path: run the pre-seeded Reader on the one dedicated thread.
        try:
            import numpy as np
        except ImportError:
            return None

        def _run() -> Optional[str]:
            try:
                arr = np.array(im.convert("RGB"))
                parts = reader.readtext(arr, detail=0)
                text = "\n".join(s for s in (str(x).strip() for x in parts) if s)
                return text or None
            except Exception:
                return None

        try:
            return _ocr_executor().submit(_run).result()
        except Exception:
            return None

    # Production path: isolate EasyOCR/torch in a child process.
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="ocr_")
        os.close(fd)
        try:
            im.convert("RGB").save(tmp_path, format="PNG")
        except Exception:
            return None
        return _run_easyocr_child(tmp_path, langs)
    except Exception:
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _easyocr_text(p: Path, lang: str) -> Optional[str]:
    """OCR with EasyOCR, or None when it is unavailable / finds nothing.

    Why this exists: the Tesseract path needs a per-language .traineddata file, and
    a stock Windows install ships English only -- so Japanese silently comes back
    EMPTY rather than as an error. An agent that only sees "" cannot tell "no text
    here" from "this engine cannot read this script", and in practice it then fills
    the gap by INFERRING values from surrounding data and reports them as if they
    were read. EasyOCR bundles its own Japanese model, so it turns that silent gap
    into an actual reading.

    The image is opened with Pillow and handed to _easyocr_image_text, which routes it
    to a child process (production) or a pre-seeded in-process Reader (tests).
    """
    try:
        from PIL import Image as PILImage
    except ImportError:
        return None
    try:
        with PILImage.open(p) as im:
            im.load()
            return _easyocr_image_text(im, lang)
    except Exception:
        return None


def ocr_image(
    path: str,
    lang: str = "jpn+eng",
    psm: int = 3,
) -> str:
    """Extract text from an image using Tesseract OCR.

    Use this when you need plain text from a screenshot, scan, or photo and the
    image contains many strings. For general-purpose visual interpretation
    (e.g. understanding a chart or diagram), `read_image` plus the agent's
    vision capability is usually more flexible and needs no extra install.

    Args:
        path: Image path under the allowed base.
        lang: Tesseract language code(s), e.g. "jpn+eng" (default), "eng", "jpn".
            The matching .traineddata must exist in the Tesseract install.
        psm: Page Segmentation Mode (Tesseract --psm). Common values:
            3 = auto with OSD (default), 6 = uniform block of text,
            11 = sparse text, 12 = sparse text with OSD.

    Engines:
        Tesseract runs first. If it is missing, or returns nothing (which is what a
        default Windows install does for Japanese, because only the English
        .traineddata ships), EasyOCR runs as a fallback -- it carries its own
        Japanese model, so no extra language pack is needed. EasyOCR runs in a child
        process, so a native torch crash cannot take the gateway down.

    Optional:
        - Tesseract binary on PATH (fastest path; Windows: install from
          https://github.com/UB-Mannheim/tesseract/wiki and add to PATH)
        - `jpn.traineddata` to let Tesseract itself handle Japanese
    """
    try:
        try:
            import pytesseract
        except ImportError:
            return "[ocr_image error: pytesseract not installed]"

        p = _validate_path(path)
        if not p.is_file():
            return f"[ocr_image error: not a file: {p}]"

        try:
            from PIL import Image as PILImage
        except ImportError:
            return "[ocr_image error: Pillow not installed]"

        text = ""
        try:
            with PILImage.open(p) as im:
                im.load()
                config = f"--psm {int(psm)}"
                text = pytesseract.image_to_string(im, lang=lang, config=config)
        except pytesseract.TesseractNotFoundError:
            text = ""   # no binary at all -- EasyOCR below may still handle it
        except Exception:
            # Missing .traineddata for the requested language raises here. Do not give
            # up yet: EasyOCR carries its own models and is tried next.
            text = ""

        text = (text or "").strip()
        if text and not _tesseract_failed_japanese(text, lang):
            return wrap_if_content(text, source="ocr", origin=str(p))

        # Either nothing came back, or Japanese was requested and the result contains
        # no Japanese at all (the language pack is missing and the glyphs were
        # transliterated into Latin). Both mean "this engine may not have read the
        # page". Try the fallback, but keep whatever Tesseract produced: an image
        # that genuinely holds only Latin text also lands here, and its correct
        # result must not be thrown away just because no Japanese appeared.
        alt = _easyocr_text(p, lang)
        if alt and (not text or _has_cjk(alt)):
            return wrap_if_content(alt, source="ocr", origin=str(p))
        return wrap_if_content(text or "(no text recognized)", source="ocr", origin=str(p))
    except Exception as e:
        return f"[ocr_image error: {type(e).__name__}: {e}]"


def ocr_pdf(path: str, pages: Optional[str] = None, lang: str = "jpn+eng") -> str:
    """OCR a PDF by rendering each requested page to an image, then running Tesseract.

    Useful for scanned (image-only) PDFs where read_pdf returns nothing.
    Requires Tesseract and pdf2image (which itself requires Poppler on PATH).

    Args:
        path: PDF path under the allowed base.
        pages: Page spec like "1-3,5". Omit for all pages.
        lang: Tesseract language(s).
    """
    try:
        try:
            from pdf2image import convert_from_path  # type: ignore
        except ImportError:
            return (
                "[ocr_pdf error: pdf2image not installed. "
                "Run: pip_install(packages=['pdf2image']). "
                "Also requires Poppler on PATH "
                "(https://github.com/oschwartz10612/poppler-windows/releases).]"
            )

        try:
            import pytesseract
        except ImportError:
            return "[ocr_pdf error: pytesseract not installed]"

        p = _validate_path(path)
        if not p.is_file():
            return f"[ocr_pdf error: not a file: {p}]"

        first_page, last_page = None, None
        if pages:
            try:
                if "-" in pages:
                    a, b = pages.split(",")[0].split("-")
                    first_page, last_page = int(a), int(b)
                else:
                    first_page = int(pages.split(",")[0])
                    last_page = first_page
            except ValueError:
                return "[ocr_pdf error: pages must look like '1-3' or '2']"

        images = convert_from_path(str(p), first_page=first_page, last_page=last_page)
        chunks: list[str] = []
        for i, im in enumerate(images, 1):
            try:
                text = pytesseract.image_to_string(im, lang=lang).strip()
            except pytesseract.TesseractNotFoundError:
                text = ""
            except Exception:
                text = ""   # e.g. the requested language pack is not installed
            if not text:
                # Same reasoning as ocr_image: an empty result from Tesseract does not
                # distinguish "blank page" from "cannot read this script", and Japanese
                # is unreadable on a default install. Fall back to EasyOCR, which brings
                # its own models, before declaring the page empty.
                text = _easyocr_image_text(im, lang) or ""
            label = first_page + i - 1 if first_page else i
            chunks.append(f"--- page {label} ---\n{text or '(empty)'}")
        return wrap_if_content("\n\n".join(chunks), source="ocr_pdf", origin=str(p))
    except Exception as e:
        return f"[ocr_pdf error: {type(e).__name__}: {e}]"
