from pathlib import Path
from typing import Optional

from .file_ops import _validate_path


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

    Requires:
        - Tesseract binary on PATH (Windows: install from
          https://github.com/UB-Mannheim/tesseract/wiki and add to PATH)
        - For Japanese, install the `jpn` language pack (jpn.traineddata)
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

        try:
            with PILImage.open(p) as im:
                im.load()
                config = f"--psm {int(psm)}"
                text = pytesseract.image_to_string(im, lang=lang, config=config)
        except pytesseract.TesseractNotFoundError:
            return (
                "[ocr_image error: Tesseract binary not found on PATH. "
                "Install from https://github.com/UB-Mannheim/tesseract/wiki "
                "and ensure tesseract.exe is on PATH.]"
            )
        text = text.strip()
        if not text:
            return "(no text recognized)"
        return text
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
                return "[ocr_pdf error: Tesseract not on PATH]"
            label = first_page + i - 1 if first_page else i
            chunks.append(f"--- page {label} ---\n{text or '(empty)'}")
        return "\n\n".join(chunks)
    except Exception as e:
        return f"[ocr_pdf error: {type(e).__name__}: {e}]"
