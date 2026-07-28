"""Tests for the OCR fallback wiring.

The behaviour under test is the reason the fallback exists: a stock Windows
Tesseract install has English data only, so Japanese comes back EMPTY instead of
raising. An agent that only sees "" cannot tell "nothing written here" from "this
engine cannot read this script", and will happily infer the missing values from
context and report them as if they had been read.

Run: pytest -q tools/test_ocr_ops.py
"""
from pathlib import Path

from tools import ocr_ops


def test_easyocr_langs_maps_tesseract_codes():
    assert ocr_ops._easyocr_langs("jpn+eng") == ["ja", "en"]
    assert ocr_ops._easyocr_langs("eng") == ["en"]
    # Japanese alone still gets English appended -- EasyOCR requires the pairing.
    assert ocr_ops._easyocr_langs("jpn") == ["ja", "en"]
    # Unknown/blank input must not explode; it falls back to English.
    assert ocr_ops._easyocr_langs("") == ["en"]


def test_easyocr_langs_dedupes():
    assert ocr_ops._easyocr_langs("eng+eng") == ["en"]
    assert ocr_ops._easyocr_langs("jpn+eng+jpn") == ["ja", "en"]


def test_ocr_image_falls_back_when_tesseract_returns_empty(tmp_path, monkeypatch):
    """Empty Tesseract output must trigger EasyOCR, not be reported as no text."""
    img = tmp_path / "sample.png"
    try:
        from PIL import Image
    except ImportError:
        import pytest
        pytest.skip("Pillow not installed")
    Image.new("RGB", (40, 20), (255, 255, 255)).save(img)

    monkeypatch.setattr(ocr_ops, "_validate_path", lambda p: Path(p))

    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "   ")

    called = {}

    def fake_fallback(path, lang):
        called["lang"] = lang
        return "判定\n良\n注意"

    monkeypatch.setattr(ocr_ops, "_easyocr_text", fake_fallback)

    out = ocr_ops.ocr_image(str(img), lang="jpn+eng")
    assert "注意" in out
    assert called["lang"] == "jpn+eng"


def test_ocr_image_reports_no_text_when_both_engines_find_nothing(tmp_path, monkeypatch):
    img = tmp_path / "blank.png"
    try:
        from PIL import Image
    except ImportError:
        import pytest
        pytest.skip("Pillow not installed")
    Image.new("RGB", (40, 20), (255, 255, 255)).save(img)

    monkeypatch.setattr(ocr_ops, "_validate_path", lambda p: Path(p))
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "")
    monkeypatch.setattr(ocr_ops, "_easyocr_text", lambda p, l: None)

    assert ocr_ops.ocr_image(str(img)) == "(no text recognized)"


def test_ocr_image_prefers_tesseract_when_it_succeeds(tmp_path, monkeypatch):
    """The fallback must not run (nor pay its model-load cost) on the happy path."""
    img = tmp_path / "eng.png"
    try:
        from PIL import Image
    except ImportError:
        import pytest
        pytest.skip("Pillow not installed")
    Image.new("RGB", (40, 20), (255, 255, 255)).save(img)

    monkeypatch.setattr(ocr_ops, "_validate_path", lambda p: Path(p))
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "hello")

    def boom(*a, **k):
        raise AssertionError("EasyOCR must not be used when Tesseract succeeds")

    monkeypatch.setattr(ocr_ops, "_easyocr_text", boom)
    assert ocr_ops.ocr_image(str(img)) == "hello"
