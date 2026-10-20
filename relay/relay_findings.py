# -*- coding: utf-8 -*-
"""Second-stage hand-off, SHADOW ONLY: when a single-stage fan-out child finishes, record what
a later stage COULD have been handed from it -- the findings carried forward -- next to nothing
being changed. Nothing acts on it.

WHERE IT SITS. A top-level goal that splits once produces N `subtask` children and one
aggregator (relay/fanout.py, relay/relay_fleet.py). Today each child's answer is carried into
the merge whole (capped to 1200 chars, relay/fanout.aggregation_prompt). There is no second
stage: a child that discovered a file path, confirmed a fact or hit an unresolved gap hands
that forward only inside its own 1200-char blob, mixed with prose. This module reads a
finished child's answer and extracts the findings a second stage would need -- discovered
paths, confirmed facts, unresolved gaps -- and records, in `shadow`, the hand-off text a
second-stage child's prompt WOULD have carried. It never builds that child, changes a prompt,
the queue or the merge.

WHAT THIS MODULE IS NOT. It is not a second stage. The setting `relay_findings` accepts
`off`, `shadow` and `on`; only `off` and `shadow` do anything here, and `on` is reserved and
behaves as `off` (there is no second-stage builder yet, and adding one is a later change with
its own review). In `off` nothing here reads a file or records. It never mutates its inputs.

WHAT A FINDING IS (kept small on purpose and stated so a reader can disagree). From a child's
answer text, line by line:
  * path     a line that names a saved artifact path (contains a path separator and a file
             extension, or is introduced by a path marker like "保存" / "path" / "ファイル").
  * fact     a line the child states as a confirmed count or result ("N件取得", "確認",
             "completed", a terminal check).
  * gap      a line that names something unresolved ("未取得", "未完了", "失敗", "unresolved",
             "TODO").
Lines that are none of these are dropped: a hand-off is the findings, not the whole report.
The extraction is pure and conservative -- it only ever returns substrings the child wrote.

HONESTY RULES. A write error is counted and swallowed: the recorder runs inside the worker's
child-settle path and must never raise into it. The log is bounded (one rotated copy) and
append-only. A row holds counts, lengths, finding KINDS and the hand-off text length only; it
carries the extracted finding lines (which are the child's own words) but never the parent
goal text, and never invents a line.
"""
from __future__ import annotations

import io
import json
import os
import re
import time

KEY = "relay_findings"
#: `on` is accepted by the setting but reserved: there is no second-stage builder yet, so it
#: behaves as `off` here (see hand_off, which only records under `shadow`).
MODES = ("off", "shadow", "on")

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".fleet", "relay_findings_shadow.jsonl")
#: The log is rotated to `<LOG>.1` (replacing the old copy) when it reaches this size, so the
#: records on disk never exceed about twice this.
MAX_BYTES = 1024 * 1024
#: Splits remembered so that a retried settle of one slice is one row. Bounds memory.
SEEN_MAX = 4096
#: Most finding lines carried into one hand-off record (a hand-off is a summary, not the whole
#: answer; a child that lists forty paths is summarised, not transcribed).
MAX_FINDINGS = 12
#: Longest single finding line kept (a paragraph is not a finding).
MAX_FINDING_CHARS = 300
#: Hand-off text is capped like the merge caps a child answer (fanout.aggregation_prompt).
HANDOFF_CAP = 1200
SCHEMA = 1

K_PATH, K_FACT, K_GAP = "path", "fact", "gap"

_STATE = {"recorded": 0, "with_findings": 0, "write_errors": 0}
_seen = set()
_settings_cache = {}


# ------------------------------------------------------------------ the setting

def mode(settings_path=None):
    """`relay_findings=` from settings.txt: "shadow", "on" or "off". Anything else reads as off.
    Re-read when the file changes (each_gate). Never raises."""
    try:
        path = settings_path
        if path is None:
            from tools.settings_path import settings_file
            path = settings_file()
        if not path:
            return "off"
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        hit = _settings_cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = "off"
        with io.open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(KEY + "="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = v if v in MODES else "off"
                    break
        _settings_cache[path] = (key, val)
        return val
    except Exception:
        return "off"


# ------------------------------------------------------------------ extraction (pure)

#: A line that points at a saved artifact: a path with a separator and a file extension, or a
#: line a path marker introduces. Deliberately narrow; a bare sentence is not a path.
_PATH_SEP = re.compile(r"[\\/][^\\/\s]+\.[A-Za-z0-9]{1,8}(?:\b|$)")
_PATH_MARK = re.compile(r"(?:保存|出力先|パス|ファイル(?:名|パス)?|path|saved to|wrote to|output)",
                        re.IGNORECASE)
#: A confirmed count or completion the child states as done.
_FACT_MARK = re.compile(r"(?:\d+\s*件|取得(?:済|完了|しました)|確認(?:済|しました|できました)"
                        r"|完了(?:しました)?|終端|completed|confirmed|fetched|retrieved)",
                        re.IGNORECASE)
#: An unresolved gap the child names.
_GAP_MARK = re.compile(r"(?:未取得|未完了|未解決|取得できな|失敗|エラー"
                       r"|unresolved|failed|missing|TODO|could not)", re.IGNORECASE)


def _classify(line):
    """The finding kind of one stripped line, or None when it is not a finding.

    Order is gap, then path, then fact: a line that both names a count and says it failed is a
    gap, because the hand-off a second stage most needs is the one that is not done."""
    s = line.strip()
    if not s:
        return None
    if _GAP_MARK.search(s):
        return K_GAP
    if _PATH_SEP.search(s) or _PATH_MARK.search(s):
        return K_PATH
    if _FACT_MARK.search(s):
        return K_FACT
    return None


def extract_findings(result_text):
    """[(kind, line)] a second stage could be handed from this child answer. Pure.

    Only lines the child actually wrote are returned, each trimmed to MAX_FINDING_CHARS and the
    list to MAX_FINDINGS. [] when the answer carries no finding -- never a synthesised one."""
    out = []
    for raw in (result_text or "").splitlines():
        kind = _classify(raw)
        if kind is None:
            continue
        s = raw.strip()
        if len(s) > MAX_FINDING_CHARS:
            s = s[:MAX_FINDING_CHARS]
        out.append((kind, s))
        if len(out) >= MAX_FINDINGS:
            break
    return out


def handoff_text(findings):
    """The text a second-stage child's prompt WOULD carry, built from these findings, or "" when
    there are none. Grouped by kind so a second stage reads gaps, paths and facts apart. Pure
    and bounded (HANDOFF_CAP). This is recorded, never queued."""
    if not findings:
        return ""
    by = {K_GAP: [], K_PATH: [], K_FACT: []}
    for kind, line in findings:
        by.setdefault(kind, []).append(line)
    parts = []
    labels = ((K_GAP, "未解決"), (K_PATH, "成果物"), (K_FACT, "確認済"))
    for kind, label in labels:
        if by.get(kind):
            parts.append("【%s】" % label)
            parts.extend("- " + ln for ln in by[kind])
    text = "\n".join(parts)
    return text[:HANDOFF_CAP]


# ------------------------------------------------------------------ the recorder

def _append(row):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        try:
            if os.path.getsize(LOG) >= MAX_BYTES:
                os.replace(LOG, LOG + ".1")
        except OSError:
            pass
        with io.open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception:
        _STATE["write_errors"] += 1
        return False


def hand_off(campaign_id, subtask_index, result_text, run_id="", settings_path=None, now=None):
    """Called by the worker once per child that ends DONE, after its `child_result` is written.
    In `shadow` it appends one row: the findings extracted from this child and the hand-off
    text a second stage would have carried. In `off` (and in reserved `on`) it returns at once.
    Never raises, never mutates its arguments, returns nothing."""
    try:
        if mode(settings_path) != "shadow":
            return None
        cid = str(campaign_id or "")
        ident = (str(run_id or ""), cid, subtask_index)
        if cid:
            if ident in _seen:
                return None          # a re-settle of one slice: one row
            if len(_seen) >= SEEN_MAX:
                _seen.clear()
            _seen.add(ident)
        findings = extract_findings(result_text)
        handoff = handoff_text(findings)
        kinds = {}
        for kind, _ in findings:
            kinds[kind] = kinds.get(kind, 0) + 1
        row = {
            "v": SCHEMA, "ts": round(time.time() if now is None else now, 1),
            "run_id": str(run_id or ""), "mode": "shadow", "campaign_id": cid,
            "subtask_index": subtask_index,
            "findings": [{"kind": k, "line": ln} for k, ln in findings],
            "kinds": kinds,
            "result_chars": len(result_text or ""),
            "handoff_chars": len(handoff),
            "handoff": handoff,
        }
        if _append(row):
            _STATE["recorded"] += 1
            _STATE["with_findings"] += 1 if findings else 0
        return None
    except Exception:
        return None


def status_block(settings_path=None):
    """{"relay_findings": {mode, recorded, with_findings}} for status.json (additive)."""
    return {KEY: {"mode": mode(settings_path), "recorded": int(_STATE["recorded"]),
                  "with_findings": int(_STATE["with_findings"])}}


def _reset_for_test():
    _STATE.update({"recorded": 0, "with_findings": 0, "write_errors": 0})
    _seen.clear()
    _settings_cache.clear()
