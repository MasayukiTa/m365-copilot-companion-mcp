from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .file_ops import _validate_path
from .security import require_unlocked

if TYPE_CHECKING:  # annotations only; the real import happens inside each function
    from pptx import Presentation

# python-pptx costs ~1-2s to import at startup; defer it until a pptx tool is actually
# called. Each function that needs it calls _pptx_imports() first, so behaviour is
# unchanged -- the cost just moves from server boot to first pptx use.


def _pptx_imports():
    from pptx import Presentation
    from pptx.util import Emu, Inches, Pt
    return Presentation, Emu, Inches, Pt


# Layout indices in the default python-pptx blank template.
# 0=title slide, 1=title+content, 2=section header, 3=two content,
# 5=title only, 6=blank
LAYOUT_TITLE = 0
LAYOUT_TITLE_CONTENT = 1
LAYOUT_SECTION = 2
LAYOUT_TITLE_ONLY = 5
LAYOUT_BLANK = 6


def create_pptx(path: str, slides: list[dict], title: Optional[str] = None,
                template: Optional[str] = None) -> str:
    """Create a .pptx file from a list of slide specs.

    Each slide is a dict supporting these keys (all optional unless noted):
      - title: str
      - subtitle: str            (only used for the cover slide)
      - bullets: list[str]       (one bullet per item; nested via "  " indent)
      - body: str                (multiline freeform text, used if no bullets)
      - image_path: str          (PNG/JPG path, placed on the right half)
      - notes: str               (presenter notes)
      - layout: "cover" | "title_content" | "section" | "title_only" | "blank"

    If layout is omitted, a sensible default is chosen.

    When `template` names an existing .pptx, the deck is BUILT ON THAT FILE rather
    than on python-pptx's blank default: its slide masters, layouts, theme colours,
    fonts and slide size are inherited, so "this month's report from the past
    material" actually looks like the past material instead of a stock white deck.
    The template's own slides are not copied into the output (the new slides are the
    content); only its design is carried across. This is the reference-first path --
    prefer it whenever a source deck exists, so output is not a from-scratch deck that
    ignores what it was asked to resemble.

    Args:
        path: Output .pptx path under the allowed base.
        slides: Ordered list of slide spec dicts. Must contain at least one slide.
        title: Optional deck title used for file metadata.
        template: Optional path to an existing .pptx whose design is inherited.

    Self-verify before reporting done: call pptx_export_png on the output, then check a
    slide or two with ocr_image (does the text you put there come back?) or run_python with
    PIL (is anything clipped at the edge?). NOT read_image -- it returns base64 text no model
    in this stack sees, so a worker that calls it reports a check it did not perform
    (measured 2026-09-17). To have a model actually look, end the turn with
    `ANALYZE: <png path> | <what to check>`. Regenerate if it looks wrong.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not isinstance(slides, list) or not slides:
            return "[create_pptx error: 'slides' must be a non-empty list]"
        Presentation, Emu, Inches, Pt = _pptx_imports()
        out = _validate_path(path)
        if out.suffix.lower() != ".pptx":
            return "[create_pptx error: path must end with .pptx]"
        out.parent.mkdir(parents=True, exist_ok=True)

        if template:
            src = _validate_path(template)
            if not src.exists():
                return f"[create_pptx error: template not found: {template}]"
            if src.suffix.lower() != ".pptx":
                return "[create_pptx error: template must be a .pptx file]"
            # Build on the reference deck so its masters, layouts, theme and size are
            # inherited; drop the template's own slides so only the design carries over.
            prs = Presentation(str(src))
            _strip_existing_slides(prs)
        else:
            prs = Presentation()
            prs.slide_width = Inches(13.333)
            prs.slide_height = Inches(7.5)
        if title:
            prs.core_properties.title = title

        for spec in slides:
            if not isinstance(spec, dict):
                return "[create_pptx error: each slide must be an object]"
            _add_slide(prs, spec)

        prs.save(str(out))
        return f"Wrote PowerPoint {out} ({len(slides)} slide(s))"
    except Exception as e:
        return f"[create_pptx error: {type(e).__name__}: {e}]"


def pptx_from_markdown(path: str, markdown: str, title: Optional[str] = None,
                       template: Optional[str] = None) -> str:
    """Convert a markdown document into slides.

    Conventions:
      - The first H1 (# ...) becomes the cover slide title.
      - Each subsequent H1 starts a new slide (title + bullets).
      - H2 inside a slide becomes a sub-heading line.
      - Bullet lines (- or *) become slide bullets.
      - "---" creates a section divider slide using the next H1 as the title.

    Args:
        path: Output .pptx path under the allowed base.
        markdown: Markdown source text.
        title: Optional deck title used for file metadata.
        template: Optional path to an existing .pptx whose design is inherited
            (reference-first path; see create_pptx).

    Self-verify before reporting done: call pptx_export_png on the output, then check a
    slide or two with ocr_image (does the text you put there come back?) or run_python with
    PIL (is anything clipped at the edge?). NOT read_image -- it returns base64 text no model
    in this stack sees, so a worker that calls it reports a check it did not perform
    (measured 2026-09-17). To have a model actually look, end the turn with
    `ANALYZE: <png path> | <what to check>`. Regenerate if it looks wrong.
    """
    slides = _markdown_to_slide_specs(markdown)
    if not slides:
        return "[pptx_from_markdown error: no headings found; nothing to render]"
    return create_pptx(path, slides, title=title or slides[0].get("title"),
                       template=template)


def pptx_info(path: str) -> str:
    """Return a structural summary of an existing .pptx (slide count + titles)."""
    try:
        Presentation, Emu, Inches, Pt = _pptx_imports()
        p = _validate_path(path)
        if p.suffix.lower() != ".pptx":
            return "[pptx_info error: path must end with .pptx]"
        prs = Presentation(str(p))
        lines = [f"slides: {len(prs.slides)}"]
        # WHERE THE PROVENANCE TAG GETS READ. Writing a tag nothing ever reads is a tag that
        # does not exist; this is the place somebody already asks about a deck. Stated only
        # when it is there, because absence says nothing -- a deck written before the stamp
        # existed, or by a path that never reaches it, carries no tag either, and printing
        # "not agent-made" would turn silence into a claim about a person.
        try:
            from .pptx_provenance import made_by_agent

            if made_by_agent(str(p)):
                lines.append("provenance: this deck carries the agent's own mark")
        except Exception:
            pass
        for i, slide in enumerate(prs.slides, 1):
            title_text = ""
            if slide.shapes.title and slide.shapes.title.has_text_frame:
                title_text = slide.shapes.title.text_frame.text.strip()
            shapes_with_text = sum(
                1 for shp in slide.shapes if shp.has_text_frame and shp.text_frame.text.strip()
            )
            lines.append(f"  {i:>3}. {title_text or '(untitled)'}  [{shapes_with_text} text shapes]")
        return "\n".join(lines)
    except Exception as e:
        return f"[pptx_info error: {type(e).__name__}: {e}]"


def pptx_add_slide(pptx_path: str, slide: dict) -> str:
    """Append one slide to an existing .pptx using the same spec shape as create_pptx.

    Args:
        pptx_path: Existing .pptx path.
        slide: Slide spec dict with keys title/subtitle/bullets/body/image_path/notes/layout.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        Presentation, Emu, Inches, Pt = _pptx_imports()
        if not isinstance(slide, dict):
            return "[pptx_add_slide error: slide must be an object]"
        p = _validate_path(pptx_path)
        if not p.is_file():
            return f"[pptx_add_slide error: file not found: {p}]"
        prs = Presentation(str(p))
        _add_slide(prs, slide)
        prs.save(str(p))
        return f"Appended 1 slide to {p} (now {len(prs.slides)} total)"
    except Exception as e:
        return f"[pptx_add_slide error: {type(e).__name__}: {e}]"


def pptx_add_image(
    pptx_path: str,
    slide_index: int,
    image_path: str,
    left_in: float = 0.7,
    top_in: float = 1.4,
    width_in: Optional[float] = None,
    height_in: Optional[float] = None,
) -> str:
    """Place an image on an existing slide (1-indexed).

    Args:
        pptx_path: Existing .pptx path.
        slide_index: 1-based slide index to modify.
        image_path: Image file to insert.
        left_in: Position from left, in inches.
        top_in: Position from top, in inches.
        width_in: Width in inches. If both width and height are omitted, the
            image's native size is used.
        height_in: Height in inches.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        Presentation, Emu, Inches, Pt = _pptx_imports()
        p = _validate_path(pptx_path)
        if not p.is_file():
            return f"[pptx_add_image error: file not found: {p}]"
        img = _validate_path(image_path)
        if not img.is_file():
            return f"[pptx_add_image error: image not found: {img}]"
        prs = Presentation(str(p))
        if slide_index < 1 or slide_index > len(prs.slides):
            return f"[pptx_add_image error: slide_index out of range (1..{len(prs.slides)})]"
        slide = prs.slides[slide_index - 1]
        kwargs = {}
        if width_in is not None:
            kwargs["width"] = Inches(width_in)
        if height_in is not None:
            kwargs["height"] = Inches(height_in)
        pic = slide.shapes.add_picture(str(img), Inches(left_in), Inches(top_in), **kwargs)
        prs.save(str(p))
        w_in = pic.width / 914400.0
        h_in = pic.height / 914400.0
        return (
            f"Added image on slide {slide_index} of {p}\n"
            f"  position: ({left_in:.2f}, {top_in:.2f}) in\n"
            f"  size: {w_in:.2f} x {h_in:.2f} in"
        )
    except Exception as e:
        return f"[pptx_add_image error: {type(e).__name__}: {e}]"


def pptx_replace_image(
    pptx_path: str,
    slide_index: int,
    new_image_path: str,
    image_index: int = 1,
) -> str:
    """Replace an existing picture on a slide with a new image, preserving position/size.

    Args:
        pptx_path: Existing .pptx path.
        slide_index: 1-based slide index.
        new_image_path: Replacement image file.
        image_index: 1-based index among picture shapes on the slide.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        Presentation, Emu, Inches, Pt = _pptx_imports()
        p = _validate_path(pptx_path)
        if not p.is_file():
            return f"[pptx_replace_image error: file not found: {p}]"
        img = _validate_path(new_image_path)
        if not img.is_file():
            return f"[pptx_replace_image error: image not found: {img}]"
        prs = Presentation(str(p))
        if slide_index < 1 or slide_index > len(prs.slides):
            return f"[pptx_replace_image error: slide_index out of range (1..{len(prs.slides)})]"
        slide = prs.slides[slide_index - 1]
        pics = [shp for shp in slide.shapes if shp.shape_type == 13]  # 13 = PICTURE
        if not pics:
            return f"[pptx_replace_image error: no picture shapes on slide {slide_index}]"
        if image_index < 1 or image_index > len(pics):
            return f"[pptx_replace_image error: image_index out of range (1..{len(pics)})]"
        target = pics[image_index - 1]
        left, top, width, height = target.left, target.top, target.width, target.height
        sp = target._element
        sp.getparent().remove(sp)
        slide.shapes.add_picture(str(img), left, top, width=width, height=height)
        prs.save(str(p))
        return f"Replaced picture #{image_index} on slide {slide_index} of {p}"
    except Exception as e:
        return f"[pptx_replace_image error: {type(e).__name__}: {e}]"


def pptx_add_table(
    pptx_path: str,
    slide_index: int,
    rows: list[list[str]],
    left_in: float = 0.6,
    top_in: float = 1.4,
    width_in: float = 11.8,
    height_in: float = 5.0,
    header_row: bool = True,
) -> str:
    """Insert a table onto an existing slide.

    Args:
        pptx_path: Existing .pptx path.
        slide_index: 1-based slide index.
        rows: 2D list of cell strings. The first row is treated as header when header_row=True.
        left_in, top_in: Position in inches.
        width_in, height_in: Size in inches.
        header_row: When true, bold the first row.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        Presentation, Emu, Inches, Pt = _pptx_imports()
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
            return "[pptx_add_table error: rows must be a 2D list]"
        p = _validate_path(pptx_path)
        if not p.is_file():
            return f"[pptx_add_table error: file not found: {p}]"
        prs = Presentation(str(p))
        if slide_index < 1 or slide_index > len(prs.slides):
            return f"[pptx_add_table error: slide_index out of range (1..{len(prs.slides)})]"
        slide = prs.slides[slide_index - 1]
        n_rows = len(rows)
        n_cols = max(len(r) for r in rows)
        tbl = slide.shapes.add_table(
            n_rows, n_cols, Inches(left_in), Inches(top_in), Inches(width_in), Inches(height_in)
        ).table
        for r, row in enumerate(rows):
            for c in range(n_cols):
                text = str(row[c]) if c < len(row) else ""
                cell = tbl.cell(r, c)
                cell.text = text
                for para in cell.text_frame.paragraphs:
                    for run in para.runs:
                        run.font.size = Pt(12)
                        if header_row and r == 0:
                            run.font.bold = True
        prs.save(str(p))
        return f"Added {n_rows}x{n_cols} table on slide {slide_index} of {p}"
    except Exception as e:
        return f"[pptx_add_table error: {type(e).__name__}: {e}]"


def pptx_export_png(
    pptx_path: str,
    output_dir: Optional[str] = None,
    width_px: int = 1280,
) -> str:
    """Render every slide of a .pptx to PNG using Microsoft PowerPoint (Windows COM).

    Use this to self-verify slide layout before reporting completion. Requires
    PowerPoint to be installed on the host.

    Args:
        pptx_path: Existing .pptx path.
        output_dir: Directory for the PNGs (created if missing). Defaults to
            "<pptx>_preview" alongside the pptx.
        width_px: Target PNG width in pixels.
    """
    try:
        p = _validate_path(pptx_path)
        if not p.is_file():
            return f"[pptx_export_png error: file not found: {p}]"
        out_dir = _validate_path(output_dir) if output_dir else p.with_suffix("").parent / f"{p.stem}_preview"
        if isinstance(out_dir, Path):
            out_dir = _validate_path(str(out_dir))
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError:
            return "[pptx_export_png error: pywin32 not installed; cannot drive PowerPoint COM]"

        pythoncom.CoInitialize()
        ppt = None
        pres = None
        try:
            ppt = win32com.client.DispatchEx("PowerPoint.Application")
            # PowerPoint can't be fully hidden on all builds; the dispatch-export-quit pattern is the standard.
            pres = ppt.Presentations.Open(str(p), WithWindow=False)
            scale_h = int(width_px * (pres.PageSetup.SlideHeight / pres.PageSetup.SlideWidth))
            pres.Export(str(out_dir), "PNG", width_px, scale_h)
            count = len(list(out_dir.glob("*.PNG"))) + len(list(out_dir.glob("*.png")))
            return f"Exported {count} PNG(s) to {out_dir}"
        finally:
            try:
                if pres is not None:
                    pres.Close()
            except Exception:
                pass
            try:
                if ppt is not None:
                    ppt.Quit()
            except Exception:
                pass
            pythoncom.CoUninitialize()
    except Exception as e:
        return f"[pptx_export_png error: {type(e).__name__}: {e}]"


def _resolve_reference(reference: str, search_dir: Optional[str]) -> tuple[Optional[Path], str]:
    """Resolve a reference deck given as a path, or as a provenance id (RunId or digest).

    Returns (path, note). The path is the deck to edit in place; note explains how it was
    resolved. The point of accepting an id is the operator's own words: a deck must be
    addressable by the id it was stamped with (CompanionRunId) or by its content digest, so
    "this month from last month's" can name the source without knowing where it sits on disk.
    When an id is given, every .pptx under search_dir is scanned and matched against its
    stamped RunId or its content digest; a path is tried as a path first.
    """
    from . import pptx_provenance as _prov

    # A literal path wins when it points at a real file.
    try:
        cand = _validate_path(reference)
        if cand.is_file() and cand.suffix.lower() == ".pptx":
            return cand, f"path: {cand}"
    except Exception:
        pass

    # Otherwise treat `reference` as a provenance id (RunId, or a content digest prefix).
    if not search_dir:
        return None, ("reference is not a .pptx path and no search_dir was given to resolve "
                      "it as a provenance id")
    try:
        base = _validate_path(search_dir)
    except Exception as e:
        return None, f"search_dir invalid: {e}"
    if not base.is_dir():
        return None, f"search_dir not a directory: {base}"

    want = reference.strip()
    by_run = None
    by_digest = None
    # scan() returns rows whose "path" is RELATIVE to base, and already carries the stamped
    # "run" id -- so resolve each path against base and match the run id without re-reading.
    for row in _prov.scan(str(base)):
        rel = row.get("path") if isinstance(row, dict) else None
        if not rel:
            continue
        fpath = os.path.join(str(base), rel)
        if row.get("run") and row.get("run") == want:
            by_run = fpath
            break
        d = _prov.digest(fpath)
        if d and want and (d == want or d.startswith(want) or want.startswith(d)):
            by_digest = by_digest or fpath
    hit = by_run or by_digest
    if hit:
        how = "CompanionRunId" if by_run else "content digest"
        return Path(hit), f"id {want!r} matched by {how}: {hit}"
    return None, f"no deck under {base} carries the provenance id {want!r}"


def _apply_text_updates(slide, upd: dict) -> int:
    """Edit one slide's text in place, preserving its existing formatting.

    Only text is changed; runs keep their font/size/colour because we overwrite run.text
    rather than rebuilding the shape. Supported keys in `upd`:
      - title: str                       set the slide's title placeholder text
      - body:  str                       set the first body placeholder (joined lines)
      - bullets: list[str]               set the first body placeholder, one bullet per line
      - replace: dict[str, str]          substring replacement across every text run
    Returns the number of text edits applied, so a caller can tell a no-op from a real edit.
    """
    edits = 0
    title = upd.get("title")
    if title is not None and slide.shapes.title is not None:
        slide.shapes.title.text = str(title)
        edits += 1

    body_text = None
    if isinstance(upd.get("bullets"), list):
        body_text = "\n".join(str(b) for b in upd["bullets"])
    elif upd.get("body") is not None:
        body_text = str(upd["body"])
    if body_text is not None:
        for ph in slide.placeholders:
            if ph == slide.shapes.title:
                continue
            if ph.has_text_frame:
                ph.text_frame.text = body_text
                edits += 1
                break

    repl = upd.get("replace")
    if isinstance(repl, dict) and repl:
        for shp in slide.shapes:
            if not shp.has_text_frame:
                continue
            for para in shp.text_frame.paragraphs:
                for run in para.runs:
                    new = run.text
                    for old, rep in repl.items():
                        if old:
                            new = new.replace(str(old), str(rep))
                    if new != run.text:
                        run.text = new
                        edits += 1
    return edits


def pptx_from_reference(
    output_path: str,
    reference: str,
    updates: Optional[list[dict]] = None,
    search_dir: Optional[str] = None,
    run_id: str = "",
) -> str:
    """Build a deck FROM a past deck by editing that deck's own contents in place.

    WHY THIS EXISTS, in the operator's words: 「作成されたpptxがまったくもって過去のものを
    参照できていない」「参照元があるならば参照元を直接中身の編集がなされるべき」「idでもてるは
    初期構想としてmust」. create_pptx starts from an empty python-pptx template, so its output
    inherits nothing from the material it was supposedly based on -- same words, from scratch,
    none of the source deck's master, theme, layout, colours or existing slide design. This
    tool instead OPENS the reference deck and rewrites only the text the caller names, leaving
    every visual element of the original intact, which is what "edit the source directly" means.

    The reference can be a .pptx path, OR a provenance id -- either the CompanionRunId a prior
    agent run stamped, or a content-digest prefix. Given an id and a search_dir, the matching
    deck is found by scanning stamped decks, so a deck is addressable by id as the initial
    design required. The output is stamped with CompanionSources pointing back at the source
    (its digest), so the chain source -> product stays legible later.

    Args:
        output_path: Where to write the edited deck (.pptx). The reference is copied first;
            the source file on disk is never modified.
        reference: Path to the source .pptx, or a provenance id (CompanionRunId or digest).
        updates: Optional list of per-slide edits. Entry N edits slide N (1-based via order).
            Each dict may carry title/body/bullets/replace -- see _apply_text_updates. A dict
            may instead carry {"slide": <1-based index>, ...} to target a specific slide; when
            "slide" is omitted, entries apply to slides in order starting at slide 1.
        search_dir: Folder to scan when `reference` is a provenance id rather than a path.
        run_id: Run id to stamp on the output (CompanionRunId), tying it to this transcript.

    Self-verify before reporting done: call pptx_export_png on the output, then look at a
    slide with `ANALYZE: <png path> | does it carry the source deck's design`. NOT read_image.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        Presentation, Emu, Inches, Pt = _pptx_imports()
        out = _validate_path(output_path)
        if out.suffix.lower() != ".pptx":
            return "[pptx_from_reference error: output_path must end with .pptx]"
        src, note = _resolve_reference(reference, search_dir)
        if src is None:
            return f"[pptx_from_reference error: {note}]"

        out.parent.mkdir(parents=True, exist_ok=True)
        # Open the SOURCE and save AS the output: every part of the source deck (master,
        # theme, layouts, existing slides and their formatting) is carried over, and the
        # source file on disk is left untouched.
        prs = Presentation(str(src))

        applied = 0
        touched_slides = 0
        if updates:
            if not isinstance(updates, list):
                return "[pptx_from_reference error: 'updates' must be a list of slide edits]"
            n = len(prs.slides)
            for i, upd in enumerate(updates, start=1):
                if not isinstance(upd, dict):
                    return "[pptx_from_reference error: each update must be an object]"
                idx = upd.get("slide", i)
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    return f"[pptx_from_reference error: slide index not an int: {upd.get('slide')!r}]"
                if idx < 1 or idx > n:
                    return f"[pptx_from_reference error: slide index {idx} out of range (1..{n})]"
                e = _apply_text_updates(prs.slides[idx - 1], upd)
                applied += e
                if e:
                    touched_slides += 1

        prs.save(str(out))

        # Stamp provenance: mark it agent-made, record the run, and point CompanionSources at
        # the source deck's digest so the product can be traced back to what it was built from.
        stamp_note = ""
        try:
            from . import pptx_provenance as _prov
            _prov.stamp(str(out), sources=[str(src)], run_id=run_id)
            stamp_note = " provenance stamped (CompanionSources -> source digest)"
        except Exception as se:
            stamp_note = f" [provenance stamp skipped: {type(se).__name__}: {se}]"

        return (
            f"Wrote {out} from reference ({note}); "
            f"{len(prs.slides)} slide(s), {applied} text edit(s) across {touched_slides} slide(s)."
            f"{stamp_note}"
        )
    except Exception as e:
        return f"[pptx_from_reference error: {type(e).__name__}: {e}]"


# ---------- internals ----------

def _strip_existing_slides(prs: Presentation) -> None:
    """Remove every slide a template deck arrived with, keeping its masters and layouts.

    Inheriting a reference deck's design means opening it with Presentation(); that also
    opens its slides. The output should carry the template's look, not its content, so
    the sample slides are dropped here before the new ones are added. Masters, layouts
    and the theme live on the presentation part, not on the slides, so they survive.
    """
    slides_part = prs.slides
    xml_slides = slides_part._sldIdLst
    for sld_id in list(xml_slides):
        rId = sld_id.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        # Drop the slide part itself (and its package relationship), not just its entry
        # in the id list, so the output zip does not keep an orphaned slide1.xml.
        if rId is not None:
            try:
                slides_part.part.drop_rel(rId)
            except Exception:
                pass
        xml_slides.remove(sld_id)


def _add_slide(prs: "Presentation", spec: dict) -> None:
    _P, Emu, Inches, Pt = _pptx_imports()
    layout_name = spec.get("layout")
    layout_idx = _pick_layout(spec, layout_name)
    # A template deck may carry fewer layouts than the default's six; fall back to the
    # last available one rather than raising IndexError on a reference-first build.
    layouts = prs.slide_layouts
    if layout_idx >= len(layouts):
        layout_idx = len(layouts) - 1
    layout = layouts[layout_idx]
    slide = prs.slides.add_slide(layout)

    title = spec.get("title")
    subtitle = spec.get("subtitle")
    bullets = spec.get("bullets")
    body = spec.get("body")
    image_path = spec.get("image_path")
    notes = spec.get("notes")

    if title and slide.shapes.title is not None:
        slide.shapes.title.text = str(title)
        _style_title(slide.shapes.title)

    if layout_idx == LAYOUT_TITLE and subtitle:
        placeholders = [ph for ph in slide.placeholders if ph.placeholder_format.idx == 1]
        if placeholders:
            placeholders[0].text = str(subtitle)

    # Find the content placeholder (idx 1) for text body if present.
    body_ph = None
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == 1 and ph != slide.shapes.title:
            body_ph = ph
            break

    if bullets and isinstance(bullets, list) and body_ph is not None:
        _fill_bullets(body_ph, bullets)
    elif body and body_ph is not None:
        _fill_body(body_ph, str(body))

    if image_path:
        img = _validate_path(image_path)
        if not img.is_file():
            raise FileNotFoundError(f"image_path not found: {img}")
        bullets_or_body = bullets or body
        if bullets_or_body:
            # Right-half layout: text on left, image on right.
            left = Inches(7.0)
            top = Inches(1.5)
            width = Inches(5.8)
            slide.shapes.add_picture(str(img), left, top, width=width)
        else:
            # Full-width hero image under the title.
            left = Inches(0.7)
            top = Inches(1.4)
            width = Inches(11.9)
            slide.shapes.add_picture(str(img), left, top, width=width)

    if notes:
        slide.notes_slide.notes_text_frame.text = str(notes)


def _pick_layout(spec: dict, explicit: Optional[str]) -> int:
    if explicit:
        return {
            "cover": LAYOUT_TITLE,
            "title_content": LAYOUT_TITLE_CONTENT,
            "section": LAYOUT_SECTION,
            "title_only": LAYOUT_TITLE_ONLY,
            "blank": LAYOUT_BLANK,
        }.get(explicit, LAYOUT_TITLE_CONTENT)
    if spec.get("subtitle"):
        return LAYOUT_TITLE
    if not spec.get("bullets") and not spec.get("body") and spec.get("title"):
        return LAYOUT_SECTION
    return LAYOUT_TITLE_CONTENT


def _style_title(title_shape: Any) -> None:
    _P, _Emu, _Inches, Pt = _pptx_imports()
    tf = title_shape.text_frame
    for para in tf.paragraphs:
        for run in para.runs:
            run.font.size = Pt(36)
            run.font.bold = True


def _fill_bullets(placeholder: Any, bullets: list) -> None:
    _P, _Emu, _Inches, Pt = _pptx_imports()
    tf = placeholder.text_frame
    tf.clear()
    first = True
    for raw in bullets:
        text = str(raw)
        indent = 0
        # Honor a leading "  " (2-space) indent per level for nesting.
        while text.startswith("  "):
            text = text[2:]
            indent += 1
        para = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        para.text = text
        para.level = min(indent, 4)
        for run in para.runs:
            run.font.size = Pt(20 - indent * 2)


def _fill_body(placeholder: Any, text: str) -> None:
    _P, _Emu, _Inches, Pt = _pptx_imports()
    tf = placeholder.text_frame
    tf.clear()
    lines = text.splitlines() or [""]
    first = True
    for line in lines:
        para = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        para.text = line
        for run in para.runs:
            run.font.size = Pt(18)


def _markdown_to_slide_specs(md: str) -> list[dict]:
    lines = md.splitlines()
    slides: list[dict] = []
    current: Optional[dict] = None
    cover_done = False

    def flush() -> None:
        if current is not None:
            slides.append(current)

    section_pending = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if line.strip() == "---":
            section_pending = True
            continue

        h1 = re.match(r"^#\s+(.*)", line)
        h2 = re.match(r"^##\s+(.*)", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)", line)

        if h1:
            flush()
            heading = h1.group(1).strip()
            if not cover_done:
                current = {"title": heading, "layout": "cover"}
                cover_done = True
            elif section_pending:
                current = {"title": heading, "layout": "section"}
                section_pending = False
            else:
                current = {"title": heading, "bullets": [], "layout": "title_content"}
            continue

        if current is None:
            current = {"title": "Overview", "bullets": [], "layout": "title_content"}

        if h2:
            current.setdefault("bullets", []).append(h2.group(1).strip())
            continue

        if bullet:
            indent_spaces = len(raw_line) - len(raw_line.lstrip(" "))
            level = indent_spaces // 2
            current.setdefault("bullets", []).append("  " * level + bullet.group(1).strip())
            continue

        if line.strip():
            # Treat orphan paragraph lines as bullet items so they show up.
            current.setdefault("bullets", []).append(line.strip())

    flush()
    return slides
