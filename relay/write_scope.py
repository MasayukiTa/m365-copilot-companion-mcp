# -*- coding: utf-8 -*-
"""Do two siblings of one fan-out campaign write the same path?  SHADOW ONLY: it records.

WHY THIS EXISTS. Each child of a split is told, in its prompt, to stay inside its own slice.
Nothing checks that it did. Two siblings editing one file is the way a split silently loses
work at the merge, and until something counts how often it happens there is no way to say
whether the prompt line is enough.

WHAT THIS MODULE IS NOT. It never blocks, never edits a prompt and never changes an output.
The setting `fanout_write_scope` accepts `off` and `shadow` and nothing else: enforcing a write
scope would run through the folder policy, which is frozen and needs its own approval, so no
enforcing value exists to be set by mistake. In `off` nothing here reads a ledger or records.

THE HONESTY RULES (the same ones scripts/sibling_dup_report.py keeps).

* A step that names no path is "no declaration". That is neither a success nor a violation.
* Only calls to tools whose NAME says they write a file are write calls. run_python and
  shell_exec can write anything and cannot be classified from their arguments, so they are
  counted as "unknown write" and never flagged.
* A tool_events row is used only when its attribution kind is trustworthy and it resolves to
  exactly one child of one campaign. Anything else is counted as "unknown" and skipped.
"""
from __future__ import annotations

import io
import json
import os
import re

KEY = "fanout_write_scope"
#: ONLY these. There is deliberately no enforcing value (see the module docstring).
MODES = ("off", "shadow")

#: Tools whose name says they write a file, and which argument carries the path.
WRITE_TOOLS = {"write_file", "append_file", "replace_in_file", "multi_edit", "edit_and_verify"}
#: Tools that may write but cannot be classified from their arguments.
UNKNOWN_WRITE_TOOLS = {"run_python", "shell_exec", "shell", "pwsh_exec", "code_exec"}

#: Attribution kinds under which a tool_events row names its task/worker trustworthily
#: (tools/tool_ledger.py `attr`). `ambiguous` and "" are deliberately absent.
ATTRIBUTED_KINDS = ("explicit", "session", "window", "session-window")

#: How much of the end of tool_events.jsonl one scan reads.
TAIL_BYTES = 8 * 1024 * 1024

_EXT = (r"py|pyi|cs|md|json|jsonl|txt|yml|yaml|toml|ini|cfg|js|jsx|ts|tsx|html|htm|css|ps1|bat|sh|"
        r"sql|csv|tsv|xml|xlsx|xlsm|docx|pptx|pdf|c|h|cpp|hpp|java|go|rs|rb|php|lock")
_ASCII = r"[A-Za-z0-9_.\-]"
# A path with at least one separator, optionally rooted. Accepted only when it is rooted, ends
# in a known extension, or ends in a separator (so "and/or" is not a path).
_SLASHED = re.compile(
    r"(?<![A-Za-z0-9_.\-/\\:])((?:[A-Za-z]:)?(?:\.{1,2}|~)?[\\/]?(?:%s+[\\/])+(?:%s+)?)" % (_ASCII, _ASCII))
_BARE_FILE = re.compile(r"(?<![A-Za-z0-9_.\-/\\])(%s+\.(?:%s))(?![A-Za-z0-9_\-])" % (_ASCII, _EXT), re.I)
_QUOTED = re.compile(r"\"([^\"\n]{1,260})\"|`([^`\n]{1,260})`|「([^」\n]{1,260})」|『([^』\n]{1,260})』"
                     r"|(?<![A-Za-z0-9])'([^'\n]{1,260})'(?![A-Za-z0-9])")
_HAS_EXT = re.compile(r"\.(?:%s)$" % _EXT, re.I)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+")


def norm_path(p, base=""):
    """Comparable form of a path: forward slashes, lower case, no ./, a trailing / kept (that
    marks a directory). "" for anything unusable."""
    s = str(p or "").strip().strip("\"'`").replace("\\", "/")
    if not s or "\n" in s or len(s) > 400:
        return ""
    is_dir = s.endswith("/")
    rooted = s.startswith("/") or bool(re.match(r"^[A-Za-z]:/", s)) or s.startswith("~")
    if base and not rooted:
        s = str(base).replace("\\", "/").rstrip("/") + "/" + s
    parts = []
    for seg in s.split("/"):
        if seg in ("", "."):
            continue
        if seg == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(seg)
    if not parts:
        return ""
    out = "/".join(parts).lower()
    if s.startswith("/") and not re.match(r"^[a-z]:", out):
        out = "/" + out
    return out + ("/" if is_dir else "")


def _clean_token(tok):
    return str(tok).strip().rstrip(".,;:)]}>、。").strip()


def declared_scope(step_text):
    """The path / file / directory tokens a child's slice step names, normalised, in order of
    appearance without repeats. Conservative: only explicit paths or file names. Nothing named
    -> () which means "no declaration" (neither success nor violation). Never raises."""
    try:
        text = _URL.sub(" ", str(step_text or ""))
        found = []

        def add(tok):
            tok = _clean_token(tok)
            if not tok or re.fullmatch(r"[\\/.]*", tok):
                return
            n = norm_path(tok)
            if n and n not in found:
                found.append(n)

        for m in _QUOTED.finditer(text):
            q = next((g for g in m.groups() if g), "").strip()
            # A quoted token counts only when it looks like a path: a separator or an extension.
            if ("/" in q or "\\" in q or _HAS_EXT.search(q)) and not q.endswith(" "):
                if len(q.split()) <= 6 or re.match(r"^[A-Za-z]:[\\/]", q):
                    add(q)
        unquoted = _QUOTED.sub(" ", text)
        for m in _SLASHED.finditer(unquoted):
            tok = _clean_token(m.group(1))
            rooted = bool(re.match(r"^(?:[A-Za-z]:|\.{1,2}|~)?[\\/]", tok)) or tok[:2] in ("./", ".\\")
            if rooted or _HAS_EXT.search(tok) or tok.endswith(("/", "\\")):
                add(tok)
        for m in _BARE_FILE.finditer(unquoted):
            add(m.group(1))
        return tuple(found)
    except Exception:
        return ()


def _components(n):
    return [c for c in n.split("/") if c]


def _is_dir(n):
    return n.endswith("/")


def path_matches(a, b):
    """Whether two normalised paths name the same file, or one is a directory holding the
    other. A relative path matches an absolute one that ends with it at a component boundary
    (a step says `relay/x.py`, a call says `C:/repo/relay/x.py`)."""
    if not a or not b:
        return False
    ca, cb = _components(a), _components(b)
    if not ca or not cb:
        return False
    if _is_dir(a) and not _is_dir(b):
        return _contains(cb, ca)
    if _is_dir(b) and not _is_dir(a):
        return _contains(ca, cb)
    if _is_dir(a) and _is_dir(b):
        return _contains(ca, cb) or _contains(cb, ca)
    short, long_ = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    return long_[len(long_) - len(short):] == short


def _contains(components, directory):
    """`directory` components appear as a contiguous run inside `components` (a path)."""
    n = len(directory)
    if n == 0 or n >= len(components):
        return False
    for i in range(0, len(components) - n):
        if components[i:i + n] == directory:
            return True
    return False


# ------------------------------------------------------------------ reading the ledger rows

def _arg_text(args, name):
    v = args.get(name) if isinstance(args, dict) else None
    if isinstance(v, dict):
        v = v.get("text")
    return v if isinstance(v, str) else ""


_PATH_FIELD = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')


def written_paths(call):
    """The normalised paths one write-class call_row names; () when none can be read.

    The ledger keeps a bounded copy of each argument, so a long edit list may be cut off: the
    paths that survive are real, the ones cut off are simply not seen (counted by nobody, never
    invented)."""
    try:
        tool = str(call.get("tool") or "")
        args = call.get("args") or {}
        base = _arg_text(args, "repo")
        base = "" if base in ("", ".") else base
        out = []
        if tool in ("write_file", "append_file", "replace_in_file", "multi_edit"):
            p = norm_path(_arg_text(args, "path"))
            if p:
                out.append(p)
        elif tool == "edit_and_verify":
            for m in _PATH_FIELD.finditer(_arg_text(args, "edits")):
                try:
                    raw = json.loads('"%s"' % m.group(1))
                except ValueError:
                    continue
                p = norm_path(raw, base=base)
                if p and p not in out:
                    out.append(p)
        return tuple(out)
    except Exception:
        return ()


class Roster(object):
    """Maps a ledger row to (campaign_id, child_id) using status.workers-shaped rows."""

    def __init__(self, workers):
        self.by_key = {}           # jid / task_id -> (campaign, child)
        self.by_name = {}          # worker name -> set of (campaign, child)
        self.goals = {}            # (campaign, child) -> goal text
        self.outcomes = {}         # (campaign, child) -> outcome
        for w in workers or []:
            if not isinstance(w, dict):
                continue
            cid = str(w.get("campaign_id") or "")
            if not cid or str(w.get("role") or "").lower() != "subtask":
                continue
            child = str(w.get("task_id") or w.get("jid") or w.get("name") or "")
            if not child:
                continue
            ident = (cid, child)
            for k in (w.get("jid"), w.get("task_id")):
                if k:
                    self.by_key[str(k)] = ident
            if w.get("name"):
                self.by_name.setdefault(str(w["name"]), set()).add(ident)
            self.goals[ident] = str(w.get("goal") or "")
            self.outcomes[ident] = str(w.get("outcome") or "")

    def resolve(self, row):
        """(campaign, child) or None. Campaign/task fields on the row win when present (the
        job-to-campaign identity); otherwise the roster maps the task / worker."""
        cid, tid = row.get("campaign_id"), row.get("task_id")
        if cid and tid:
            return (str(cid), str(tid))
        task = str(row.get("task") or "")
        if task and task in self.by_key:
            return self.by_key[task]
        name = str(row.get("worker") or "")
        hit = self.by_name.get(name)
        if hit and len(hit) == 1:
            return next(iter(hit))
        return None


def writes_from_events(rows, roster):
    """([write dict], stats) from tool_events rows (call rows only are read).

    A write dict is {campaign, child, path, tool, ts}. stats counts what was NOT used:
    unknown_attribution (untrusted or unresolvable rows) and unknown_write (shell / python
    calls, which may write anything)."""
    stats = {"calls": 0, "writes": 0, "unknown_attribution": 0, "unknown_write": 0}
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("event") != "call":
            continue
        tool = str(r.get("tool") or "")
        if tool not in WRITE_TOOLS and tool not in UNKNOWN_WRITE_TOOLS:
            continue
        stats["calls"] += 1
        ident = None
        if r.get("attr") in ATTRIBUTED_KINDS or (r.get("campaign_id") and r.get("task_id")):
            ident = roster.resolve(r)
        if ident is None:
            stats["unknown_attribution"] += 1
            continue
        if tool in UNKNOWN_WRITE_TOOLS:
            stats["unknown_write"] += 1
            continue
        for p in written_paths(r):
            stats["writes"] += 1
            out.append({"campaign": ident[0], "child": ident[1], "path": p, "tool": tool,
                        "ts": r.get("ts")})
    return out, stats


def overlaps(declarations, write_calls):
    """Overlaps among siblings of one campaign. Pure.

    declarations: {(campaign, child): iterable of normalised tokens} (empty = no declaration).
    write_calls: dicts with campaign, child, path (normalised), tool.

    Two kinds, each reported once per (campaign, kind, writer, other, path):
      declared_by_other  sibling A wrote a path that sibling B's step names
      written_by_two     siblings A and B both wrote the same path
    A write by A to a path A alone declares is the plan working, so it is not an overlap."""
    found = {}
    decl = {k: tuple(v) for k, v in (declarations or {}).items()}
    for w in write_calls or []:
        a = (w.get("campaign"), w.get("child"))
        p = w.get("path") or ""
        if not p or not a[0]:
            continue
        for other, toks in decl.items():
            if other[0] != a[0] or other == a:
                continue
            if any(path_matches(t, p) for t in toks):
                key = (a[0], "declared_by_other", a[1], other[1], p)
                found.setdefault(key, {"campaign": a[0], "kind": "declared_by_other",
                                       "writer": a[1], "other": other[1], "path": p,
                                       "tool": w.get("tool", "")})
    wrote = {}
    for w in write_calls or []:
        a = (w.get("campaign"), w.get("child"))
        if w.get("path") and a[0]:
            wrote.setdefault(a, []).append(w)
    keys = sorted(wrote, key=lambda k: (str(k[0]), str(k[1])))
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if a[0] != b[0]:
                continue
            for wa in wrote[a]:
                for wb in wrote[b]:
                    if path_matches(wa["path"], wb["path"]):
                        key = (a[0], "written_by_two", a[1], b[1], wa["path"])
                        found.setdefault(key, {"campaign": a[0], "kind": "written_by_two",
                                               "writer": a[1], "other": b[1],
                                               "path": wa["path"], "tool": wa.get("tool", "")})
    return list(found.values())


# ------------------------------------------------------------------ the setting

_settings_cache = {}


def mode(settings_path=None):
    """`fanout_write_scope=` from settings.txt: "shadow" or "off". Anything else, including a
    value that would mean enforcement, reads as off. Re-read when the file changes (each_gate).
    Never raises."""
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


# ------------------------------------------------------------------ the shadow recorder

_STATE = {"overlaps_seen": 0, "finished": set(), "recorded": set(), "unknown": 0}


def _read_tail_rows(path, tail_bytes=TAIL_BYTES):
    rows = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            data = fh.read()
        lines = data.decode("utf-8", "replace").splitlines()
        if size > tail_bytes and lines:
            lines = lines[1:]          # the first line is cut in the middle
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    except OSError:
        pass
    return rows


def worker_rows(workers):
    """status.workers-shaped dicts from live Worker objects (only the fields used here)."""
    out = []
    for w in workers or []:
        env = getattr(w, "task_envelope", None)
        out.append({"name": getattr(w, "name", ""), "goal": getattr(w, "goal", "") or "",
                    "outcome": getattr(w, "outcome", "") or "",
                    "jid": getattr(w, "jid", "") or "",
                    "campaign_id": getattr(env, "campaign_id", "") or "",
                    "task_id": getattr(env, "task_id", "") or "",
                    "role": getattr(env, "role", "") or ""})
    return out


def _own_step(goal):
    try:
        from relay import fanout
        return fanout._own_scope_step(goal)
    except Exception:
        return ""


def shadow_tick(workers, events_path=None, settings_path=None, record=None):
    """Called once per coordinator sweep. In `shadow`, when a child of a campaign has newly
    finished, scans that campaign's recent tool_events and records one `scope_overlap`
    mechanism row per NEW overlap. In `off` it returns at once: no ledger is read and nothing
    is recorded. Never raises, never blocks, never touches a prompt or an output."""
    try:
        if mode(settings_path) != "shadow":
            return 0
        rows = worker_rows(workers)
        roster = Roster(rows)
        newly = set()
        for ident, outcome in roster.outcomes.items():
            if outcome and ident not in _STATE["finished"]:
                _STATE["finished"].add(ident)
                newly.add(ident[0])
        if not newly:
            return 0
        if events_path is None:
            from tools import tool_ledger
            events_path = tool_ledger.LEDGER_PATH
        events = _read_tail_rows(events_path)
        writes, stats = writes_from_events(events, roster)
        _STATE["unknown"] = stats["unknown_attribution"]
        decl = {ident: declared_scope(_own_step(goal)) for ident, goal in roster.goals.items()}
        if record is None:
            from relay import mechanism_telemetry
            record = mechanism_telemetry.record
        n = 0
        for o in overlaps(decl, [w for w in writes if w["campaign"] in newly]):
            key = (o["campaign"], o["kind"], o["writer"], o["other"], o["path"])
            if key in _STATE["recorded"]:
                continue
            _STATE["recorded"].add(key)
            _STATE["overlaps_seen"] += 1
            n += 1
            record("scope_overlap", instance=o["writer"], configured=True, config_source="settings",
                   config_value="shadow", eligible=True, triggered=True, executed=True,
                   extra={"campaign": o["campaign"], "kind": o["kind"], "writer": o["writer"],
                          "other": o["other"], "path": o["path"], "tool": o["tool"],
                          "unknown_attribution": stats["unknown_attribution"],
                          "unknown_write": stats["unknown_write"], "record_only": True})
        return n
    except Exception:
        return 0


def status_block(settings_path=None):
    """{"fanout_write_scope": {mode, overlaps_seen}} for status.json (additive)."""
    return {KEY: {"mode": mode(settings_path), "overlaps_seen": int(_STATE["overlaps_seen"])}}


def _reset_for_test():
    _STATE.update({"overlaps_seen": 0, "unknown": 0})
    _STATE["finished"].clear()
    _STATE["recorded"].clear()
    _settings_cache.clear()
