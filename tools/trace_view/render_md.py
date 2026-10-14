# -*- coding: utf-8 -*-
"""Markdown time-line report."""
from __future__ import annotations

from . import labels as L
from .timeutil import fmt_dt, fmt_duration, fmt_time


def _cell(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def status_text(step, lab) -> str:
    text = lab[step["status"]]
    if step["reason"]:
        text += " / " + lab["reasons"].get(step["reason"], step["reason"])
    return text


def merged_rows(sess):
    """Steps, events and gaps of one session in time order: (ts, order, kind, payload)."""
    rows = [(s["start"], 1, "step", s) for s in sess["steps"]]
    rows += [(e["ts"], 0, "event", e) for e in sess["events"]]
    rows += [(g["start"], 2, "gap", g) for g in sess["gaps"]]
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def _table(header, rows) -> list:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out


def render_markdown(report, tz, lang="ja", max_steps=None, max_sessions=None) -> str:
    lab = L.get(lang)
    meta = report["meta"]
    out = ["# " + lab["title"], ""]
    since = fmt_dt(meta["since"], tz) if meta.get("since") else "-"
    until = fmt_dt(meta["until"], tz) if meta.get("until") else "-"
    out.append("- %s: %s .. %s" % (lab["window"], since, until))
    out.append("- %s: %s / %s: %s" % (lab["tz"], getattr(tz, "key", None) or str(tz.tzname(None) or tz),
                                      lab["gap_threshold"], fmt_duration(meta["gap_s"], lang)))
    out.append("- %s: %d / %s: %d" % (lab["sessions"], meta["n_sessions"], lab["calls"], meta["n_steps"]))
    out += ["- " + lab["withheld_note"], "- " + lab["limits"], ""]

    sessions = report["sessions"]
    if not sessions:
        out += [lab["no_sessions"], ""]
        return "\n".join(out)

    out += ["## " + lab["sessions"], ""]
    header = [lab["session"], lab["first"], lab["last"], lab["calls"], lab["ok"], lab["error"],
              lab["refused"], lab["gaps"], lab["mode"], lab["findings"]]
    rows = []
    for s in sessions:
        st = s["stats"]["status"]
        rows.append([s["id"] or "(none)", fmt_time(s["first"], tz), fmt_time(s["last"], tz),
                     len(s["steps"]), st.get("ok", 0), st.get("error", 0), st.get("refused", 0),
                     len(s["gaps"]), lab[s["concurrency"]["mode"]], len(s["findings"])])
    out += _table(header, rows) + [""]

    if report["findings"]:
        out += ["## " + lab["findings"], ""]
        rows = []
        for f in report["findings"]:
            sec = fmt_duration(f["seconds"], lang) if f.get("seconds") else ""
            rows.append([fmt_time(f["ts"], tz), f["session"] or "(none)",
                         lab["kinds"].get(f["kind"], f["kind"]), (sec + " " + f.get("detail", "")).strip()])
        out += _table([lab["time"], lab["session"], lab["status"], "detail"], rows) + [""]

    out += ["## " + lab["stats"], ""]
    by_tool = report["stats"]["by_tool"]
    rows = [[t, v["calls"], v["ok"], v["error"], v["refused"], v["unavailable"], v["no_outcome"],
             round(v["total_s"], 1), round(v["max_s"], 1)]
            for t, v in sorted(by_tool.items(), key=lambda kv: -kv[1]["calls"])]
    out += ["### " + lab["by_tool"], ""]
    out += _table([lab["tool"], lab["calls"], lab["ok"], lab["error"], lab["refused"],
                   lab["unavailable"], lab["no_outcome"], "sum s", "max s"], rows) + [""]
    out += ["### %s / %s" % (lab["by_status"], lab["by_reason"]), ""]
    rows = [[lab[k], v] for k, v in report["stats"]["status"].items()]
    rows += [[lab["reasons"].get(k, k), v] for k, v in report["stats"]["reasons"].items()]
    out += _table([lab["status"], lab["calls"]], rows) + [""]

    if report["global_gaps"]:
        out += ["### " + lab["global_gaps"], ""]
        rows = [[fmt_time(g["start"], tz), fmt_time(g["end"], tz), fmt_duration(g["shown_s"], lang),
                 g["after_tool"], g["before_tool"]] for g in report["global_gaps"]]
        out += _table([lab["first"], lab["last"], lab["gap_line"], lab["gap_after"], lab["gap_before"]], rows) + [""]

    shown = sessions if not max_sessions else sessions[:max_sessions]
    for sess in shown:
        out += ["## %s %s" % (lab["session"], sess["id"] or "(none)"), ""]
        st = sess["stats"]["status"]
        out.append("- %s .. %s / %s %d (%s) / %s %s (%s %d)" % (
            fmt_dt(sess["first"], tz), fmt_time(sess["last"], tz), lab["calls"], len(sess["steps"]),
            ", ".join("%s %d" % (lab[k], v) for k, v in st.items()) or "-",
            lab["mode"], lab[sess["concurrency"]["mode"]], lab["max_overlap"],
            sess["concurrency"]["max_overlap"]))
        con = sess["connect"]
        if con["methods"]:
            out.append("- %s: %s (ua: %s)" % (lab["connect"], ", ".join(
                "%s x%d" % (m, con["counts"][m]) for m in con["methods"]), con["ua"] or "-"))
        for sub in sess["submits"]:
            out.append("- fleet_submit #%d %s: %s%s" % (
                sub["n"], fmt_time(sub["start"], tz), sub["result"],
                (" (" + lab["reasons"].get(sub["reason"], sub["reason"]) + ")") if sub["reason"] else
                ((" job " + sub["job"]) if sub.get("job") else "")))
        for f in sess["findings"]:
            if f["kind"] != "gap":
                out.append("- **%s**: %s" % (lab["kinds"].get(f["kind"], f["kind"]), f.get("detail", "")))
        out.append("")
        header = ["#", lab["time"], lab["delta"], lab["tool"], lab["args"], lab["status"], lab["dur"], lab["result"]]
        rows, prev_end, n_shown = [], None, 0
        for ts, _order, kind, p in merged_rows(sess):
            if kind == "step":
                if max_steps is not None and n_shown >= max_steps:
                    continue
                n_shown += 1
                delta = fmt_duration(p["start"] - prev_end, lang) if prev_end is not None else ""
                prev_end = max(p["end"], prev_end or 0)
                result = p["result"] or p["error"]
                rows.append([p["n"], fmt_time(p["start"], tz), delta, p["tool"], "; ".join(p["args"]),
                             status_text(p, lab), fmt_duration(p["dur"], lang) if p["dur"] is not None else "",
                             result])
            elif kind == "event":
                if max_steps is not None and n_shown >= max_steps:
                    continue
                rows.append(["", fmt_time(ts, tz), "", "(%s)" % lab["event_kinds"].get(p["kind"], p["kind"]),
                             p.get("method") or p.get("site") or p.get("via") or "", "", "", ""])
            else:
                if max_steps is not None and n_shown >= max_steps:
                    continue
                rows.append(["", "%s-%s" % (fmt_time(p["start"], tz), fmt_time(p["end"], tz)), "",
                             "**%s %s**" % (lab["gap_line"], fmt_duration(p["shown_s"], lang)),
                             "%s: %s / %s: %s / %s: %d" % (lab["gap_after"], p["after_tool"],
                                                          lab["gap_before"], p["before_tool"],
                                                          lab["other_calls"], p["other_sessions_calls"]),
                             "", "", ""])
        out += _table(header, rows)
        if max_steps is not None and len(sess["steps"]) > max_steps:
            out += ["", lab["more_steps"] % (len(sess["steps"]) - max_steps)]
        out.append("")
    if max_sessions and len(sessions) > max_sessions:
        out.append(lab["more_steps"] % (len(sessions) - max_sessions) + " (%s)" % lab["sessions"])
    if report["events_global"]:
        out += ["## " + lab["global_events"], ""]
        rows = [[fmt_time(e["ts"], tz), lab["event_kinds"].get(e["kind"], e["kind"]),
                 e.get("reason") or e.get("stage") or e.get("site") or ""] for e in report["events_global"][:200]]
        out += _table([lab["time"], lab["status"], "detail"], rows) + [""]
    h = meta.get("health") or {}
    if h:
        out += ["## " + lab["health"], ""]
        out += _table(["file", "present", "rows", "bad lines", "rows without time"],
                      [[k, v.get("present"), v.get("rows"), v.get("bad"), v.get("no_ts", 0)]
                       for k, v in h.items()]) + [""]
    return "\n".join(out)
