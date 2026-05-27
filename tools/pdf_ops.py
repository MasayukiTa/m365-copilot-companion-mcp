from typing import Optional

from .file_ops import _validate_path


def pdf_info(path: str) -> str:
    """Return basic metadata about a PDF (page count, title, author, encrypted)."""
    try:
        from pypdf import PdfReader

        p = _validate_path(path)
        if not p.is_file():
            return f"[pdf_info error: not a file: {p}]"
        reader = PdfReader(str(p))
        meta = reader.metadata or {}
        lines = [
            f"path: {p}",
            f"pages: {len(reader.pages)}",
            f"encrypted: {reader.is_encrypted}",
        ]
        for key in ("/Title", "/Author", "/Subject", "/Creator", "/Producer", "/CreationDate"):
            value = meta.get(key)
            if value:
                lines.append(f"{key.lstrip('/').lower()}: {value}")
        return "\n".join(lines)
    except Exception as e:
        return f"[pdf_info error: {type(e).__name__}: {e}]"


def read_pdf(
    path: str,
    pages: Optional[str] = None,
    max_chars: int = 60_000,
) -> str:
    """Extract plain text from a PDF.

    Args:
        path: PDF path.
        pages: Optional page range, e.g. "1-5", "3", or "1,3,5-7". 1-indexed.
            Omit to read the entire document.
        max_chars: Truncate output to this many characters.
    """
    try:
        from pypdf import PdfReader

        p = _validate_path(path)
        if not p.is_file():
            return f"[read_pdf error: not a file: {p}]"
        reader = PdfReader(str(p))
        total = len(reader.pages)
        page_indices = _parse_page_spec(pages, total) if pages else list(range(total))
        chunks: list[str] = []
        for idx in page_indices:
            if idx < 0 or idx >= total:
                continue
            try:
                text = reader.pages[idx].extract_text() or ""
            except Exception as e:
                text = f"[error on page {idx + 1}: {e}]"
            chunks.append(f"--- page {idx + 1} ---\n{text.strip()}")
        out = "\n\n".join(chunks)
        if len(out) > max_chars:
            out = out[:max_chars] + f"\n... truncated at {max_chars:,} characters"
        return out if out else "(no extractable text — PDF may be scanned)"
    except Exception as e:
        return f"[read_pdf error: {type(e).__name__}: {e}]"


def _parse_page_spec(spec: str, total: int) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                lo = int(a) - 1
                hi = int(b) - 1
            except ValueError:
                continue
            for i in range(lo, hi + 1):
                if 0 <= i < total:
                    out.append(i)
        else:
            try:
                i = int(part) - 1
                if 0 <= i < total:
                    out.append(i)
            except ValueError:
                continue
    return out
