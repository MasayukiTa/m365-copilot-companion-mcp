"""project_memory.py -- what this agent has actually DONE, kept per THEME, read back later.

Not a file the operator writes. Every finished task records a compact note by itself, and
the next task on the same theme gets those notes primed into its goal, so the agent
accumulates working knowledge without anyone maintaining it.

SHAPE (follows where Claude Code and Codex both landed, deliberately):

    .fleet/memory/INDEX.md          one line per theme -- the only thing read every time
    .fleet/memory/<slug>.md         one file per theme: frontmatter + newest-first entries

Claude Code writes its own learnings to ~/.claude/projects/<project>/memory/ as one fact
per file behind a MEMORY.md index it loads each session; Codex summarises prior sessions
into ~/.codex/memories/ and reads them on the next one. Both separate a always-read INDEX
from on-demand bodies, and both use markdown rather than an opaque blob so a human can read
and edit the store. This does the same.

WHY THEME AND NOT FOLDER: the first version keyed on the working folder. Two unrelated jobs
that both touched Desktop/test landed in one bucket and primed each other with irrelevant
history. A theme ("OGF 異物解析", "ブリッジの同意カード") is what the agent actually needs to
recall, and it survives the work moving between folders.

Frame-side only: plain files under .fleet, stdlib only, no MCP unlock, nothing executes.
Every function swallows its own errors -- memory is an enhancement, and a broken store must
never take down the run that was trying to record into it.
"""
from __future__ import annotations

import os
import re
import time

_MAX_PER_THEME = 20           # keep the most recent N entries per theme
_NOTE_CAP = 280               # chars per note snippet
_GOAL_CAP = 160
_INDEX_NAME = "INDEX.md"
# The index is the part that gets read on EVERY task, so it is capped the way Claude Code
# caps MEMORY.md: enough to see what exists, never enough to crowd out the actual work.
_INDEX_MAX_THEMES = 40
_SLUG_MAX = 48


def _mem_dir(state_dir):
    return os.path.join(state_dir or ".fleet", "memory")


def _index_path(state_dir):
    return os.path.join(_mem_dir(state_dir), _INDEX_NAME)


def slugify(theme):
    """A stable, filesystem-safe slug for a theme.

    Japanese themes are the normal case here, so this cannot be the usual ASCII-only
    slugifier -- that would collapse every Japanese theme to the empty string and put all
    of them in one file. Keep word characters (which include kana/kanji under re.UNICODE),
    fold everything else to a hyphen, and fall back to a hash only when nothing survives.
    """
    s = " ".join(str(theme or "").split()).lower()
    s = re.sub(r"[^\w]+", "-", s, flags=re.UNICODE).strip("-")
    s = s[:_SLUG_MAX].strip("-")
    if not s:
        s = "theme-%08x" % (abs(hash(str(theme or ""))) & 0xFFFFFFFF)
    return s


def theme_from_goal(goal, folder=""):
    """Best-effort theme when a caller has none.

    A caller that knows its theme should pass it. This exists so that wiring a new call
    site is never blocked on inventing a taxonomy: a goal's first clause is a decent
    theme, and a wrong-but-stable theme still beats no memory at all.
    """
    text = " ".join(str(goal or "").split())
    if not text:
        return os.path.basename(str(folder or "").rstrip("/\\")) or "general"
    # first clause: split on the punctuation people actually end a topic with
    head = re.split(r"[。．\.\n:：,、/／]", text, 1)[0]
    return (head or text)[:_GOAL_CAP]


def _looks_like_path(value):
    v = str(value or "")
    return bool(v) and (os.sep in v or "/" in v or (len(v) > 1 and v[1] == ":"))


def _resolve(key, goal="", folder=""):
    """(display theme, slug) for whatever the caller passed.

    A PATH stays folder-grouped. That is not just back-compat: deriving the theme from the
    goal instead would scatter every task on one repo into its own file, which loses both
    the isolation between folders and the point of accumulating anything. The slug carries
    a hash of the full path so two folders that happen to share a basename stay separate.
    """
    if _looks_like_path(key):
        full = os.path.abspath(str(key)).replace("\\", "/").rstrip("/").lower()
        base = os.path.basename(full) or "folder"
        return base, "folder-%s-%08x" % (slugify(base), abs(hash(full)) & 0xFFFFFFFF)
    theme = " ".join(str(key or "").split())[:_GOAL_CAP]
    if not theme:
        theme = theme_from_goal(goal, folder)
    return theme, slugify(theme)


def _theme_path(slug, state_dir):
    return os.path.join(_mem_dir(state_dir), slug + ".md")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def _write_atomic(path, text):
    """Write via a temp file in the same directory, then replace. A half-written memory
    file would be read back on the next task, so a crash mid-write must not be able to
    leave one."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")
        except Exception:
            pass
        return False


def _entry_lines(text):
    """The entry lines of a theme file, newest first, frontmatter excluded."""
    body = text.split("---", 2)[-1] if text.startswith("---") else text
    return [ln for ln in body.splitlines() if ln.startswith("- [")]


def _fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


def record_task(theme, goal, outcome, note="", state_dir=".fleet", ts=None, folder=""):
    """Record one finished piece of work under `theme`. Never raises; returns bool.

    `theme` may be a path for back-compat with the folder-keyed call sites -- a caller
    that passes one gets a theme derived from the goal instead, so old wiring keeps
    working and starts producing theme-shaped memory without being touched.
    """
    try:
        theme, slug = _resolve(theme, goal, folder)
        when = time.time() if ts is None else ts
        line = "- [%s] %s — %s%s" % (
            (outcome or "?").strip() or "?",
            " ".join(str(goal or "").split())[:_GOAL_CAP],
            " ".join(str(note or "").split())[:_NOTE_CAP] or "(記録なし)",
            "  <!-- %s -->" % _fmt_ts(when),
        )
        path = _theme_path(slug, state_dir)
        old = _entry_lines(_read(path))
        entries = ([line] + old)[:_MAX_PER_THEME]          # newest first
        text = (
            "---\n"
            "theme: %s\n"
            "updated: %s\n"
            "entries: %d\n"
            "---\n\n"
            "# %s\n\n"
            "%s\n"
        ) % (theme, _fmt_ts(when), len(entries), theme, "\n".join(entries))
        if not _write_atomic(path, text):
            return False
        _upsert_index(theme, slug, len(entries), when, state_dir)
        return True
    except Exception:
        return False


def _upsert_index(theme, slug, count, when, state_dir):
    """Keep INDEX.md as one line per theme, most recently touched first."""
    try:
        keep = []
        for ln in _read(_index_path(state_dir)).splitlines():
            if ln.startswith("- [") and ("(%s.md)" % slug) not in ln:
                keep.append(ln)
        line = "- [%s](%s.md) — %d件 / 最終 %s" % (theme, slug, count, _fmt_ts(when))
        lines = ([line] + keep)[:_INDEX_MAX_THEMES]
        text = ("# 実施済みの作業（テーマ別）\n\n"
                "各テーマの詳細は同じディレクトリの .md を読むこと。\n\n"
                "%s\n") % "\n".join(lines)
        _write_atomic(_index_path(state_dir), text)
    except Exception:
        pass


def _default_max_items():
    """How many entries to prime, from the ACTIVE HARNESS rather than a constant.

    This is the point where a genome stops being a record and starts being a behaviour.
    An evolvable parameter that no running code reads produces A/B arms that are the same
    program, and every experiment over it measures noise.

    Falls back to 5 if the harness config is unavailable -- memory is an enhancement and
    must not be able to break a run by failing to resolve.
    """
    try:
        from relay.selfimprove.runtime_config import memory_max_items
        return memory_max_items()
    except Exception:
        return 5


def _memory_v1(entries, max_items):
    """The original: the most recent N, whatever they say."""
    return entries[:max_items]


def _memory_v2(entries, max_items):
    """Recent N after collapsing near-identical entries.

    A loop that repeats a task writes the same line many times, and v1 then spends the whole
    budget re-telling the agent one fact. Whether that is an improvement is a question for
    the benchmark, which is the point of having two.
    """
    seen, out = set(), []
    for line in entries:
        key = "".join(ch for ch in line.lower() if ch.isalnum())[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= max_items:
            break
    return out


#: The versioned implementations of the `memory` component. THIS is what makes component
#: evolution real rather than a label: an independent review found that none of the seven
#: declared components had a single production reader, so changing planner/v1 to planner/v2
#: moved the manifest hash and executed exactly the same code -- an A/B whose two arms were
#: the same program, reporting a p-value about noise. A component belongs in
#: EVOLVABLE_COMPONENTS only once it has a table like this one behind it.
MEMORY_VERSIONS = {"memory/v1": _memory_v1, "memory/v2": _memory_v2}


def _select_entries(entries, max_items):
    """Pick the entries to prime, using whichever memory version the active harness names."""
    try:
        from relay.selfimprove import runtime_config as _rc
        impl = MEMORY_VERSIONS.get(_rc.component("memory"), _memory_v1)
    except Exception:
        impl = _memory_v1
    return impl(entries, max_items)


def load_notes(theme, max_items=None, state_dir=".fleet", goal="", include_index=True):
    """Text block to prime into a goal: this theme's recent entries, plus the index of
    what else is remembered. Returns "" when there is nothing. Never raises.

    Two layers on purpose. The theme's own entries are what the agent needs right now;
    the index is how it discovers that a NEIGHBOURING theme exists and is worth reading.
    Without the index every theme is an island and the store stops compounding.
    """
    try:
        if max_items is None:
            max_items = _default_max_items()
        theme, slug = _resolve(theme, goal)
        entries = _select_entries(_entry_lines(_read(_theme_path(slug, state_dir))),
                                  max_items)
        index = _read(_index_path(state_dir)).strip() if include_index else ""
        blocks = []
        if entries:
            blocks.append("このテーマ「%s」での過去の作業（新しい順）:\n%s"
                          % (theme, "\n".join(entries)))
        if index:
            other = [ln for ln in index.splitlines()
                     if ln.startswith("- [") and ("(%s.md)" % slug) not in ln]
            if other:
                blocks.append("記憶している他のテーマ（必要なら .fleet/memory/ を読む）:\n"
                              + "\n".join(other[:_INDEX_MAX_THEMES]))
        return "\n\n".join(blocks)
    except Exception:
        return ""


def list_themes(state_dir=".fleet"):
    """Every remembered theme as (theme, slug, path). For the cockpit and for tests."""
    out = []
    try:
        d = _mem_dir(state_dir)
        for name in sorted(os.listdir(d)):
            if not name.endswith(".md") or name == _INDEX_NAME:
                continue
            text = _read(os.path.join(d, name))
            m = re.search(r"^theme:\s*(.+)$", text, re.M)
            out.append(((m.group(1).strip() if m else name[:-3]), name[:-3],
                        os.path.join(d, name)))
    except Exception:
        pass
    return out
