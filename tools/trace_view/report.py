# -*- coding: utf-8 -*-
"""Glue: load -> build -> render. Used by the CLI and by the MCP tool."""
from __future__ import annotations

import os
import re

from . import build as _build
from . import load as _load
from .render_html import render_html
from .render_md import render_markdown
from .render_xlsx import render_xlsx
from .timeutil import fmt_dt

FORMATS = ("md", "html", "xlsx")


def make_report(state_dir=None, since=None, until=None, session=None,
                gap_s: float = _build.DEFAULT_GAP_S) -> dict:
    loaded = _load.load(state_dir, since, until, session)
    report = _build.build(loaded, since, until, gap_s)
    if session:
        report = _build.filter_sessions(report, str(session))
        report["meta"]["n_sessions"] = len(report["sessions"])
        report["meta"]["n_steps"] = sum(len(s["steps"]) for s in report["sessions"])
    return report


def _stem(report, tz) -> str:
    def part(ts):
        return re.sub(r"[^0-9]", "", fmt_dt(ts, tz))[:12] if ts else "x"
    return "trace_%s_%s" % (part(report["meta"].get("since")), part(report["meta"].get("until")))


def write_outputs(report, out_dir, formats, tz, lang="ja") -> list:
    """Write the requested formats into `out_dir`; returns the paths written."""
    os.makedirs(out_dir, exist_ok=True)
    stem = _stem(report, tz)
    written = []
    for fmt in formats:
        if fmt not in FORMATS:
            raise ValueError("unknown format %r (use md, html, xlsx)" % fmt)
        path = os.path.join(out_dir, "%s.%s" % (stem, fmt))
        if fmt == "md":
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(render_markdown(report, tz, lang))
        elif fmt == "html":
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(render_html(report, tz, lang))
        else:
            render_xlsx(report, tz, path, lang)
        written.append(path)
    return written
