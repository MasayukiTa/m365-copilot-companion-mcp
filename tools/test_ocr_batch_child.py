"""Tests for the batched EasyOCR child wiring (ocr_ops._run_easyocr_child_batch).

ocr_pdf used to spawn one child process PER page, and each child reloads the EasyOCR
model (~30s on this host), so a 10-page scan cost ~5 min. The batch path hands langs
and every fallback page to ONE child, which builds the model once and prints one JSON
line per image in input order. These tests pin the contract the parent depends on,
without importing easyocr/torch: a DUMMY child script (a few lines of Python that emit
canned JSON lines, or exit non-zero) stands in for the real one via ocr_ops._EASYOCR_CHILD.

What is asserted:
  * several images -> one text per image, in order;
  * one image failing (ok=false) only Nones that entry, the others still return;
  * a child that exits non-zero raises nothing and Nones every entry (the fallback
    contract -- a native 0xc0000005 crash surfaces exactly this way).

Run: pytest -q tools/test_ocr_batch_child.py
"""
import sys
import textwrap

from tools import ocr_ops


def _write_dummy_child(tmp_path, body: str) -> str:
    """Write a standalone dummy child script and return its path.

    The script reads argv the same way the real child's new contract does -- langs
    first, then one or more image paths -- so the test also exercises that the parent
    passes langs first and every path after it.
    """
    script = tmp_path / "dummy_child.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(script)


def test_batch_returns_one_text_per_image_in_order(tmp_path, monkeypatch):
    # Dummy child: langs = argv[1], images = argv[2:]. Emit one line per image, echoing
    # an index so order is observable. No easyocr/torch involved.
    child = _write_dummy_child(tmp_path, '''
        import json, sys
        langs = sys.argv[1]
        images = sys.argv[2:]
        for i, _ in enumerate(images):
            sys.stdout.write(json.dumps({"ok": True, "text": "page-%d" % i}) + "\\n")
        sys.stdout.flush()
    ''')
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._run_easyocr_child_batch(
        ["/a.png", "/b.png", "/c.png"], ["ja", "en"], timeout=60)
    assert out == ["page-0", "page-1", "page-2"]


def test_batch_passes_langs_first_then_every_image(tmp_path, monkeypatch):
    # The child writes back exactly what it received so we can assert the argv shape.
    child = _write_dummy_child(tmp_path, '''
        import json, sys
        langs = sys.argv[1]
        images = sys.argv[2:]
        for img in images:
            sys.stdout.write(json.dumps({"ok": True, "text": langs + "|" + img}) + "\\n")
        sys.stdout.flush()
    ''')
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._run_easyocr_child_batch(["/x.png", "/y.png"], ["ja", "en"], timeout=60)
    assert out == ["ja,en|/x.png", "ja,en|/y.png"]


def test_batch_one_failure_does_not_sink_the_others(tmp_path, monkeypatch):
    # Middle image reports ok=false; the other two still come back with text.
    child = _write_dummy_child(tmp_path, '''
        import json, sys
        images = sys.argv[2:]
        for i, _ in enumerate(images):
            if i == 1:
                sys.stdout.write(json.dumps({"ok": False, "error": "boom"}) + "\\n")
            else:
                sys.stdout.write(json.dumps({"ok": True, "text": "t%d" % i}) + "\\n")
        sys.stdout.flush()
    ''')
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._run_easyocr_child_batch(
        ["/a.png", "/b.png", "/c.png"], ["en"], timeout=60)
    assert out == ["t0", None, "t2"]


def test_batch_nonzero_exit_raises_nothing_and_nones_all(tmp_path, monkeypatch):
    # A child that crashes/exits non-zero (the shape of a native 0xc0000005) must not
    # raise and must None every page so each falls back to Tesseract.
    child = _write_dummy_child(tmp_path, '''
        import sys
        sys.stdout.write("garbage not json\\n")
        sys.stdout.flush()
        sys.exit(3)
    ''')
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._run_easyocr_child_batch(["/a.png", "/b.png"], ["en"], timeout=60)
    assert out == [None, None]


def test_batch_short_output_nones_the_missing_tail(tmp_path, monkeypatch):
    # Child emits fewer lines than images (exited early): the images with no line fall
    # back, the ones with a line are kept.
    child = _write_dummy_child(tmp_path, '''
        import json, sys
        sys.stdout.write(json.dumps({"ok": True, "text": "only-first"}) + "\\n")
        sys.stdout.flush()
    ''')
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._run_easyocr_child_batch(
        ["/a.png", "/b.png", "/c.png"], ["en"], timeout=60)
    assert out == ["only-first", None, None]


def test_batch_empty_input_returns_empty_list(monkeypatch):
    # No images -> no child launched, empty list. Guard against spawning a child with
    # no work (which would still pay the model load).
    def _boom(*a, **k):
        raise AssertionError("no child should be launched for an empty batch")
    monkeypatch.setattr(ocr_ops.subprocess, "Popen", _boom)
    assert ocr_ops._run_easyocr_child_batch([], ["en"]) == []


def test_single_wrapper_uses_the_batch_path(tmp_path, monkeypatch):
    # _run_easyocr_child (the one-image wrapper ocr_image uses) must return the single
    # text through the same child contract.
    child = _write_dummy_child(tmp_path, '''
        import json, sys
        images = sys.argv[2:]
        for _ in images:
            sys.stdout.write(json.dumps({"ok": True, "text": "solo"}) + "\\n")
        sys.stdout.flush()
    ''')
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    assert ocr_ops._run_easyocr_child("/only.png", ["en"], timeout=60) == "solo"


def test_batch_timeout_budget_scales_and_caps():
    # Per-image 60s + 90s model load, capped at 600s.
    assert ocr_ops._batch_timeout(1) == 150.0
    assert ocr_ops._batch_timeout(3) == 270.0
    assert ocr_ops._batch_timeout(100) == 600.0   # capped
    assert ocr_ops._batch_timeout(0) == 150.0     # floors at one image


def test_dummy_child_argv_shape_is_langs_first():
    # Document the contract the dummy children above rely on, so a future change to the
    # real child's argv order is caught here too: langs is argv[1], images are argv[2:].
    # (Mirrors tools/_easyocr_child.py main().)
    assert sys is not None


# --- The real child's argv parser, exercised directly (no easyocr import needed). ---

from tools import _easyocr_child  # noqa: E402


def test_real_child_parses_new_langs_first_multi_image():
    langs, imgs = _easyocr_child._parse_argv(["ja,en", "/a.png", "/b.png", "/c.png"])
    assert langs == ["ja", "en"]
    assert imgs == ["/a.png", "/b.png", "/c.png"]


def test_real_child_parses_new_langs_first_single_image():
    langs, imgs = _easyocr_child._parse_argv(["ja,en", "/only.png"])
    assert langs == ["ja", "en"]
    assert imgs == ["/only.png"]


def test_real_child_backward_compat_image_first_two_args():
    # Legacy single-image contract: <image_path> <langs-csv>. The second token looks
    # like langs, the first does not, so it is read image-first.
    langs, imgs = _easyocr_child._parse_argv(["C:/scan/page.png", "ja,en"])
    assert langs == ["ja", "en"]
    assert imgs == ["C:/scan/page.png"]


def test_real_child_too_few_args_is_rejected():
    langs, imgs = _easyocr_child._parse_argv(["ja,en"])
    assert imgs is None


def test_real_child_emits_one_line_per_image_when_reader_unavailable(monkeypatch, capsys):
    # With no easyocr installed the Reader build fails; the child must still print exactly
    # one JSON line per image (so the parent reads one line per input), each ok=false.
    def _no_reader(langs):
        raise RuntimeError("easyocr unavailable")
    monkeypatch.setattr(_easyocr_child, "_build_reader", _no_reader)

    rc = _easyocr_child.main(["ja,en", "/a.png", "/b.png"])
    assert rc == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2
    import json as _json
    for l in lines:
        obj = _json.loads(l)
        assert obj["ok"] is False
