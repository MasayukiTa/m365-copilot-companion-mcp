# -*- coding: utf-8 -*-
"""Self-contained HTML: CSS only, no scripts, no external resources."""
from __future__ import annotations

import html as _html

from . import labels as L
from .render_md import merged_rows, status_text
from .timeutil import fmt_dt, fmt_duration, fmt_time

CSS = """
:root{--bg:#ffffff;--fg:#1d2733;--muted:#5c6b7a;--line:#d5dbe2;--card:#f6f8fa;--ok:#2e9e5b;
--error:#d33a3a;--refused:#e08a00;--unavailable:#7b8794;--no_outcome:#8a4fd1;--gap:#d33a3a}
@media (prefers-color-scheme:dark){:root{--bg:#14181d;--fg:#e4e9ee;--muted:#9aa7b4;--line:#2b333c;
--card:#1b2127;--ok:#47c079;--error:#ef6a6a;--refused:#f0a73a;--unavailable:#97a3af;--no_outcome:#b387ee;--gap:#ef6a6a}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Segoe UI","Yu Gothic UI","Hiragino Sans",sans-serif}
h1{font-size:20px;margin:0 0 8px}h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.meta{color:var(--muted);margin:0 0 4px}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
th,td{border:1px solid var(--line);padding:3px 6px;text-align:left;vertical-align:top;word-break:break-word}
th{background:var(--card)}
.num{text-align:right;white-space:nowrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px;margin:10px 0}
.axis{position:relative;border-left:1px solid var(--line);border-right:1px solid var(--line);background:var(--bg);margin:6px 0 2px}
.bar{position:absolute;height:12px;min-width:3px;border-radius:2px}
.bar.ok{background:var(--ok)}.bar.error{background:var(--error)}.bar.refused{background:var(--refused)}
.bar.unavailable{background:var(--unavailable)}.bar.no_outcome{background:var(--no_outcome)}
.gapbox{position:absolute;top:0;bottom:0;background:repeating-linear-gradient(45deg,transparent 0 5px,var(--gap) 5px 7px);opacity:.35;border-left:1px solid var(--gap);border-right:1px solid var(--gap)}
.gaplabel{position:absolute;top:0;font-size:11px;color:var(--gap);font-weight:600;white-space:nowrap;padding-left:2px}
.ticks{position:relative;height:16px;color:var(--muted);font-size:11px}
.tick{position:absolute;border-left:1px solid var(--line);padding-left:2px;height:16px}
.ev{position:absolute;width:2px;background:var(--muted)}
.st{font-weight:600;white-space:nowrap}.st.ok{color:var(--ok)}.st.error{color:var(--error)}.st.refused{color:var(--refused)}
.st.unavailable{color:var(--unavailable)}.st.no_outcome{color:var(--no_outcome)}
tr.gaprow td{background:repeating-linear-gradient(45deg,transparent 0 6px,rgba(211,58,58,.18) 6px 8px);font-weight:600}
tr.eventrow td{color:var(--muted);font-style:italic}
.legend span{margin-right:12px;white-space:nowrap}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:middle}
.sw.ok{background:var(--ok)}.sw.error{background:var(--error)}.sw.refused{background:var(--refused)}
.sw.unavailable{background:var(--unavailable)}.sw.no_outcome{background:var(--no_outcome)}
code{font-family:ui-monospace,Consolas,monospace;font-size:12px}
@media (max-width:640px){body{padding:16px 16px}table{font-size:12px}}
"""


def _e(x) -> str:
    return _html.escape(str(x if x is not None else ""), quote=True)


def _lanes(steps):
    """Greedy lane assignment so overlapping (parallel) calls do not draw on top of each other."""
    ends, out = [], {}
    for s in sorted(steps, key=lambda s: s["start"]):
        for i, e in enumerate(ends):
            if s["start"] >= e:
                ends[i] = max(s["end"], s["start"] + 0.5)
                out[s["id"]] = i
                break
        else:
            ends.append(max(s["end"], s["start"] + 0.5))
            out[s["id"]] = len(ends) - 1
    return out, max(1, len(ends))


def _ticks(t0, t1, tz) -> str:
    span = max(t1 - t0, 1.0)
    step = next((s for s in (10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400)
                 if span / s <= 12), 86400)
    first = (int(t0) // step + 1) * step
    parts = []
    t = first
    while t < t1:
        pct = (t - t0) / span * 100.0
        parts.append('<div class="tick" style="left:%.3f%%">%s</div>' % (pct, _e(fmt_time(t, tz))))
        t += step
    return '<div class="ticks">%s</div>' % "".join(parts)


def _timeline(sess, t0, t1, tz, lab, lang) -> str:
    span = max(t1 - t0, 1.0)
    lanes, n_lanes = _lanes(sess["steps"])
    height = n_lanes * 16 + 22
    parts = []
    for g in sess["gaps"]:
        left = (g["start"] - t0) / span * 100.0
        width = max((g["end"] - g["start"]) / span * 100.0, 0.2)
        parts.append('<div class="gapbox" style="left:%.3f%%;width:%.3f%%" title="%s"></div>'
                     '<div class="gaplabel" style="left:%.3f%%">%s</div>'
                     % (left, width, _e("%s %s" % (lab["gap_line"], fmt_duration(g["shown_s"], lang))),
                        left, _e(fmt_duration(g["shown_s"], lang))))
    for s in sess["steps"]:
        left = (s["start"] - t0) / span * 100.0
        width = max(((s["end"] - s["start"]) / span * 100.0), 0.0)
        tip = "%s %s  %s  %s" % (fmt_time(s["start"], tz), s["tool"], status_text(s, lab),
                                 fmt_duration(s["dur"], lang) if s["dur"] is not None else "")
        parts.append('<div class="bar %s" style="left:%.3f%%;width:%.3f%%;top:%dpx" title="%s"></div>'
                     % (s["status"], left, width, 20 + lanes[s["id"]] * 16, _e(tip)))
    for e in sess["events"]:
        left = (e["ts"] - t0) / span * 100.0
        parts.append('<div class="ev" style="left:%.3f%%;top:0;height:16px" title="%s"></div>'
                     % (left, _e("%s %s" % (fmt_time(e["ts"], tz), lab["event_kinds"].get(e["kind"], e["kind"])))))
    return '%s<div class="axis" style="height:%dpx">%s</div>' % (_ticks(t0, t1, tz), height, "".join(parts))


def render_html(report, tz, lang="ja", max_steps=None) -> str:
    lab = L.get(lang)
    meta = report["meta"]
    sessions = report["sessions"]
    o = ['<!doctype html><html lang="%s"><head><meta charset="utf-8">' % ("ja" if lang == "ja" else "en"),
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         "<title>%s</title><style>%s</style></head><body>" % (_e(lab["title"]), CSS),
         "<h1>%s</h1>" % _e(lab["title"])]
    since = fmt_dt(meta["since"], tz) if meta.get("since") else "-"
    until = fmt_dt(meta["until"], tz) if meta.get("until") else "-"
    o.append('<p class="meta">%s: %s .. %s &nbsp; %s: %s &nbsp; %s: %s</p>' % (
        _e(lab["window"]), _e(since), _e(until), _e(lab["gap_threshold"]),
        _e(fmt_duration(meta["gap_s"], lang)), _e(lab["sessions"]), meta["n_sessions"]))
    o.append('<p class="meta">%s<br>%s</p>' % (_e(lab["withheld_note"]), _e(lab["limits"])))
    o.append('<p class="legend">%s</p>' % "".join(
        '<span><i class="sw %s"></i>%s</span>' % (k, _e(lab[k])) for k in
        ("ok", "error", "refused", "unavailable", "no_outcome")))
    if not sessions:
        o.append("<p>%s</p></body></html>" % _e(lab["no_sessions"]))
        return "".join(o)
    t0 = min(s["first"] for s in sessions)
    t1 = max(s["last"] for s in sessions)
    if meta.get("since"):
        t0 = min(t0, meta["since"])
    if meta.get("until"):
        t1 = max(t1, meta["until"])
    o.append("<h2>%s</h2><table><tr>%s</tr>" % (_e(lab["sessions"]), "".join(
        "<th>%s</th>" % _e(h) for h in (lab["session"], lab["first"], lab["last"], lab["calls"], lab["ok"],
                                        lab["error"], lab["refused"], lab["gaps"], lab["mode"], lab["findings"]))))
    for i, s in enumerate(sessions):
        st = s["stats"]["status"]
        o.append('<tr><td><a href="#s%d"><code>%s</code></a></td><td>%s</td><td>%s</td>%s</tr>' % (
            i, _e(s["id"] or "(none)"), _e(fmt_time(s["first"], tz)), _e(fmt_time(s["last"], tz)),
            "".join('<td class="num">%s</td>' % v for v in (
                len(s["steps"]), st.get("ok", 0), st.get("error", 0), st.get("refused", 0), len(s["gaps"]))) +
            "<td>%s</td><td class=\"num\">%d</td>" % (_e(lab[s["concurrency"]["mode"]]), len(s["findings"]))))
    o.append("</table>")
    if report["findings"]:
        o.append("<h2>%s</h2><table><tr><th>%s</th><th>%s</th><th>%s</th><th>detail</th></tr>" % (
            _e(lab["findings"]), _e(lab["time"]), _e(lab["session"]), _e(lab["status"])))
        for f in report["findings"]:
            sec = fmt_duration(f["seconds"], lang) if f.get("seconds") else ""
            o.append("<tr><td>%s</td><td><code>%s</code></td><td>%s</td><td>%s</td></tr>" % (
                _e(fmt_time(f["ts"], tz)), _e(f["session"] or "(none)"),
                _e(lab["kinds"].get(f["kind"], f["kind"])), _e((sec + " " + f.get("detail", "")).strip())))
        o.append("</table>")
    o.append("<h2>%s</h2><table><tr><th>%s</th><th>%s</th><th>%s</th><th>%s</th><th>%s</th><th>%s</th></tr>" % (
        _e(lab["by_tool"]), _e(lab["tool"]), _e(lab["calls"]), _e(lab["ok"]), _e(lab["error"]),
        _e(lab["refused"]), "sum / max s"))
    for t, v in sorted(report["stats"]["by_tool"].items(), key=lambda kv: -kv[1]["calls"]):
        o.append('<tr><td>%s</td><td class="num">%d</td><td class="num">%d</td><td class="num">%d</td>'
                 '<td class="num">%d</td><td class="num">%.1f / %.1f</td></tr>' % (
                     _e(t), v["calls"], v["ok"], v["error"], v["refused"], v["total_s"], v["max_s"]))
    o.append("</table>")
    if report["stats"]["reasons"]:
        o.append("<table><tr><th>%s</th><th>%s</th></tr>" % (_e(lab["by_reason"]), _e(lab["calls"])))
        for k, v in report["stats"]["reasons"].items():
            o.append('<tr><td>%s</td><td class="num">%d</td></tr>' % (_e(lab["reasons"].get(k, k)), v))
        o.append("</table>")
    for i, sess in enumerate(sessions):
        o.append('<div class="card" id="s%d"><h2>%s <code>%s</code></h2>' % (
            i, _e(lab["session"]), _e(sess["id"] or "(none)")))
        o.append("<p class=\"meta\">%s .. %s &nbsp; %s %d &nbsp; %s: %s (%s %d)</p>" % (
            _e(fmt_dt(sess["first"], tz)), _e(fmt_time(sess["last"], tz)), _e(lab["calls"]), len(sess["steps"]),
            _e(lab["mode"]), _e(lab[sess["concurrency"]["mode"]]), _e(lab["max_overlap"]),
            sess["concurrency"]["max_overlap"]))
        for f in sess["findings"]:
            if f["kind"] != "gap":
                o.append("<p><strong>%s</strong>: %s</p>" % (
                    _e(lab["kinds"].get(f["kind"], f["kind"])), _e(f.get("detail", ""))))
        for sub in sess["submits"]:
            o.append("<p class=\"meta\">fleet_submit #%d %s: %s %s</p>" % (
                sub["n"], _e(fmt_time(sub["start"], tz)), _e(sub["result"]),
                _e(lab["reasons"].get(sub["reason"], sub["reason"]) if sub["reason"] else sub.get("job", ""))))
        if sess["steps"] or sess["events"]:
            o.append(_timeline(sess, t0, t1, tz, lab, lang))
        o.append("<table><tr>%s</tr>" % "".join("<th>%s</th>" % _e(h) for h in (
            "#", lab["time"], lab["delta"], lab["tool"], lab["args"], lab["status"], lab["dur"], lab["result"])))
        prev_end, shown = None, 0
        for ts, _order, kind, p in merged_rows(sess):
            if max_steps is not None and shown >= max_steps:
                break
            shown += 1
            if kind == "step":
                delta = fmt_duration(p["start"] - prev_end, lang) if prev_end is not None else ""
                prev_end = max(p["end"], prev_end or 0)
                o.append('<tr><td class="num">%d</td><td>%s</td><td>%s</td><td><code>%s</code></td><td>%s</td>'
                         '<td class="st %s">%s</td><td class="num">%s</td><td>%s</td></tr>' % (
                             p["n"], _e(fmt_time(p["start"], tz)), _e(delta), _e(p["tool"]),
                             _e("; ".join(p["args"])), p["status"], _e(status_text(p, lab)),
                             _e(fmt_duration(p["dur"], lang) if p["dur"] is not None else ""),
                             _e(p["result"] or p["error"])))
            elif kind == "event":
                o.append('<tr class="eventrow"><td></td><td>%s</td><td></td><td colspan="5">(%s) %s</td></tr>' % (
                    _e(fmt_time(ts, tz)), _e(lab["event_kinds"].get(p["kind"], p["kind"])),
                    _e(p.get("method") or p.get("site") or p.get("via") or "")))
            else:
                o.append('<tr class="gaprow"><td></td><td>%s-%s</td><td></td><td colspan="5">%s %s &nbsp; '
                         '%s: %s / %s: %s / %s: %d</td></tr>' % (
                             _e(fmt_time(p["start"], tz)), _e(fmt_time(p["end"], tz)), _e(lab["gap_line"]),
                             _e(fmt_duration(p["shown_s"], lang)), _e(lab["gap_after"]), _e(p["after_tool"]),
                             _e(lab["gap_before"]), _e(p["before_tool"]), _e(lab["other_calls"]),
                             p["other_sessions_calls"]))
        o.append("</table></div>")
    o.append("</body></html>")
    return "".join(o)
