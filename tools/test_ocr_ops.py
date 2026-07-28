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
    # lang="eng" means no Japanese was expected, so Latin-only output is a complete
    # answer and the fallback must not run at all.
    assert ocr_ops.ocr_image(str(img), lang="eng") == "hello"


def test_latin_result_is_kept_when_the_fallback_also_finds_no_japanese(tmp_path, monkeypatch):
    """A genuinely English page requested as jpn+eng must keep Tesseract's text.

    The default language is "jpn+eng", so an English-only document reaches the same
    branch as a transliterated Japanese one -- both produce Latin with no CJK, and
    the two cannot be told apart from the text alone. The fallback is therefore
    consulted, but its answer is only preferred when it actually contains Japanese;
    otherwise the original reading stands. The cost is one extra pass on
    English pages; the alternative is silently serving transliterated Japanese.
    """
    img = tmp_path / "eng.png"
    try:
        from PIL import Image
    except ImportError:
        import pytest
        pytest.skip("Pillow not installed")
    Image.new("RGB", (40, 20), (255, 255, 255)).save(img)

    monkeypatch.setattr(ocr_ops, "_validate_path", lambda p: Path(p))
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "hello world")
    # Fallback runs but returns Latin too -> Tesseract's result must win.
    monkeypatch.setattr(ocr_ops, "_easyocr_text", lambda p, l: "hello vvorld")

    assert ocr_ops.ocr_image(str(img), lang="jpn+eng") == "hello world"

def test_detects_transliterated_japanese_as_a_failed_read():
    """Without the jpn pack Tesseract returns confident Latin garbage, not empty.

    A 判定 column of 良/良/注意 comes back as "R/R/aa": non-empty, so an
    empty-check alone never fires and the wrong text is served as if it were read.
    """
    garbled = "08:00 72.4 101.3 45.2 R\n14:00 76.2 102.8 44.1 aa"
    assert ocr_ops._tesseract_failed_japanese(garbled, "jpn+eng") is True
    # Real Japanese output must NOT be treated as a failure.
    assert ocr_ops._tesseract_failed_japanese("判定 良 注意", "jpn+eng") is False
    # If Japanese was never requested, Latin-only output is perfectly valid.
    assert ocr_ops._tesseract_failed_japanese(garbled, "eng") is False


def test_has_cjk_covers_kana_and_ideographs():
    assert ocr_ops._has_cjk("良") is True
    assert ocr_ops._has_cjk("ちゅうい") is True
    assert ocr_ops._has_cjk("カタカナ") is True
    assert ocr_ops._has_cjk("R aa 45.2") is False


def test_ocr_image_falls_back_when_japanese_comes_back_transliterated(tmp_path, monkeypatch):
    img = tmp_path / "form.png"
    try:
        from PIL import Image
    except ImportError:
        import pytest
        pytest.skip("Pillow not installed")
    Image.new("RGB", (40, 20), (255, 255, 255)).save(img)

    monkeypatch.setattr(ocr_ops, "_validate_path", lambda p: Path(p))
    import pytesseract
    # Non-empty but Japanese-free: the exact shape that used to slip through.
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "45.2 R\n44.1 aa")
    monkeypatch.setattr(ocr_ops, "_easyocr_text", lambda p, l: "45.2 良\n44.1 注意")

    out = ocr_ops.ocr_image(str(img), lang="jpn+eng")
    assert "注意" in out and "aa" not in out
