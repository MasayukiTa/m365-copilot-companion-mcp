import base64
import zlib
from pathlib import Path
from typing import Optional

import httpx

from .file_ops import _validate_path
from .security import require_unlocked

KROKI_BASE = "https://kroki.io"
SUPPORTED = {
    "mermaid",
    "graphviz",
    "plantuml",
    "blockdiag",
    "seqdiag",
    "actdiag",
    "nwdiag",
    "c4plantuml",
    "structurizr",
    "excalidraw",
    "bpmn",
    "d2",
}


def render_diagram(
    kind: str,
    source: str,
    output_path: str,
    fmt: str = "png",
    timeout: int = 30,
    verify_ssl: bool = True,
) -> str:
    """Render a diagram (mermaid / graphviz / plantuml / d2 etc.) to a file via Kroki.

    No local dependency needed — calls the public Kroki rendering service.

    Args:
        kind: Diagram language. One of: mermaid, graphviz, plantuml, d2,
            blockdiag, seqdiag, actdiag, nwdiag, c4plantuml, structurizr,
            excalidraw, bpmn.
        source: Diagram source text. For mermaid use the mermaid DSL.
        output_path: Where to save the rendered file (.png or .svg recommended).
        fmt: Output format. "png" or "svg" or "pdf".
        timeout: HTTP timeout in seconds.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        kind_l = kind.lower().strip()
        if kind_l not in SUPPORTED:
            return (
                f"[render_diagram error: unsupported kind {kind!r}. "
                f"Supported: {sorted(SUPPORTED)}]"
            )
        fmt_l = fmt.lower().strip()
        if fmt_l not in {"png", "svg", "pdf"}:
            return "[render_diagram error: fmt must be png, svg, or pdf]"
        out = _validate_path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        compressed = zlib.compress(source.encode("utf-8"), 9)
        encoded = base64.urlsafe_b64encode(compressed).decode("ascii")
        url = f"{KROKI_BASE}/{kind_l}/{fmt_l}/{encoded}"

        with httpx.Client(follow_redirects=True, timeout=timeout, verify=verify_ssl) as client:
            r = client.get(url)
        if r.status_code != 200:
            body_preview = r.text[:400]
            return f"[render_diagram error: HTTP {r.status_code} from Kroki\n{body_preview}]"
        out.write_bytes(r.content)
        return f"Wrote {out} ({len(r.content):,} bytes, {kind_l}->{fmt_l})"
    except httpx.HTTPError as e:
        return f"[render_diagram HTTP error: {type(e).__name__}: {e}]"
    except Exception as e:
        return f"[render_diagram error: {type(e).__name__}: {e}]"


def render_mermaid_png(source: str, output_path: str, timeout: int = 30) -> str:
    """Shortcut: render a mermaid diagram to PNG. Best for embedding in PowerPoint slides.

    Args:
        source: Mermaid DSL source.
        output_path: Output .png path.
        timeout: HTTP timeout.
    """
    return render_diagram("mermaid", source, output_path, fmt="png", timeout=timeout)
