# -*- coding: utf-8 -*-
"""create_pptx(template=...) builds on a reference deck instead of a blank one.

The operator's complaint on row #93 was that a generated deck "did not reference the
past material at all" -- it came out as a stock white python-pptx deck even when a
source deck existed. The fix lets create_pptx open the reference deck so its slide
size, masters, layouts and theme carry across, while the template's own sample slides
are dropped so only the design is inherited.

These tests build a distinctive template (an unusual slide size and an extra layout
marker), generate on top of it, and prove the design came across and the sample
slides did not.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")
from pptx import Presentation  # noqa: E402
from pptx.util import Inches  # noqa: E402

from tools import pptx_ops  # noqa: E402


def _unlock(monkeypatch):
    # create_pptx guards on require_unlocked(); neutralise it for the test.
    monkeypatch.setattr(pptx_ops, "require_unlocked", lambda: None)


def _make_template(path: Path) -> None:
    """A reference deck with a distinctive 4:3 size and a sample slide to be dropped."""
    prs = Presentation()
    prs.slide_width = Inches(10.0)   # 4:3-ish, different from the 13.333 default
    prs.slide_height = Inches(7.5)
    s = prs.slides.add_slide(prs.slide_layouts[0])
    if s.shapes.title is not None:
        s.shapes.title.text = "TEMPLATE SAMPLE SLIDE"
    prs.save(str(path))


def test_template_slide_size_is_inherited(tmp_path, monkeypatch):
    _unlock(monkeypatch)
    tpl = tmp_path / "ref.pptx"
    _make_template(tpl)
    out = tmp_path / "out.pptx"
    res = pptx_ops.create_pptx(
        str(out),
        [{"title": "New", "bullets": ["a", "b"]}],
        template=str(tpl),
    )
    assert "error" not in res.lower(), res
    prs = Presentation(str(out))
    # The template's 10-inch width must survive, NOT the 13.333 blank default.
    assert prs.slide_width == Inches(10.0)
    assert prs.slide_height == Inches(7.5)


def test_template_sample_slides_are_dropped(tmp_path, monkeypatch):
    _unlock(monkeypatch)
    tpl = tmp_path / "ref.pptx"
    _make_template(tpl)
    out = tmp_path / "out.pptx"
    pptx_ops.create_pptx(
        str(out),
        [{"title": "Only This", "bullets": ["x"]}],
        template=str(tpl),
    )
    prs = Presentation(str(out))
    # Exactly the one new slide -- the template's sample slide must be gone.
    assert len(prs.slides) == 1
    texts = []
    for sh in prs.slides[0].shapes:
        if sh.has_text_frame:
            texts.append(sh.text_frame.text)
    joined = " ".join(texts)
    assert "Only This" in joined
    assert "TEMPLATE SAMPLE SLIDE" not in joined


def test_template_masters_carry_across(tmp_path, monkeypatch):
    _unlock(monkeypatch)
    tpl = tmp_path / "ref.pptx"
    _make_template(tpl)
    out = tmp_path / "out.pptx"
    pptx_ops.create_pptx(
        str(out),
        [{"title": "New"}],
        template=str(tpl),
    )
    src = Presentation(str(tpl))
    dst = Presentation(str(out))
    # The design parts (masters) are inherited from the template.
    assert len(dst.slide_masters) == len(src.slide_masters)
    assert len(dst.slide_layouts) == len(src.slide_layouts)


def test_without_template_uses_blank_default(tmp_path, monkeypatch):
    _unlock(monkeypatch)
    out = tmp_path / "plain.pptx"
    res = pptx_ops.create_pptx(str(out), [{"title": "Hi", "bullets": ["a"]}])
    assert "error" not in res.lower(), res
    prs = Presentation(str(out))
    # Unchanged legacy behaviour: 16:9 widescreen default.
    assert prs.slide_width == Inches(13.333)
    assert len(prs.slides) == 1


def test_missing_template_is_reported(tmp_path, monkeypatch):
    _unlock(monkeypatch)
    out = tmp_path / "out.pptx"
    res = pptx_ops.create_pptx(
        str(out),
        [{"title": "New"}],
        template=str(tmp_path / "does_not_exist.pptx"),
    )
    assert "error" in res.lower()
    assert not out.exists()


def test_markdown_passes_template_through(tmp_path, monkeypatch):
    _unlock(monkeypatch)
    tpl = tmp_path / "ref.pptx"
    _make_template(tpl)
    out = tmp_path / "md.pptx"
    md = "# Cover\n\n# Topic\n- one\n- two\n"
    res = pptx_ops.pptx_from_markdown(str(out), md, template=str(tpl))
    assert "error" not in res.lower(), res
    prs = Presentation(str(out))
    assert prs.slide_width == Inches(10.0)
    # None of the rendered slides should be the template's sample slide.
    for sl in prs.slides:
        for sh in sl.shapes:
            if sh.has_text_frame:
                assert "TEMPLATE SAMPLE SLIDE" not in sh.text_frame.text
