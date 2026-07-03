from typing import Optional

from .file_ops import _validate_path
from .security import require_unlocked


def render_math(
    expression: str,
    output_path: str,
    fontsize: int = 28,
    dpi: int = 200,
    transparent: bool = False,
) -> str:
    """Render a math expression to PNG using matplotlib's built-in mathtext.

    Uses matplotlib's mathtext (a TeX-like mini engine) so **no LaTeX or TeX
    distribution is required**. Supports most common math notation:
      - Fractions: $\\frac{a}{b}$
      - Integrals/sums: $\\int_0^\\infty$, $\\sum_{i=1}^n$
      - Greek: $\\alpha$, $\\Sigma$
      - Roots: $\\sqrt{x^2 + y^2}$
      - Sub/superscripts: $x_i^2$
      - Matrices and aligned environments are not supported (use a full LaTeX
        backend if you need them; mathtext is the lightweight path).

    Args:
        expression: Math expression. Wrap in $...$ as usual; the function adds
            them if missing.
        output_path: Output PNG path.
        fontsize: Base font size.
        dpi: Image resolution.
        transparent: Save with transparent background.

    Self-verify before reporting done: call read_image on output_path to confirm
    the formula rendered correctly (no cut-off glyphs or mathtext errors).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        out = _validate_path(output_path)
        if out.suffix.lower() not in {".png", ".svg", ".pdf"}:
            return "[render_math error: output_path must end with .png, .svg, or .pdf]"
        out.parent.mkdir(parents=True, exist_ok=True)
        expr = expression.strip()
        if not expr.startswith("$"):
            expr = f"${expr}$"
        fig = plt.figure(figsize=(0.01, 0.01))
        fig.text(0, 0, expr, fontsize=fontsize)
        fig.savefig(
            out,
            dpi=dpi,
            bbox_inches="tight",
            pad_inches=0.1,
            transparent=transparent,
            facecolor="none" if transparent else "white",
        )
        plt.close(fig)
        return f"Wrote {out}"
    except Exception as e:
        return f"[render_math error: {type(e).__name__}: {e}]"
