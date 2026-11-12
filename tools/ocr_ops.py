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

# EasyOCR reader cache. Kept for backward compatibility only: the Reader is now built
# INSIDE the child process (see _easyocr_image_text), so this process no longer caches
# readers. Tests that pre-seed a fake reader here still find the attribute.
_EASYOCR_READERS: dict = {}

# Crash isolation (2026-11-12). EasyOCR runs on PyTorch, whose native layer (c10.dll)
# crashed the gateway's python.exe with an access violation (exception 0xc0000005,
# offset 0x8f5a4) three times on 2026-11-12 (17:58:59, 19:22:13, 19:32:03), each taking
# the public endpoint down for 1-4 minutes. A native crash cannot be caught by a Python
# try/except -- it takes the whole process with it, so the previous in-process fix (a
# single dedicated worker thread) could stop the THREAD leak but could not stop a crash
# from killing the server.
#
# Fix: run every EasyOCR call in a short-lived CHILD process (tools/_easyocr_child.py).
# If torch crashes, only the child dies; the parent sees a non-zero exit code, logs it,
# returns None, and the caller falls back to Tesseract. The gateway process never imports
# torch at all.
#
# Concurrency: at most ONE child runs at a time (this machine is RAM-constrained, and the
# old design also funnelled all OCR through a single worker). A semaphore serialises child
# launches; a blocked caller waits rather than spawning a second torch process.
_OCR_CHILD_TIMEOUT_S = 120
_OCR_CHILD_SEM = threading.Semaphore(1)

# Batch timeout budget (ocr_pdf): one child now OCRs several pages in a single run,
# so the timeout scales with the number of images -- per-image recognition plus the
# one-time model load the child pays up front -- bounded so a pathological PDF cannot
# hold the single OCR slot open indefinitely.
_OCR_PER_IMAGE_TIMEOUT_S = 60
_OCR_MODEL_LOAD_TIMEOUT_S = 90
_OCR_BATCH_TIMEOUT_CAP_S = 600


def _batch_timeout(n_images: int) -> float:
    """Timeout for a batch child: per-image budget + one model load, capped."""
    n = max(1, int(n_images))
    return float(min(_OCR_BATCH_TIMEOUT_CAP_S,
                     n * _OCR_PER_IMAGE_TIMEOUT_S + _OCR_MODEL_LOAD_TIMEOUT_S))

# Kept for backward compatibility. The executor is no longer on the OCR execution path
# (the work now runs in a child process), but the symbol is preserved so existing callers
# and tests that reference it keep importing.
_OCR_EXECUTOR: Optional[ThreadPoolExecutor] = None
_OCR_EXECUTOR_LOCK = threading.Lock()


def _ocr_executor() -> ThreadPoolExecutor:
    """Legacy single-worker executor. No longer on the OCR path (work runs in a child
    process now); retained for backward compatibility with callers that imported it."""
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


def _run_easyocr_child_batch(
    image_paths: list, langs: list, timeout: Optional[float] = None
) -> list:
    """Run EasyOCR on several images in ONE child process; return a text-or-None list.

    The returned list is the same length and order as ``image_paths``: each entry is
    the recognised text for that image, or None when that image produced no usable
    text. The whole list is None-filled (never raises) on every batch-level failure
    so the caller can fall back to Tesseract for every page: the child could not be
    launched, it exited non-zero (including a native 0xc0000005 crash), it timed out
    (the child -- and only the child -- is killed), or it printed no parseable output.
    A single image failing inside an otherwise-healthy child only Nones that one entry.

    The child is handed langs FIRST, then every image path, and prints one JSON line
    per image in input order (see tools/_easyocr_child.py). At most one child runs at
    a time (RAM-constrained host); the shared semaphore serialises launches.
    """
    paths = list(image_paths)
    n = len(paths)
    if n == 0:
        return []
    if timeout is None:
        timeout = _batch_timeout(n)
    langs_csv = ",".join(langs)
    none_list = [None] * n
    with _OCR_CHILD_SEM:
        try:
            proc = subprocess.Popen(
                [sys.executable, _EASYOCR_CHILD, langs_csv, *paths],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:
            _log.warning("easyocr child could not start: %s", exc)
            return none_list
        try:
            out, _err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=10)
            except Exception:
                pass
            _log.warning("easyocr child timed out after %ss; killed", timeout)
            return none_list
        except Exception as exc:
            try:
                proc.kill()
            except Exception:
                pass
            _log.warning("easyocr child read failed: %s", exc)
            return none_list

        rc = proc.returncode
        if rc != 0:
            # Non-zero includes native crashes. On Windows an access violation surfaces
            # as a large unsigned/negative code (0xC0000005); log it so the crash leaves
            # a one-line trace and the server keeps running. Every page falls back.
            _log.warning("easyocr child exited with code %s (0x%08X); falling back",
                         rc, rc & 0xFFFFFFFF)
            return none_list

        lines = [ln for ln in (out or b"").decode("utf-8", "replace").splitlines()
                 if ln.strip()]
        if not lines:
            _log.warning("easyocr child produced no output; falling back")
            return none_list

        results: list = []
        for idx in range(n):
            if idx >= len(lines):
                # Child stopped early (fewer lines than images): remaining pages fall back.
                results.append(None)
                continue
            try:
                data = json.loads(lines[idx])
            except Exception as exc:
                _log.warning("easyocr child output line %d was not JSON: %s", idx, exc)
                results.append(None)
                continue
            if not isinstance(data, dict) or not data.get("ok"):
                if isinstance(data, dict) and data.get("error"):
                    _log.warning("easyocr child reported for image %d: %s",
                                 idx, data.get("error"))
                results.append(None)
                continue
            text = data.get("text") or ""
            results.append(text or None)
        return results


def _run_easyocr_child(image_path: str, langs: list, timeout: float = _OCR_CHILD_TIMEOUT_S) -> Optional[str]:
    """Run EasyOCR on a single image in a child process and return its text, or None.

    Thin wrapper over _run_easyocr_child_batch for the one-image path (ocr_image). All
    the failure handling -- unlaunchable child, non-zero exit (including a native
    0xc0000005 crash), timeout, unparseable output, ok=false -- lives in the batch
    function and yields None here. At most one child runs at a time.
    """
    results = _run_easyocr_child_batch([image_path], langs, timeout=timeout)
    return results[0] if results else None


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
    column reading 良/良/注意 comes back as "R/R/aa". That output is confidently
    wrong, which is worse than empty: it looks like a successful read, so nothing
    downstream questions it. Treat "Japanese requested but no CJK produced" as a
    failed read and let the fallback engine handle it.
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


def _image_to_ascii_png(im) -> Optional[str]:
    """Write a PIL image to a fresh ASCII-named temp PNG; return its path or None.

    EasyOCR reads paths through OpenCV, whose Windows imread cannot open a non-ASCII
    path, so the temp file is created with an ASCII-only name under the system temp
    directory. The caller owns the returned file and must remove it. Returns None if
    the temp file could not be created or the image could not be saved (in which case
    no file is left behind).
    """
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="ocr_")
        os.close(fd)
        im.convert("RGB").save(tmp_path, format="PNG")
        return tmp_path
    except Exception:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return None


def _easyocr_image_text(im, lang: str) -> Optional[str]:
    """OCR an already-open PIL image with EasyOCR, in a child process. None on failure.

    Crash isolation (see the module note): EasyOCR/torch runs in a separate, short-lived
    child process, never in the gateway process. A native torch crash (0xc0000005) kills
    only the child; this function then returns None and the caller falls back to
    Tesseract. At most one child runs at a time.

    The image is written to a temporary PNG and the child is handed the PATH (see
    _image_to_ascii_png for why the name is ASCII-only).
    """
    langs = _easyocr_langs(lang)
    tmp_path = _image_to_ascii_png(im)
    if tmp_path is None:
        return None
    try:
        return _run_easyocr_child(tmp_path, langs)
    except Exception:
        return None
    finally:
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

    Images are decoded with Pillow and handed over as a numpy array on purpose:
    EasyOCR reads paths through OpenCV, whose Windows imread cannot open a path
    containing non-ASCII characters (it returns None and the call dies deep inside
    the library). Japanese file names are exactly the case this fallback is for.
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
        Japanese model, so no extra language pack is needed. EasyOCR's first call
        in a process loads its models (~10-20s); later calls reuse them.

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

        # First pass: Tesseract on every page. Record which pages need the EasyOCR
        # fallback (empty, or -- when Japanese was asked for -- read as Latin only,
        # which on a default install means the jpn pack is missing and the glyphs were
        # transliterated). Those pages are collected and sent to ONE child process
        # rather than paying the ~30s model load once per page: a 10-page scan went
        # from ~5 min (one child per page) to roughly one model load plus per-page
        # recognition.
        page_texts: list[str] = []
        fallback_indexes: list[int] = []
        for i, im in enumerate(images):
            try:
                text = pytesseract.image_to_string(im, lang=lang).strip()
            except pytesseract.TesseractNotFoundError:
                text = ""
            except Exception:
                text = ""   # e.g. the requested language pack is not installed
            page_texts.append(text)
            if not text or _tesseract_failed_japanese(text, lang):
                fallback_indexes.append(i)

        # Second pass: one batched EasyOCR child for every page Tesseract could not read.
        if fallback_indexes:
            tmp_paths: list[Optional[str]] = []
            try:
                for idx in fallback_indexes:
                    tmp_paths.append(_image_to_ascii_png(images[idx]))
                # Pages whose temp PNG could not be written stay on their Tesseract text;
                # only the ones that were written are handed to the child, in order.
                sendable = [(idx, tp) for idx, tp in zip(fallback_indexes, tmp_paths)
                            if tp is not None]
                if sendable:
                    langs = _easyocr_langs(lang)
                    alts = _run_easyocr_child_batch([tp for _idx, tp in sendable], langs)
                    for (idx, _tp), alt in zip(sendable, alts):
                        if alt and (not page_texts[idx] or _has_cjk(alt)):
                            page_texts[idx] = alt
            finally:
                for tp in tmp_paths:
                    if tp:
                        try:
                            os.remove(tp)
                        except OSError:
                            pass

        chunks: list[str] = []
        for i, text in enumerate(page_texts, 1):
            label = first_page + i - 1 if first_page else i
            chunks.append(f"--- page {label} ---\n{text or '(empty)'}")
        return wrap_if_content("\n\n".join(chunks), source="ocr_pdf", origin=str(p))
    except Exception as e:
        return f"[ocr_pdf error: {type(e).__name__}: {e}]"
