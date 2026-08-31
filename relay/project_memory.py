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

from relay import provenance as P

_MAX_PER_THEME = 20           # keep the most recent N entries per theme
_NOTE_CAP = 280               # chars per note snippet
_GOAL_CAP = 160
_INDEX_NAME = "INDEX.md"
# The index is the part that gets read on EVERY task, so it is capped the way Claude Code
# caps MEMORY.md: enough to see what exists, never enough to crowd out the actual work.
_INDEX_MAX_THEMES = 40
_SLUG_MAX = 48


#: An explicit state root, for a run that must not share memory with anything else. The
#: fleet A/B adapter seeds one per arm: fleet memory is read AND written every run, so two
#: arms sharing a store turn a paired comparison into a sequence where the baseline teaches
#: the candidate. The adapter set this and nothing read it, so the isolation it advertised
#: did not exist -- an independent review found the seeded directory sitting unused.
STATE_DIR_ENV = "FLEET_STATE_DIR"


def _mem_dir(state_dir):
    root = state_dir or os.environ.get(STATE_DIR_ENV, "").strip() or ".fleet"
    return os.path.join(root, "memory")


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
    if not s:
        return "theme-%08x" % (abs(hash(str(theme or ""))) & 0xFFFFFFFF)
    if len(s) <= _SLUG_MAX:
        return s
    # TRUNCATION MERGES THEMES THAT SHARE AN OPENING, and this store had a live case of it.
    # Every SWE-bench goal begins "You are fixing a real bug in the open-source project
    # **<repo>...", and the repo name falls past character 48 -- so ansible and NodeBB
    # produced DIFFERENT theme titles and the SAME slug, and shared one file. A NodeBB worker
    # was primed with ansible history and the other way round; the ansible theme file on disk
    # holds NodeBB entries, which is how this was noticed.
    #
    # The repository has already paid for this exact mistake once, one directory over:
    # bench/pro_stage_goals.py's wt_for() carries six hex of the instance id for the same
    # reason, after a worker's file reads were routed into the wrong container.
    #
    # Six hex of the FULL theme, appended after truncation. Themes at or under the cap are
    # untouched, so the ordinary case keeps its readable filename and existing files keep
    # working. The long themes get new files; the old merged one is left on disk rather than
    # deleted -- it is the user's data, and nothing needs it gone to be correct.
    import hashlib
    tag = hashlib.sha256(s.encode("utf-8")).hexdigest()[:6]
    return s[:_SLUG_MAX - 7].strip("-") + "-" + tag


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


#: The timestamp comment, which is the only part of two otherwise identical entries that
#: differs. NOT ANCHORED TO END OF LINE: the first version was, and the repeat marker this
#: function appends then sat after it, so a collapsed line's timestamp stopped being stripped
#: and the line stopped matching its own successors. Eight identical records collapsed
#: pairwise to four instead of to one -- a dedupe that half worked, which is the shape that
#: gets believed.
_ENTRY_TS = re.compile(r"\s*<!--\s*\d{4}-\d{2}-\d{2}[^>]*-->")

#: A repeat marker, so collapsing does not throw away the fact that the work recurred.
_REPEAT = re.compile(r"\s*<!--\s*x(\d+)\s*-->")


def _entry_key(line):
    """What makes two entries THE SAME entry: everything except when it happened."""
    return " ".join(_REPEAT.sub("", _ENTRY_TS.sub("", line or "")).split())


def _repeat_count(line):
    m = _REPEAT.search(line or "")
    return int(m.group(1)) if m else 1


def dedupe_entries(entries):
    """Collapse entries that record the same thing, keeping the newest and counting the rest.

    WHY THIS IS NOT HOUSEKEEPING. Measured 2026-08-31, in a store of 156 theme files: one
    file held EIGHT lines, all of them

        - [DONE] 2の12乗はいくつか、数字だけ答えて — refuter#1: UPHELD

    differing only in their timestamp comment. The theme's entries are primed into every
    later goal on that theme, and the index of all themes is primed into every goal at all --
    measured at 4,047 characters against a 1,401-character protocol. Nearly three times the
    instructions, carrying one fact stated eight times.

    Same shape as the volatile-field defect in relay/relay_fleet.py's no-progress key, and
    the same fix: compare on what identifies the entry, not on the bytes. A per-line
    timestamp is exactly the field that makes "the same thing again" look new.

    THE COUNT IS KEPT. "This happened eight times" is information -- it is the repetition
    that was not. Collapsing to one line with x8 says both in one line's worth of budget.
    """
    out, seen = [], {}
    for line in entries or []:
        key = _entry_key(line)
        if not key:
            continue
        if key in seen:
            seen[key][0] += _repeat_count(line)
            continue
        seen[key] = [_repeat_count(line), len(out)]
        out.append(line)
    for key, (count, idx) in seen.items():
        if count > 1:
            base = _REPEAT.sub("", out[idx])
            out[idx] = base + "  <!-- x%d -->" % count
    return out


def _tokens(text):
    """Words worth matching on, in a store that holds both Japanese and English themes.

    Latin runs of 3+ characters, plus CJK bigrams -- Japanese does not space its words, so
    whitespace splitting produces one enormous token per phrase and matches nothing.
    """
    low = (text or "").lower()
    out = set(re.findall(r"[a-z0-9_./-]{3,}", low))
    cjk = re.findall(r"[぀-ヿ一-鿿]{2,}", low)
    for run in cjk:
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def rank_index_lines(lines, theme="", goal=""):
    """Order the index by how likely each theme is to be worth opening for THIS work.

    WHY NOT RECENCY. The index is primed into EVERY goal -- measured 2026-08-31 at 4,047
    characters against a 1,401-character protocol, so nearly three times the instructions.
    What it contained was 40 of the store's 156 themes in date order, and because a theme is
    keyed on the goal's opening words, most of them were one-shot questions: "2の12乗はいくつか",
    "625の平方根はいくつか". A worker fixing a bug in ansible was handed those and nothing about
    ansible.

    Recency answers "what happened lately", which is not the question the index exists for.
    Its stated purpose is that a worker DISCOVERS a neighbouring theme, and neighbouring means
    related to the work in hand.

    Ties keep the file's existing order, which is recency -- so with nothing to go on the
    behaviour is what it was, and this can only reorder, never invent or drop. The cap is
    applied by the caller, unchanged.
    """
    try:
        want = _tokens(theme) | _tokens(goal)
        if not want:
            return list(lines)
        scored = []
        for i, ln in enumerate(lines):
            title = ln.split("](", 1)[0][3:] if "](" in ln else ln
            overlap = len(want & _tokens(title))
            scored.append((-overlap, i, ln))
        scored.sort()
        return [ln for _s, _i, ln in scored]
    except Exception:
        return list(lines)


#: How many UNRELATED themes to keep, always. The index's stated job is DISCOVERY, and a
#: filter that shows only what already looks related can never surface anything new -- so a
#: short recency tail stays whatever happens. Five lines is about 340 characters; forty was
#: 2,718.
_INDEX_RECENT_TAIL = 5


def prune_index_lines(lines, theme="", goal=""):
    """Drop index entries that share nothing with the work in hand -- when any of them do.

    MEASURED against the live store, for a worker fixing a bug in ansible: ONE of the forty
    index lines shared a single token with its goal. The other thirty-nine were one-shot
    questions the theme key had turned into themes -- "2の12乗はいくつか", "625の平方根はいくつか" --
    and all of them were primed into that worker's first turn, 2,718 characters of them.

    EVERY related theme is kept; the unrelated ones are cut to a short recency tail. The rule
    is uniform on purpose. The first version kept everything when NOTHING matched -- meant as
    caution, and it left the measured case untouched: load_notes filters the CURRENT theme's
    own line out of the index before this is called, so the one line that matched was already
    gone, `related` was empty, and all thirty-nine unrelated lines came back. A conservative
    branch that exempts exactly the case being fixed is not caution.

    The tail is what keeps discovery possible: the index exists so a worker finds a
    NEIGHBOURING theme, and a filter showing only what already looks related can never surface
    anything it did not know to look for.

    Token overlap is a weak signal and will sometimes drop a theme that mattered. Weighed
    against thirty-nine arithmetic questions crowding a bug fix, that is the better error, and
    the theme file itself is still on disk for a worker that goes looking.
    """
    try:
        want = _tokens(theme) | _tokens(goal)
        if not want:
            return list(lines)
        related, rest = [], []
        for ln in lines:
            title = ln.split("](", 1)[0][3:] if "](" in ln else ln
            (related if (want & _tokens(title)) else rest).append(ln)
        return related + rest[:_INDEX_RECENT_TAIL]
    except Exception:
        return list(lines)


def entry_authority(line):
    """The authority recorded on a memory line, or EXTERNAL_UNTRUSTED if there is none.

    Entries written before provenance existed have no marker, and they are exactly the ones
    whose origin nobody can now establish -- so they read as untrusted. That is the safe
    direction and it is also the true one.
    """
    marker = "<!-- authority="
    if marker not in (line or ""):
        return P.EXTERNAL_UNTRUSTED
    return P.normalise(line.split(marker, 1)[1].split("-->", 1)[0])


def authorities_in(theme, state_dir=None, goal=""):
    """Every distinct authority present in a theme's entries. For the evolution loop."""
    try:
        _theme, slug = _resolve(theme, goal)
        return sorted({entry_authority(ln)
                       for ln in _entry_lines(_read(_theme_path(slug, state_dir)))})
    except Exception:
        return [P.EXTERNAL_UNTRUSTED]


def _fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


def record_task(theme, goal, outcome, note="", state_dir=None, ts=None, folder="",
                authority=None):
    """Record one finished piece of work under `theme`. Never raises; returns bool.

    `theme` may be a path for back-compat with the folder-keyed call sites -- a caller
    that passes one gets a theme derived from the goal instead, so old wiring keeps
    working and starts producing theme-shaped memory without being touched.

    `authority` is where the CONTENT of this entry came from. This store is the live
    contamination channel for lineage poisoning: whatever lands here is prepended to future
    goals by load_notes, so text an attacker put in a document can end up shaping tasks it
    never touched. Recording the origin does not stop that text being written -- notes about
    a document are legitimately about the document -- it keeps the mark attached, so the
    evolution loop can refuse to treat it as a REASON to change the harness.

    An omitted authority becomes EXTERNAL_UNTRUSTED, deliberately. Most callers here
    summarise a run that read external content, and "nobody recorded where this came from"
    must not read as "it is fine".
    """
    try:
        theme, slug = _resolve(theme, goal, folder)
        when = time.time() if ts is None else ts
        auth = P.normalise(authority)
        line = "- [%s] %s — %s%s%s" % (
            (outcome or "?").strip() or "?",
            " ".join(str(goal or "").split())[:_GOAL_CAP],
            " ".join(str(note or "").split())[:_NOTE_CAP] or "(記録なし)",
            "  <!-- authority=%s -->" % auth,
            "  <!-- %s -->" % _fmt_ts(when),
        )
        path = _theme_path(slug, state_dir)
        old = _entry_lines(_read(path))
        # DEDUPED BEFORE THE CAP, not after. Twenty slots filled with one fact repeated is a
        # theme that remembers nothing while looking full -- and the cap would then evict the
        # genuinely different entries first, because they are the older ones.
        entries = dedupe_entries([line] + old)[:_MAX_PER_THEME]      # newest first
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

    THE KEY USED TO INCLUDE THE TIMESTAMP, so this arm did not do the thing it is named for.
    It was `"".join(alnum(line))[:80]`, and the entries this store writes end in a comment
    like `<!-- 2026-08-28 08:49 -->` whose digits survive that filter. Measured 2026-08-31 on
    the store's real lines: eight identical records, eight distinct keys, nothing collapsed.
    The 80-character truncation hid it only for goals long enough to fill the budget before
    reaching the timestamp -- so the arm worked on long goals and silently did nothing on
    short ones, which is worse than not working at all, because the A/B still reported a
    difference sometimes.

    Same volatile-field defect as relay_fleet.py's no-progress key, found the same day. It
    now shares the one identity function, `_entry_key`.
    """
    seen, out = set(), []
    for line in entries:
        key = _entry_key(line)
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


def load_notes(theme, max_items=None, state_dir=None, goal="", include_index=True):
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
        # NO DEDUPE HERE, DELIBERATELY, and the first version of this line had one.
        #
        # Collapsing repeats at READ time is what `memory/v2` already does -- it is a declared
        # component with two arms, and which one primes better is a question the benchmark is
        # supposed to answer. Doing it unconditionally here made v1 and v2 return byte-identical
        # text, so the experiment measured nothing: exactly the failure MEMORY_VERSIONS was
        # introduced to end, where a manifest named a component whose arms ran the same code.
        # A test caught it, which is the only reason it is not still true.
        #
        # The write path still dedupes, and that is a different question. v2 filters what is
        # PRIMED; it cannot recover an entry the cap already evicted. Twenty slots holding one
        # fact discard the distinct entries first, because the cap keeps the newest -- so
        # whether a distinct entry SURVIVES is decided when it is written, not when it is read.
        entries = _select_entries(_entry_lines(_read(_theme_path(slug, state_dir))), max_items)
        index = _read(_index_path(state_dir)).strip() if include_index else ""
        blocks = []
        if entries:
            blocks.append("このテーマ「%s」での過去の作業（新しい順）:\n%s"
                          % (theme, "\n".join(entries)))
        if index:
            other = [ln for ln in index.splitlines()
                     if ln.startswith("- [") and ("(%s.md)" % slug) not in ln]
            other = prune_index_lines(rank_index_lines(other, theme, goal),
                                      theme, goal)[:_INDEX_MAX_THEMES]
            if other:
                blocks.append("記憶している他のテーマ（必要なら .fleet/memory/ を読む）:\n"
                              + "\n".join(other))
        return "\n\n".join(blocks)
    except Exception:
        return ""


def list_themes(state_dir=None):
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
