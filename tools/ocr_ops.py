from pathlib import Path
from typing import Optional

from .file_ops import _validate_path
from ._untrusted import wrap_if_content

# EasyOCR reader cache. Building a Reader loads the detection + recognition models
# (a few hundred MB the first time, then cached on disk), which takes ~10-20s, so we
# keep one per language set for the life of the process.
_EASYOCR_READERS: dict = {}


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


def _easyocr_image_text(im, lang: str) -> Optional[str]:
    """OCR an already-open PIL image with EasyOCR. None when unavailable/empty.

    Takes a numpy array rather than a path: EasyOCR loads paths through OpenCV,
    whose Windows imread returns None for any path containing non-ASCII characters
    (and then fails deep inside the library). Passing pixels sidesteps that entirely.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    langs = _easyocr_langs(lang)
    key = "+".join(sorted(langs))
    try:
        reader = _EASYOCR_READERS.get(key)
        if reader is None:
            import easyocr
            reader = easyocr.Reader(langs, gpu=False, verbose=False)
            _EASYOCR_READERS[key] = reader
        arr = np.array(im.convert("RGB"))
        parts = reader.readtext(arr, detail=0)
        text = "\n".join(s for s in (str(x).strip() for x in parts) if s)
        return text or None
    except Exception:
        return None


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
