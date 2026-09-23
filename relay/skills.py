"""Claude-compatible Skill discovery, validation, trust, and rendering.

The important boundary is deliberate:

* trusting a Skill permits its instructions to be loaded;
* it never grants shell, file mutation, or outbound permissions;
* those side effects continue through the existing MCP unlock/contract gates.

Approval is keyed to a digest of the whole Skill directory.  Any change to
``SKILL.md`` or a bundled script/reference/asset invalidates prior approval.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from time import sleep as _sleep
# The racy-timestamp check compares file mtimes with the real clock; bound here so a test
# that swaps this module's `time` for a fake clock does not reach into it.
from time import time_ns as _time_ns
from typing import Any, Iterable

import yaml


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_FILES = 256
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024
# 人が席を外して戻ってくるまでの時間。10分にしていたときは、6分遅れで承認ボタンを
# 押した操作が黙って捨てられ、画面には承認できたように見えて実際には信頼されない、
# という一番たちの悪い状態になった。長くしても、承認はそのときのハッシュに対して
# しか効かず、確定の直前に取り直して突き合わせる（束が変わっていれば拒否される）。
APPROVAL_TTL_SECONDS = 24 * 60 * 60
SUPPORTED_DIRS = ("scripts", "references", "assets")


class SkillError(ValueError):
    """A safe, user-displayable Skill validation error."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    scope: str
    digest: str
    metadata: dict[str, Any]
    body: str
    files: tuple[str, ...]
    manifest: tuple[tuple[str, str], ...]
    total_bytes: int
    provenance: str = "external"
    trust: str = "untrusted"

    def public_metadata(self) -> dict[str, Any]:
        """Return progressive-disclosure metadata; never include the body."""
        return {
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
            "path": str(self.path),
            "digest": self.digest,
            "provenance": self.provenance,
            "trust": self.trust,
            "disable_model_invocation": bool(
                self.metadata.get("disable-model-invocation", False)
            ),
            "user_invocable": self.metadata.get("user-invocable", True) is not False,
            "files": list(self.files),
            "total_bytes": self.total_bytes,
        }


def default_state_db() -> Path:
    override = os.environ.get("MCP_SKILLS_STATE_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share"))
    return root / "m365-copilot-companion" / "skills.sqlite3"


def _declared_name(skill_md: Path) -> str:
    """Best-effort `name:` from a SKILL.md whose YAML does not parse.

    Used only to label an INVALID bundle, so a plain line scan is enough (and must
    never raise): the file is broken by definition -- that is why yaml.safe_load
    failed. Authors look a Skill up by the name they wrote in the file, which need
    not match the folder it sits in, so both keys are recorded.
    """
    try:
        head = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()[:12]
        for line in head:
            stripped = line.strip()
            if stripped.startswith("name:"):
                return stripped[len("name:"):].strip().strip("\"'")
    except Exception:
        pass
    return ""


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise SkillError("SKILL.md must start with YAML frontmatter delimited by ---")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL.md frontmatter opening delimiter is invalid")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise SkillError("SKILL.md frontmatter closing delimiter is missing")
    try:
        metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"invalid SKILL.md YAML: {exc}") from exc
    if not isinstance(metadata, dict):
        raise SkillError("SKILL.md frontmatter must be a mapping")
    return metadata, "\n".join(lines[end + 1 :]).strip()


def _safe_files(root: Path) -> tuple[list[tuple[str, bytes]], int]:
    root = root.resolve()
    if not root.is_dir():
        raise SkillError(f"Skill directory does not exist: {root}")
    rows: list[tuple[str, bytes]] = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix().lower()):
        if path.is_symlink():
            raise SkillError(f"symbolic links are not allowed in Skills: {path}")
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise SkillError(f"Skill file escapes its directory: {path}") from exc
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SkillError(f"Skill file exceeds {MAX_FILE_BYTES} bytes: {relative}")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise SkillError(f"Skill bundle exceeds {MAX_TOTAL_BYTES} bytes")
        rows.append((relative, data))
        if len(rows) > MAX_FILES:
            raise SkillError(f"Skill bundle exceeds {MAX_FILES} files")
    if not any(name == "SKILL.md" for name, _ in rows):
        raise SkillError("Skill directory has no SKILL.md")
    return rows, total


def load_bundle(path: str | Path, scope: str = "external") -> Skill:
    root = Path(path).expanduser().resolve()
    rows, total = _safe_files(root)
    raw = dict(rows)["SKILL.md"]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SkillError("SKILL.md must be UTF-8") from exc
    metadata, body = _split_frontmatter(text)
    name = str(metadata.get("name") or root.name).strip()
    description = str(metadata.get("description") or "").strip()
    if not NAME_RE.fullmatch(name):
        raise SkillError("Skill name must use lowercase letters, digits, and hyphens (max 64)")
    if root.name != name:
        raise SkillError(f"Skill directory name must match frontmatter name: {name}")
    if not description:
        raise SkillError("Skill description is required")
    if len(description) > 1024:
        raise SkillError("Skill description exceeds 1024 characters")
    if not body:
        raise SkillError("SKILL.md body is empty")
    digest = hashlib.sha256()
    for relative, data in rows:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return Skill(
        name=name,
        description=description,
        path=root,
        scope=scope,
        digest=digest.hexdigest(),
        metadata=metadata,
        body=body,
        files=tuple(relative for relative, _ in rows),
        manifest=tuple(
            (relative, hashlib.sha256(data).hexdigest()) for relative, data in rows
        ),
        total_bytes=total,
    )


# ------------------------------------------------------------------------ the bundle cache
#
# WHY IT EXISTS. Every tool call built a new SkillStore and ran discover() at least once --
# skill_match runs it three or four times -- and discover() re-read, re-resolved and re-hashed
# every file of every bundle and asked SQLite two questions per Skill on a fresh connection.
# Measured 2026-09-24 at 500 Skills (Windows, NTFS, antivirus on): ~17 s per call, about half
# in path resolution / stat / hashing and a third in those queries.
#
# WHAT IT IS KEYED ON. One entry per bundle directory, keyed by the directory's path under the
# RESOLVED root and validated by the bundle's listing: every entry below it as
# (relative path, mtime_ns, size), taken with os.scandir -- which on Windows reads those from
# the directory itself, without opening a file. An unchanged listing reuses the parsed Skill
# (or the recorded load error) and its digest; a changed one re-reads just that bundle through
# load_bundle(), the uncached reader, so what is cached is always exactly what that reader
# produced. Bundles that vanished from a root are dropped when that root is next scanned.
# Nothing that is a symlink, junction or other reparse point -- the bundle directory itself or
# anything inside it -- is ever cached: those are loaded uncached every time, which is also
# where load_bundle() refuses them.
#
# THE APPROVAL INVARIANT, AND HOW THE CACHE KEEPS IT. Trust is a fact about a digest, so a
# cached digest must never outlive the content it was computed from. A listing can fail to
# change when the content did in two ways, and each has its own guard:
#
# 1. RACY TIMESTAMPS (accidental). A same-size rewrite landing in the same timestamp tick as the
#    write the cache already saw leaves (mtime_ns, size) identical. This is git's "racily
#    clean" problem and gets git's answer: an entry vouches for its content only once every file
#    in it is older than the moment the listing was taken by more than _RACY_MARGIN_NS (2 s --
#    FAT's granularity, the coarsest in use). After that, any later write necessarily carries a
#    different mtime. Until then the bundle is simply re-read on every call.
#
# 2. DELIBERATE (mtime restored with os.utime / SetFileTime after an edit), or a writer that
#    still holds the file open (NTFS updates the directory's copy of size/mtime lazily, on
#    close). A listing cannot see either. So the listing is trusted only to LIST and MATCH;
#    everything that hands approved content to a caller or records a trust decision re-hashes
#    the actual bytes of that one bundle first -- get(strict=True) under render(),
#    read_resource(), request_approval() and confirm_approval(), the winner of match(), and
#    _sync_gate_approvals() (which never used the cache). A mismatch drops the entry and the
#    answer is recomputed from the real content. Cost: one bundle, not five hundred.
#    The residual exposure is metadata only -- a forged-mtime edit to a trusted bundle's
#    description could show in skill_list until anything loads it -- and forging an mtime needs
#    the same write access as forging skills.sqlite3's trust row directly, which this store has
#    never defended against.

#: How much older than the listing every file must be before an entry vouches for content.
_RACY_MARGIN_NS = 2_000_000_000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
#: Roots remembered at once. A long pytest session scans hundreds of tmp roots exactly once
#: each; a server scans its two or four forever.
_CACHE_MAX_ROOTS = 32


@dataclass(frozen=True)
class _CacheEntry:
    signature: tuple
    skill: Skill | None          # scope-free: the scope is re-applied on every read
    error: str | None
    declared: str
    key: str                     # SkillStore._key(skill.path), computed once


_CACHE: "OrderedDict[str, dict[str, _CacheEntry]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _listing(bundle: str) -> tuple[tuple, int, set[str]] | None:
    """(signature, newest file mtime_ns, top-level names) of a bundle, or None if uncacheable.

    Directories appear by name only: their own mtimes change for reasons that do not touch the
    digest, and a file added or removed below one already changes the file rows.
    """
    rows: list[tuple[str, int, int]] = []
    newest = 0
    top: set[str] = set()
    stack = [("", bundle)]
    while stack:
        prefix, path = stack.pop()
        with os.scandir(path) as it:
            for entry in it:
                if not prefix:
                    top.add(entry.name)
                if _is_link(entry):
                    return None
                rel = prefix + entry.name
                if entry.is_dir(follow_symlinks=False):
                    rows.append((rel + "/", -1, -1))
                    stack.append((rel + "/", entry.path))
                    continue
                st = entry.stat(follow_symlinks=False)
                rows.append((rel, st.st_mtime_ns, st.st_size))
                newest = max(newest, st.st_mtime_ns)
    rows.sort()
    return tuple(rows), newest, top


def _forget_bundle(path: Path) -> None:
    """Drop the cache entry for one bundle directory (resolved path), wherever it is held."""
    key = os.path.normcase(str(path))
    with _CACHE_LOCK:
        for entries in _CACHE.values():
            entries.pop(key, None)


def clear_bundle_cache() -> None:
    """Forget every cached bundle (tests, and anyone who distrusts the listing)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `path` by rename from a temporary file NO OTHER WRITER CAN BE USING.

    Every writer of one approval question used the same temporary name, <token>.json.tmp, and
    os.replace()d it. On Windows a rename fails while another writer still has that file open,
    so two request_approval() calls for one Skill at the same moment crashed with WinError 32
    instead of sharing the question. mkstemp gives each writer its own name in the same
    directory (same volume, so the rename stays atomic).

    The bounded retry is for the DESTINATION, a different and inherent Windows condition: a
    reader that has the gate file open (the cockpit, or _sync_gate_approvals reading it) blocks
    a rename onto it for as long as its read lasts. That hold ends by itself in milliseconds,
    every time, so waiting for it is the handling rather than a mask; past ~1 s it is not that
    and the error is raised.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                _sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#: The argument placeholders render() substitutes. See render() for the grammar in words.
_PLACEHOLDER = re.compile(
    r"\$ARGUMENTS\[([0-9]+)\]"                  # $ARGUMENTS[N]
    r"|\$ARGUMENTS(?![A-Za-z0-9_])"             # $ARGUMENTS
    r"|\$([0-9])(?![0-9]|[.,][0-9])"            # $N, one digit, not the start of a number
)


def substitute_arguments(body: str, arguments: str = "") -> str:
    """Fill a Skill body's argument placeholders, in ONE pass. The grammar:

    * ``$ARGUMENTS`` -- the whole argument string as typed. Not a placeholder when followed by
      an ASCII letter, digit or ``_`` (``$ARGUMENTS_X`` is left alone); any other character,
      Japanese included, may follow it.
    * ``$ARGUMENTS[N]`` -- the Nth argument, N a non-negative decimal integer.
    * ``$N`` -- shorthand for ``$ARGUMENTS[N]``, where N is exactly ONE digit 0-9 that is not
      followed by another digit, nor by ``.`` or ``,`` and then a digit.
    * Arguments are the string split on whitespace, counted from 0; the full-width space an IME
      types separates them too. A missing argument is the empty string.
    * Everything else is literal. So ``$100``, ``$5,000`` and ``$1.50`` stay as written -- a
      ``$`` followed by a number is money, not a placeholder -- while ``$1`` followed by a
      space, a letter or the end of a sentence is a placeholder. A body that must print a
      one-digit dollar amount writes it as ``$5.00`` or ``USD 5``.

    ONE PASS, and this is half of the fix. The old code replaced ``$ARGUMENTS`` and then each
    of ``$0``..``$9`` over the RESULT, so text the user typed was scanned again as template:
    arguments ``ACME $0`` rendered as ``ACME ACME``, and ``Refunds up to $100`` with arguments
    ``Mr. Smith`` became ``Refunds up to Smith00``. Here every placeholder is found in the
    body as written and replaced once; substituted text is never re-read.
    """
    parts = (arguments or "").split()

    def fill(m: re.Match) -> str:
        index = m.group(1) if m.group(1) is not None else m.group(2)
        if index is None:
            return arguments or ""
        n = int(index)
        return parts[n] if n < len(parts) else ""

    return _PLACEHOLDER.sub(fill, body)


class SkillStore:
    """SQLite trust store and Skill registry for one project/user."""

    def __init__(self, project_root: str | Path, db_path: str | Path | None = None,
                 gate_dir: str | Path | None = None, use_cache: bool = True):
        # use_cache=False is the reference reader: every bundle re-read and re-hashed and every
        # trust state queried one Skill at a time, exactly as before the cache existed. The
        # cached path must give identical answers (tests/test_skills_business_robustness.py
        # compares them); see the comment above _CacheEntry for how it earns that.
        self.use_cache = use_cache
        self.project_root = Path(project_root).expanduser().resolve()
        self.db_path = Path(db_path).expanduser().resolve() if db_path else default_state_db()
        self.gate_dir = Path(gate_dir).expanduser().resolve() if gate_dir else self._default_gate_dir()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _default_gate_dir(self) -> Path:
        """Where approval questions are written.

        THE ONE OF THE THREE WITH NO ESCAPE HATCH, AND IT LEAKED FOR MONTHS. db_path and
        project_root can both be pointed elsewhere (MCP_SKILLS_STATE_DB, MCP_SKILLS_PROJECT_ROOT)
        and the tests do point them at a tmp_path. This one always resolved to the real
        ALLOWED_BASE, which is the operator's home directory, so any test reaching the
        "ask about a near-miss skill" path wrote a REAL approval question into the REAL queue --
        and relay_fleet._with_matched_skill constructs SkillStore(root) with no gate_dir, so the
        tests had no way to isolate it even knowing.

        Measured 2026-09-07: 378 pending questions in ~/.companion_gates, every single one
        naming a pytest temp directory, none naming a skill that exists, 202 already past their
        24h TTL. pytest's tmp_path is freshly numbered per run, so nothing ever deduplicated --
        each run added more. The owner was looking at a backlog of ~370 decisions of which
        exactly zero were real, while SkillStore.unapproved() on the actual project returned [].
        The count was still climbing during the investigation, from a test run in flight.

        MCP_SKILLS_GATE_DIR mirrors MCP_SKILLS_STATE_DB so the isolation a test already asks for
        actually covers the third thing this object writes.
        """
        override = os.environ.get("MCP_SKILLS_GATE_DIR", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        try:
            from tools.file_ops import ALLOWED_BASE
            return (ALLOWED_BASE / ".companion_gates").resolve()
        except Exception:
            return (self.project_root / ".companion_gates").resolve()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS skill_sources (
                    source_path TEXT PRIMARY KEY,
                    provenance TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS skill_trust (
                    source_path TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    approved_at REAL NOT NULL,
                    approval_kind TEXT NOT NULL,
                    manifest_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS approval_challenges (
                    token_hash TEXT PRIMARY KEY,
                    gate_token TEXT,
                    source_path TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(skill_trust)").fetchall()
            }
            if "manifest_json" not in columns:
                db.execute(
                    "ALTER TABLE skill_trust ADD COLUMN manifest_json TEXT NOT NULL DEFAULT '{}'"
                )
            challenge_columns = {
                row[1] for row in db.execute("PRAGMA table_info(approval_challenges)").fetchall()
            }
            if "gate_token" not in challenge_columns:
                db.execute("ALTER TABLE approval_challenges ADD COLUMN gate_token TEXT")

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(str(path.resolve()))

    def roots(self) -> list[tuple[str, Path]]:
        # Native, product-neutral `skills/` locations let people use Skills without
        # installing Claude. `.claude/skills/` remains a compatibility source so an
        # existing Claude Skill library works unchanged. Later entries win on a
        # same-name collision; native folders therefore override compatibility ones.
        #
        # THE PERSONAL SCOPE IS OFF BY DEFAULT, and this is a correction, not a default.
        #
        # `~/.claude/skills` is the library of whatever assistant the OPERATOR runs on this
        # machine. It is not this server's, and this server hands its Skills to agents that
        # have nothing in common with that assistant. Measured 2026-08-31, on a real
        # SWE-bench goal:
        #
        #     skill_match("You are fixing a real bug in ... **ansible/ansible** ...")
        #         -> delegation-commander   score 1.0    (personal, ~/.claude/skills)
        #
        # delegation-commander is a Claude Code playbook whose description says to use it for
        # ALL coding tasks. Handed to a fleet worker it prescribes dispatching the work to
        # subagents that worker does not have -- and it won at 1.0, ahead of every project
        # Skill, because personal entries come last and last wins. Every fleet worker
        # following the server's own "call skill_match before domain work" rule on an English
        # coding goal was being given it.
        #
        # This is a leak in the direction that matters: the personal library is TRUSTED there
        # (its owner approved it for a different product), so the trust check cannot catch it.
        # An operator who does want one library for both sets MCP_SKILLS_INCLUDE_PERSONAL=1,
        # which is a decision with a name rather than an accident of path layout.
        roots = [
            ("project", self.project_root / ".claude" / "skills"),
            ("project", self.project_root / "skills"),
        ]
        if (os.environ.get("MCP_SKILLS_INCLUDE_PERSONAL") or "").strip().lower() in (
                "1", "true", "yes", "on"):
            roots += [
                ("personal", Path.home() / ".claude" / "skills"),
                # Personal Skills have the final say on a same-name collision.
                ("personal", Path.home() / "skills"),
            ]
        return roots

    def _state_for(self, skill: Skill) -> tuple[str, str]:
        key = self._key(skill.path)
        with self._connect() as db:
            source = db.execute(
                "SELECT provenance FROM skill_sources WHERE source_path=?", (key,)
            ).fetchone()
            trusted = db.execute(
                "SELECT digest FROM skill_trust WHERE source_path=?", (key,)
            ).fetchone()
        provenance = source["provenance"] if source else "external"
        if trusted and hmac.compare_digest(trusted["digest"], skill.digest):
            trust = "trusted"
        elif trusted:
            trust = "changed"
        else:
            trust = "untrusted"
        return provenance, trust

    def discover(self) -> list[Skill]:
        self._sync_gate_approvals()
        # Later roots override earlier ones: native beats compatibility within a
        # scope, and the user's personal library beats a project collision.
        selected: dict[str, Skill] = {}
        # Bundles that failed to load, as {folder name: reason}. Kept on the store so
        # callers can SAY why a Skill is missing. Silently skipping them (the previous
        # behaviour) is the worst outcome for an author: a SKILL.md with, say, an
        # unquoted ':' in its description is invalid YAML, so the Skill never appears
        # in any listing and no error is raised anywhere -- it just does not exist,
        # with nothing to debug.
        invalid: dict[str, str] = {}
        # Keys of `invalid` whose latest entry came from a `name:` a broken file DECLARES
        # rather than from the folder it sits in.
        declared_only: set[str] = set()
        outcomes = self._outcomes_cached() if self.use_cache else self._outcomes_uncached()
        for skill_md, skill, error, declared in outcomes:
            if skill is not None:
                selected[skill.name] = skill
                continue
            # The bundle is never partially exposed to the model, but the
            # reason is recorded so a human can be told what to fix. Record it
            # under the folder name AND under the `name:` the file declares:
            # a broken bundle is usually looked up by the name its author
            # wrote, which need not match the folder it sits in.
            folder = skill_md.parent.name
            invalid[folder] = error
            declared_only.discard(folder)
            if declared and declared != folder:
                invalid[declared] = error
                declared_only.add(declared)
        # A DECLARED NAME THAT A REAL SKILL ANSWERS TO IS NOT BROKEN. A second folder whose
        # SKILL.md merely claims an existing name (a copy, an impostor) is refused -- and is
        # still reported under its own folder -- but recording its error under the claimed
        # name too made the real, approved, loadable Skill show up as "invalid" in skill_list,
        # contradicting get() and skill_load. The alias exists so a name that resolves to
        # NOTHING can say why; when the name does resolve, it has nothing to explain.
        for name in declared_only & set(selected):
            del invalid[name]
        self.invalid = invalid
        return sorted(selected.values(), key=lambda s: s.name)

    def _outcomes_uncached(self):
        """(skill_md, Skill-with-trust | None, error | None, declared name) per bundle, in
        discovery order: every bundle re-read and every trust state queried on its own."""
        for scope, root in self.roots():
            if not root.is_dir():
                continue
            for skill_md in sorted(root.glob("*/SKILL.md")):
                try:
                    skill = load_bundle(skill_md.parent, scope)
                except SkillError as exc:
                    yield skill_md, None, str(exc), _declared_name(skill_md)
                    continue
                provenance, trust = self._state_for(skill)
                yield skill_md, dataclasses.replace(
                    skill, provenance=provenance, trust=trust), None, ""

    def _candidates(self, root: Path):
        """What sorted(root.glob("*/SKILL.md")) yields, as (skill_md, bundle dir entry,
        listing | None), with each bundle's listing taken on the way.

        glob's own test for "*/SKILL.md" is a stat() per subdirectory -- a file open on
        Windows, 500 of them for 500 Skills -- when the listing this needs anyway already says
        whether SKILL.md is there. Where the listing cannot say it the way glob would (a link,
        or a name that differs only in case, which a case-insensitive filesystem matches)
        the answer is glob's own: Path.exists().
        """
        found = []
        with os.scandir(root) as it:
            entries = list(it)
        for entry in entries:
            try:
                if not entry.is_dir():          # follows links, as glob's "*/" does
                    continue
            except OSError:
                continue
            skill_md = root / entry.name / "SKILL.md"
            listing = None
            taken = _time_ns()
            if not _is_link(entry):
                listing = _listing(entry.path)
            if listing is not None and "SKILL.md" in listing[2]:
                present = True
            elif listing is not None and not any(n.lower() == "skill.md" for n in listing[2]):
                present = False
            else:
                present = skill_md.exists()
            if present:
                found.append((skill_md, entry, listing, taken))
        found.sort(key=lambda row: row[0])
        return found

    def _outcomes_cached(self):
        """_outcomes_uncached, answered from the bundle cache wherever the listing allows."""
        state = None
        for scope, root in self.roots():
            if not root.is_dir():
                continue
            root_key = os.path.normcase(str(root.resolve()))
            with _CACHE_LOCK:
                previous = _CACHE.get(root_key, {})
            current: dict[str, _CacheEntry] = {}
            for skill_md, entry, listing, taken in self._candidates(root):
                key = os.path.normcase(os.path.join(root_key, entry.name))
                cached = previous.get(key)
                if listing is not None and cached is not None and \
                        cached.signature == listing[0]:
                    hit = cached
                    current[key] = cached
                else:
                    try:
                        skill = load_bundle(skill_md.parent, scope)
                        hit = _CacheEntry(listing[0] if listing else (), skill, None, "",
                                          self._key(skill.path))
                    except SkillError as exc:
                        hit = _CacheEntry(listing[0] if listing else (), None, str(exc),
                                          _declared_name(skill_md), "")
                    # Only a listing whose every file is safely older than the moment it was
                    # taken may vouch for this content next time; see _RACY_MARGIN_NS.
                    if listing is not None and listing[1] < taken - _RACY_MARGIN_NS:
                        current[key] = hit
                if hit.skill is None:
                    yield skill_md, None, hit.error, hit.declared
                    continue
                if state is None:
                    state = self._all_states()
                provenance, trust = self._state_from(hit.key, hit.skill.digest, *state)
                yield skill_md, dataclasses.replace(
                    hit.skill, scope=scope, metadata=copy.deepcopy(hit.skill.metadata),
                    provenance=provenance, trust=trust), None, ""
            with _CACHE_LOCK:
                _CACHE[root_key] = current
                _CACHE.move_to_end(root_key)
                while len(_CACHE) > _CACHE_MAX_ROOTS:
                    _CACHE.popitem(last=False)

    def _all_states(self) -> tuple[dict[str, str], dict[str, str]]:
        """Every recorded provenance and approved digest, in two queries instead of two per
        Skill. The tables hold one row per bundle path ever recorded, so reading them whole
        is the batch."""
        with self._connect() as db:
            sources = {row[0]: row[1] for row in
                       db.execute("SELECT source_path, provenance FROM skill_sources")}
            trusted = {row[0]: row[1] for row in
                       db.execute("SELECT source_path, digest FROM skill_trust")}
        return sources, trusted

    @staticmethod
    def _state_from(key: str, digest: str, sources: dict[str, str],
                    trusted: dict[str, str]) -> tuple[str, str]:
        """_state_for's decision, from _all_states' rows."""
        provenance = sources.get(key, "external")
        approved = trusted.get(key)
        if approved is not None and hmac.compare_digest(approved, digest):
            trust = "trusted"
        elif approved is not None:
            trust = "changed"
        else:
            trust = "untrusted"
        return provenance, trust

    def _verified(self, skill: Skill) -> bool:
        """Do this Skill's bytes on disk still hash to the digest it carries? (See the comment
        above _CacheEntry: the listing is trusted to list and match, never to vouch for content
        that is about to be handed over or approved.) On a mismatch the cache entry is dropped,
        so the next discover() reads the real content."""
        return self._verified_digest(skill.path, skill.scope, skill.digest)

    def _verified_digest(self, path: Path, scope: str, digest: str) -> bool:
        if not self.use_cache:
            return True
        try:
            fresh = load_bundle(path, scope)
        except SkillError:
            fresh = None
        if fresh is not None and hmac.compare_digest(fresh.digest, digest):
            return True
        _forget_bundle(path)
        return False

    def invalid_bundles(self) -> dict[str, str]:
        """Folder name -> why its SKILL.md could not be loaded (after discover())."""
        if not hasattr(self, "invalid"):
            self.discover()
        return dict(self.invalid)

    def get(self, name: str, strict: bool = False) -> Skill:
        """The Skill called `name`. strict=True re-hashes its bytes before returning it --
        required wherever its content is handed over or a trust decision is recorded."""
        for _attempt in range(3):
            skill = self._get(name)
            if not strict or self._verified(skill):
                return skill
        # Three consecutive mismatches: the bundle is being rewritten under us. Refuse rather
        # than hand over content nobody has hashed.
        raise SkillError(f"Skill {name} is changing on disk; try again")

    def _get(self, name: str) -> Skill:
        match = next((s for s in self.discover() if s.name == name), None)
        if not match:
            # A folder of that name that failed to parse is the likeliest reason a
            # Skill "does not exist": say so instead of the bare unknown-name error,
            # which sends the author looking in the wrong place.
            reason = self.invalid_bundles().get(name)
            if reason:
                raise SkillError(
                    f"Skill '{name}' exists on disk but its SKILL.md could not be "
                    f"loaded: {reason}"
                )
            raise SkillError(f"unknown Skill: {name}")
        return match

    def list_metadata(self, model_safe: bool = False) -> list[dict[str, Any]]:
        rows = []
        for skill in self.discover():
            row = skill.public_metadata()
            if model_safe and skill.trust != "trusted":
                # Even frontmatter descriptions and crafted filenames can contain prompt
                # injection. A model only needs identity/trust state before human approval.
                row["description"] = "(hidden until human approval)"
                row["files"] = []
            rows.append(row)
        return rows

    def request_approval(self, name: str) -> dict[str, Any]:
        # strict: the digest written into the question is the one a person will be trusting.
        skill = self.get(name, strict=True)
        if skill.trust == "trusted":
            return {"status": "already-trusted", "skill": skill.public_metadata()}
        now = time.time()
        # One transaction, and its first statement (the DELETE) takes SQLite's write lock, so
        # a second request for the same Skill waits and then finds this one's row: one
        # question per decision (tests/test_skills_business_lifecycle.py,
        # test_simultaneous_requests_share_one_question).
        with self._connect() as db:
            db.execute("DELETE FROM approval_challenges WHERE expires_at < ?", (now,))
            existing = db.execute(
                "SELECT gate_token FROM approval_challenges "
                "WHERE source_path=? AND digest=? AND expires_at>=? ORDER BY created_at DESC",
                (self._key(skill.path), skill.digest, now),
            ).fetchone()
            token = existing["gate_token"] if existing and existing["gate_token"] else (
                "gate_skill_" + secrets.token_hex(8)
            )
            if not existing:
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                db.execute(
                    "INSERT INTO approval_challenges "
                    "(token_hash, gate_token, source_path, digest, expires_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (token_hash, token, self._key(skill.path), skill.digest,
                     now + APPROVAL_TTL_SECONDS, now),
                )
        scripts = [f for f in skill.files if f.startswith("scripts/")]
        dynamic = [line.strip() for line in skill.body.splitlines() if line.lstrip().startswith("!")]
        key = self._key(skill.path)
        with self._connect() as db:
            old = db.execute(
                "SELECT manifest_json FROM skill_trust WHERE source_path=?", (key,)
            ).fetchone()
        try:
            old_manifest = json.loads(old["manifest_json"]) if old else {}
        except (TypeError, json.JSONDecodeError):
            old_manifest = {}
        current_manifest = dict(skill.manifest)
        changed_files = {
            "added": sorted(set(current_manifest) - set(old_manifest)),
            "modified": sorted(
                path for path in set(current_manifest) & set(old_manifest)
                if current_manifest[path] != old_manifest[path]
            ),
            "removed": sorted(set(old_manifest) - set(current_manifest)),
        }
        gate_path = self.gate_dir / f"{token}.json"
        if not gate_path.is_file():
            self._write_approval_gate(
                skill, token, changed_files, scripts, dynamic,
                skill.metadata.get("allowed-tools") or [], now,
            )
        return {
            "status": "confirmation-required",
            "token": token,
            "expires_in_seconds": APPROVAL_TTL_SECONDS,
            "gate_path": str(gate_path),
            "skill": skill.public_metadata(),
            "scripts": scripts,
            "dynamic_commands": dynamic,
            "requested_tools": skill.metadata.get("allowed-tools") or [],
            "changed_files": changed_files,
            "instruction_preview": skill.body[:8000],
            "instruction_preview_truncated": len(skill.body) > 8000,
            "bundle_limits": {
                "max_files": MAX_FILES,
                "max_file_bytes": MAX_FILE_BYTES,
                "max_total_bytes": MAX_TOTAL_BYTES,
            },
            "warning": (
                "This approval only trusts this exact bundle digest. "
                "Shell, file changes, and outbound actions remain subject to existing gates."
            ),
        }

    def confirm_approval(self, name: str, token: str) -> dict[str, Any]:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        # READ THE CHALLENGE BEFORE get(). get() runs discover(), and discover() runs
        # _sync_gate_approvals(), which deletes every expired challenge first -- so by the time
        # this looked, an expired token had always already vanished and the "has expired"
        # branch below was unreachable: a person who was simply too slow was told their token
        # was WRONG. What was there before the sweep decides which of the two it is.
        with self._connect() as db:
            before = db.execute(
                "SELECT expires_at FROM approval_challenges WHERE token_hash=?", (token_hash,)
            ).fetchone()
        skill = self.get(name, strict=True)  # Re-hash immediately before trusting.
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM approval_challenges WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if not row:
                if (before is not None and before["expires_at"] < now) or \
                        self._gate_expired(token, now):
                    raise SkillError("approval token has expired; request approval again")
                raise SkillError("approval token is invalid")
            if row["expires_at"] < now:
                db.execute("DELETE FROM approval_challenges WHERE token_hash=?", (token_hash,))
                raise SkillError("approval token has expired; request approval again")
            if row["source_path"] != self._key(skill.path) or not hmac.compare_digest(
                row["digest"], skill.digest
            ):
                raise SkillError("Skill changed after review; request approval again")
            db.execute(
                "INSERT OR REPLACE INTO skill_trust "
                "(source_path, digest, approved_at, approval_kind, manifest_json) "
                "VALUES (?, ?, ?, 'human', ?)",
                (self._key(skill.path), skill.digest, now,
                 json.dumps(dict(skill.manifest), sort_keys=True)),
            )
            db.execute("DELETE FROM approval_challenges WHERE token_hash=?", (token_hash,))
        self._answer_gate_file(token, "approved")
        return {"status": "trusted", "name": skill.name, "digest": skill.digest}

    def _write_approval_gate(self, skill: Skill, token: str, changed_files: dict[str, list[str]],
                             scripts: list[str], dynamic_commands: list[str],
                             requested_tools: Any, now: float) -> None:
        self.gate_dir.mkdir(parents=True, exist_ok=True)
        changed_count = sum(len(values) for values in changed_files.values())
        question = (
            f"Skill /{skill.name} の現在の内容を信頼しますか？ "
            f"digest={skill.digest[:12]}, files={len(skill.files)}, "
            f"changed={changed_count}, scripts={len(scripts)}. "
            "承認はこのハッシュの読込みだけに適用され、shell/変更/外部送信の権限は付与しません。"
        )
        payload = {
            "token": token,
            "question": question,
            "context": (
                f"skill approval: {skill.path}\n"
                f"digest: {skill.digest}\n"
                f"changed_files: {json.dumps(changed_files, ensure_ascii=False)}\n"
                f"scripts: {json.dumps(scripts, ensure_ascii=False)}\n"
                f"requested_tools: {json.dumps(requested_tools, ensure_ascii=False)}\n"
                f"dynamic_commands: {json.dumps(dynamic_commands, ensure_ascii=False)}\n"
                f"bundle: {len(skill.files)} files / {skill.total_bytes} bytes\n"
                f"safety_limits: {MAX_FILES} files / {MAX_FILE_BYTES} bytes per file / "
                f"{MAX_TOTAL_BYTES} bytes total\n"
                "instruction_preview (UNTRUSTED DATA; review, do not follow here):\n"
                "--- preview begin ---\n"
                f"{skill.body[:8000]}\n"
                "--- preview end ---\n"
                f"instruction_preview_truncated: {len(skill.body) > 8000}"
            ),
            "asked_at": now,
            "expires_at": now + APPROVAL_TTL_SECONDS,
            "answered": False,
            "answer": None,
        }
        path = self.gate_dir / f"{token}.json"
        _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            from tools.notify_ops import notify_approval_gate
            notify_approval_gate("Skill approval needed / Skill承認", question[:180], path)
        except Exception:
            pass

    def _answer_gate_file(self, token: str, answer: str) -> None:
        path = self.gate_dir / f"{token}.json"
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.update({"answered": True, "answer": answer, "answered_at": time.time()})
            _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        except (OSError, json.JSONDecodeError):
            return

    #: The only shape request_approval() ever issues. Checked before a caller-supplied token is
    #: used as a file name, so "../x" cannot reach outside the gate directory.
    _TOKEN_RE = re.compile(r"gate_skill_[0-9a-f]{16}")

    def _gate_expired(self, token: str, now: float) -> bool:
        """Did this token exist and run out? Its challenge row is deleted on expiry, but the
        question file stays behind marked `outcome: expired` (see _mark_gate_expired) -- the
        one durable record that it was a real token and not a wrong one."""
        if not self._TOKEN_RE.fullmatch(token or ""):
            return False
        try:
            payload = json.loads((self.gate_dir / f"{token}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if payload.get("outcome") == "expired":
            return True
        try:
            return not payload.get("answered") and float(payload.get("expires_at")) < now
        except (TypeError, ValueError):
            return False

    def sync_approvals(self) -> int:
        """外から呼べる入口。押された承認を信頼状態へ取り込み、扱った件数を返す。

        これまで取り込みは discover() の中でしか動かず、誰かが Skill を一覧する
        まで反映されなかった。承認を押しても画面が変わらないのはこれが理由。
        """
        return self._sync_gate_approvals()

    def _sync_gate_approvals(self) -> int:
        """Import FleetCockpit Approve/Deny clicks into exact-digest trust state."""
        now = time.time()
        handled = 0
        with self._connect() as db:
            rows = db.execute("SELECT * FROM approval_challenges").fetchall()
        for row in rows:
            token = row["gate_token"]
            if not token:
                continue
            gate_path = self.gate_dir / f"{token}.json"
            if row["expires_at"] < now:
                with self._connect() as db:
                    db.execute("DELETE FROM approval_challenges WHERE token_hash=?",
                               (row["token_hash"],))
                # 期限切れを黙って消さない。押した本人には、承認したつもりが効いて
                # いないことが分かるようにする。消してしまうと、画面から消えた＝
                # 通ったのだと読めてしまう。
                self._mark_gate_expired(gate_path)
                handled += 1
                continue
            try:
                gate = json.loads(gate_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not gate.get("answered"):
                continue
            answer = str(gate.get("answer") or "").strip().lower()
            if answer == "approved":
                try:
                    skill = load_bundle(Path(row["source_path"]), "external")
                except SkillError:
                    skill = None
                if skill is not None and hmac.compare_digest(skill.digest, row["digest"]):
                    with self._connect() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO skill_trust "
                            "(source_path, digest, approved_at, approval_kind, manifest_json) "
                            "VALUES (?, ?, ?, 'fleet-cockpit', ?)",
                            (row["source_path"], skill.digest, now,
                             json.dumps(dict(skill.manifest), sort_keys=True)),
                        )
            with self._connect() as db:
                db.execute("DELETE FROM approval_challenges WHERE token_hash=?",
                           (row["token_hash"],))
            handled += 1
        return handled

    def _mark_gate_expired(self, gate_path: Path) -> None:
        """期限切れの確認画面に、何が起きたのかを書いて残す。"""
        if not gate_path.is_file():
            return
        try:
            payload = json.loads(gate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("outcome") == "expired":
            return
        was = str(payload.get("answer") or "").strip().lower()
        payload.update({
            "answered": True,
            "answer": "expired",
            "outcome": "expired",
            "answered_at": payload.get("answered_at") or time.time(),
            "note": ("有効期限が切れたため、この承認は反映されていません。"
                     "元の操作からやり直してください。"
                     if was == "approved" else
                     "有効期限が切れました。元の操作からやり直してください。"),
        })
        try:
            _atomic_write_text(gate_path, json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError:
            return

    def _record_source(self, skill: Skill, provenance: str, auto_trust: bool) -> None:
        now = time.time()
        key = self._key(skill.path)
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO skill_sources (source_path, provenance, recorded_at) "
                "VALUES (?, ?, ?)", (key, provenance, now),
            )
            if auto_trust:
                db.execute(
                    "INSERT OR REPLACE INTO skill_trust "
                    "(source_path, digest, approved_at, approval_kind, manifest_json) "
                    "VALUES (?, ?, ?, 'local-created', ?)",
                    (key, skill.digest, now, json.dumps(dict(skill.manifest), sort_keys=True)),
                )

    def create_local(self, name: str, description: str, body: str = "") -> Skill:
        if not NAME_RE.fullmatch(name):
            raise SkillError("Skill name must use lowercase letters, digits, and hyphens (max 64)")
        if not description.strip():
            raise SkillError("description is required")
        target = self.project_root / "skills" / name
        if target.exists():
            raise SkillError(f"Skill already exists: {target}")
        target.mkdir(parents=True)
        content = (
            "---\n"
            f"name: {name}\n"
            f"description: {json.dumps(description.strip(), ensure_ascii=False)}\n"
            "---\n\n"
            + (body.strip() or f"# {name}\n\nDescribe the reusable workflow here.")
            + "\n"
        )
        (target / "SKILL.md").write_text(content, encoding="utf-8")
        skill = load_bundle(target, "project")
        self._record_source(skill, "local-authored", auto_trust=True)
        provenance, trust = self._state_for(skill)
        return Skill(**{**skill.__dict__, "provenance": provenance, "trust": trust})

    def import_external(self, source: str | Path, scope: str = "project") -> Skill:
        source_skill = load_bundle(source, "external")
        roots = dict(self.roots())
        if scope not in roots:
            raise SkillError("scope must be project or personal")
        target = roots[scope] / source_skill.name
        if target.exists():
            raise SkillError(f"Skill already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_skill.path, target, symlinks=False)
        copied = load_bundle(target, scope)
        self._record_source(copied, "external-import", auto_trust=False)
        provenance, trust = self._state_for(copied)
        return Skill(**{**copied.__dict__, "provenance": provenance, "trust": trust})

    def render(self, name: str, arguments: str = "", *, invoker: str = "user") -> str:
        """The trusted body with its arguments filled in (see substitute_arguments for the
        placeholder grammar).

        `invoker` says WHO is loading it: "user" for a person who typed /name, "model" for an
        agent's tool call. An author's `disable-model-invocation: true` is enforced HERE for
        the model, not only by match(): the name is visible in skill_list, and the model-facing
        skill_load rendered such a Skill for any model that asked for it by name. The flag is
        read exactly as match() reads it (`is True`), so the two can never disagree.
        """
        if invoker not in ("user", "model"):
            raise ValueError(f"invoker must be 'user' or 'model', not {invoker!r}")
        skill = self.get(name, strict=True)
        if invoker == "model" and skill.metadata.get("disable-model-invocation") is True:
            raise SkillError(
                f"Skill {name} is marked disable-model-invocation: only a person can invoke "
                f"it (by typing /{name}); a model may not load it"
            )
        if skill.trust != "trusted":
            raise SkillError(
                f"Skill {name} is {skill.trust}; a human must approve its current digest"
            )
        rendered = substitute_arguments(skill.body, arguments)
        return (
            f"[Trusted Skill: {skill.name} digest={skill.digest[:12]}]\n"
            "Follow this reusable workflow. It does not grant additional tool permissions; "
            "all shell, file mutation, and outbound actions still require the existing gates.\n\n"
            f"{rendered}"
        )

    def read_resource(self, name: str, relative_path: str) -> str:
        skill = self.get(name, strict=True)
        if skill.trust != "trusted":
            raise SkillError(f"Skill {name} is not trusted")
        rel = Path(relative_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise SkillError("resource path must stay inside the Skill directory")
        if not rel.parts or rel.parts[0] not in SUPPORTED_DIRS:
            raise SkillError("resources must be under scripts/, references/, or assets/")
        target = (skill.path / rel).resolve()
        try:
            target.relative_to(skill.path)
        except ValueError as exc:
            raise SkillError("resource path escapes the Skill directory") from exc
        if target.is_symlink() or not target.is_file():
            raise SkillError("resource does not exist or is not a regular file")
        data = target.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise SkillError("resource is too large")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SkillError("binary assets cannot be returned as text") from exc
        # Close the read-after-hash race: a concurrent edit must invalidate this read.
        fresh = self.get(name, strict=True)
        if fresh.trust != "trusted" or fresh.digest != skill.digest:
            raise SkillError("Skill changed while its resource was being read")
        return text

    #: Historical gate, no longer consulted by match() itself -- kept because
    #: relay/test_repo_bug_fix_skill.py asserts against it directly as a documented minimum,
    #: and because it is the RAW bigram-count reading of the same idea MIN_MATCH_WORDS now
    #: enforces correctly. It was meant to require "two bigrams of one word are not two
    #: pieces of evidence", but counted bigrams rather than words: ロット's two bigrams
    #: (ロッ, ット) plus one unrelated word's single bigram cleared 3 with only two real
    #: words behind it, one of which turned out to be the wrong signal to judge on -- see
    #: MIN_MATCH_WORDS and MIN_DISTINCTIVE_WORDS below, which replaced its role in match().
    MIN_MATCH_TOKENS = 3

    #: Distinct real WORDS -- not bigrams -- a candidate's overlap with the query must
    #: contain, counting only content (kanji/katakana/ASCII) words. See _merge_word_groups:
    #: _match_tokens slides a 2-character window across every run, so one word of 3+
    #: characters yields several overlapping bigrams that used to count as that many separate
    #: pieces of evidence.
    #:
    #: WHY THIS EXISTS, MEASURED LIVE 2026-09-15. A query investigating a different material's
    #: lots -- naming neither -- scored 1.0 against copper-foil-survey with the full body
    #: injected. Its overlap was exactly {ロッ, ット, 調査}: three tokens, all content, each
    #: passing the content-only filter this replaced (MIN_CONTENT_FRACTION, see git history) --
    #: that filter caught a DIFFERENT wrong match (one leaning on hiragana grammar fragments)
    #: and was never going to catch this one, because none of these three tokens is a grammar
    #: fragment. ロッ and ット are the two bigrams of ONE word, ロット (lot) -- the same shape
    #: test_a_shared_word_is_not_enough already existed to catch, except that test's cases
    #: supply no second real word, and this query does (調査, investigate). So the query
    #: reduces to two real words, ロット and 調査, and MIN_MATCH_TOKENS=3 was satisfied by
    #: three BIGRAMS of those two words -- the gate counted evidence sources wrong, not too
    #: few of them. Merging by shared-boundary-character (see _merge_word_groups) turns that
    #: overlap into 2 word-groups; MIN_MATCH_WORDS below is measured against the whole fixture
    #: plus this case, not chosen to make one query pass.
    MIN_MATCH_WORDS = 2

    #: Of a candidate's overlap (merged, content-only, see above), this many word-groups must
    #: be DISTINCTIVE: absent from every OTHER trusted candidate's vocabulary. See
    #: _document_frequency. Content is not the discriminator a small store needs --
    #: 調査, 調べ, ロット and similar are shared by any procedure that investigates records,
    #: and a token every candidate could plausibly use says nothing about which one is meant.
    #:
    #: MEASURED. The live wrong match's two merged words are ロット (df=1: only
    #: copper-foil-survey's vocabulary has it, among the 5 Skills trusted today) and 調査
    #: (df=2: also in mail-lookup's). Requiring 2 distinctive words rejects it -- only ロット
    #: qualifies. Requiring only 1 does not: ロット alone clears it, and the live case was
    #: this test's whole reason for existing. On the fixture, 2 also holds every existing
    #: match: メールを検索したい's two merged words, メール and 検索, are BOTH df=1 (df is
    #: computed the same way -- across the 5 currently trusted Skills, not some larger corpus,
    #: so it moves if the store's shape moves).
    #:
    #: WHAT IT COSTS, MEASURED. 先月のメールを一覧にして now misses: its two merged words are
    #: メール (df=1) and 一覧 (df=2, shared with desktop-md-inventory's own listing), the exact
    #: shape of the wrong match this exists to catch -- one distinctive word plus one shared
    #: one -- and nothing available to this matcher tells those two situations apart. This
    #: matcher already favours false negatives on purpose; between the two identically-shaped
    #: cases, the one that used to be wrong-and-expensive is the one worth losing the other to
    #: refuse. Re-measure both directions with scripts/win/skill_match_bench.py before
    #: changing this.
    MIN_DISTINCTIVE_WORDS = 2

    def match(self, text: str) -> dict[str, Any] | None:
        """_match_once, with the winner's bytes re-hashed before it is reported as trusted.

        The decision is _match_once's, unchanged. What this adds is the cache's promise (see
        the comment above _CacheEntry): the Skill named here, and the description returned
        with it, are the approved content, not a listing's memory of it. If the winner's bytes
        no longer hash to its digest, its cache entry is dropped and the match is recomputed
        from what is really on disk.
        """
        for _attempt in range(3):
            result = self._match_once(text)
            if result is None or not self.use_cache:
                return result
            if self._verified_digest(Path(result["path"]), result["scope"], result["digest"]):
                return result
        return None

    def _match_once(self, text: str) -> dict[str, Any] | None:
        """Conservatively select one trusted, model-invocable Skill by metadata only.

        This intentionally favors false negatives. A Skill body is not loaded until a
        clear description/when_to_use match exists, preserving progressive disclosure.

        THE DENOMINATOR IS WHAT THE LIBRARY KNOWS ABOUT, not the whole query. Dividing by the
        query's own length meant a token no Skill has ever heard of still counted against the
        match: adding a date to a question lowered its score, and the phrasings people
        actually use -- "last month's mail", "January 2026's mail" -- were refused while the
        bare "search my mail" passed. A date now leaves the denominator instead of inflating
        it, which is the difference between not-evidence and evidence-against.

        Two consultants were asked and proposed different cures; both are in here, and
        MEASURED, because neither was sufficient alone. Cleaner tokens (see _match_tokens)
        raised the passing scores but left the date queries under the bar. This denominator
        alone, on the old tokeniser, fixed every miss and produced four wrong matches -- the
        expensive kind, where the agent follows a procedure meant for something else. Together
        with MIN_MATCH_TOKENS: 16 of 16 on the fixture, no misses, no wrong matches.
        Re-measure with scripts/win/skill_match_bench.py before touching any of it.

        MIN_MATCH_WORDS and MIN_DISTINCTIVE_WORDS (below) are a later, separate fix for a
        different fail-open this one does not touch: with few Skills trusted, `known` can
        collapse to one Skill's own vocabulary regardless of topic, so precision against it
        is 1.0 by construction, and a single word's overlapping bigrams could be counted as
        multiple pieces of evidence. See those constants' docstrings for what they catch and,
        as important, what they do not.
        """
        query = _match_tokens(text)
        if len(query) < 2:
            return None
        candidates = [s for s in self.discover()
                      if s.trust == "trusted"
                      and s.metadata.get("disable-model-invocation") is not True]
        vocabulary: set[str] = set()
        haystacks: dict[str, set[str]] = {}
        for skill in candidates:
            terms = _match_tokens(
                skill.description + " " + str(skill.metadata.get("when_to_use") or ""))
            haystacks[skill.name] = terms
            vocabulary |= terms
        # How many trusted candidates' vocabulary a token appears in. A token every procedure
        # in the store could plausibly use is not evidence for any one of them; see
        # MIN_DISTINCTIVE_WORDS.
        doc_freq = {tok: sum(1 for terms in haystacks.values() if tok in terms)
                    for tok in vocabulary}
        known = query & vocabulary
        known_words = _content_word_groups(known)
        if len(known_words) < self.MIN_MATCH_WORDS:
            return None
        scored: list[tuple[float, Skill]] = []
        for skill in candidates:
            terms = haystacks[skill.name]
            if not terms:
                continue
            overlap = query & terms
            overlap_words = _content_word_groups(overlap)
            if len(overlap_words) < self.MIN_MATCH_WORDS:
                continue
            distinctive = [g for g in overlap_words
                           if any(doc_freq.get(t, 99) <= 1 for t in g)]
            if len(distinctive) < self.MIN_DISTINCTIVE_WORDS:
                continue
            score = len(overlap_words) / max(1, len(known_words))
            if skill.name in text.lower():
                score += 0.6
            scored.append((score, skill))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best_score < 0.55 or (best_score - second < 0.15 and best_score < 1.0):
            return None
        return {"score": round(best_score, 3), **best.public_metadata()}

    def match_unapproved(self, text: str) -> dict[str, Any] | None:
        """The best Skill that WOULD have matched, had a human approved it.

        WHY THIS EXISTS. Approval is requested from exactly one place in the whole system --
        a command inside the chat CLI's REPL -- so nothing an agent does can ever ask for it.
        Six Skills sat unreadable for weeks as a result: two of them had been approved on
        2026-08-06 and fell back to `changed` the moment a character of their text moved,
        which is correct (the approval is of a hash) and terminal (nothing re-requests it).
        The Approval Centre showed nothing to approve, truthfully: no request had been made.

        Matching untrusted metadata is safe. Names and descriptions are read, the body is
        not, and nothing here decides to trust anything -- it only reports that a decision is
        waiting to be made by a person. The threshold is deliberately looser than match():
        this asks "is this worth putting in front of the user", not "may this be loaded".
        """
        query = _match_tokens(text)
        if len(query) < 2:
            return None
        best_score, best = 0.0, None
        for skill in self.discover():
            if skill.trust == "trusted":
                continue
            if skill.metadata.get("disable-model-invocation") is True:
                continue
            haystack = skill.description + " " + str(skill.metadata.get("when_to_use") or "")
            terms = _match_tokens(haystack)
            if not terms:
                continue
            score = len(query & terms) / max(1, min(len(query), len(terms)))
            if skill.name in text.lower():
                score += 0.6
            if score > best_score:
                best_score, best = score, skill
        # A LOWER BAR THAN match(), on purpose, and lower again than it first looked right.
        # Japanese matches on character bigrams, so a short question against a long
        # description scores low by construction: "2026年1月のメールを検索して一覧にしたい"
        # overlaps /mail-lookup on メー・ール・検索 and scores 0.19. At 0.35 the very case
        # this exists for never fired. Nothing is granted by clearing this bar -- a question
        # is written for a person -- and requests are de-duplicated by digest, so the worst
        # case is one pending question per Skill that exists, which is exactly the list the
        # user was asking to see.
        if best is None or best_score < 0.25:
            return None
        # THE DIGEST BELONGS IN HERE, because `trust` is a fact about a digest and not about a
        # name: "changed" means precisely that this bundle's hash is not the one approved. A
        # caller that wants to ask about the version it actually saw had no way to say which
        # version that was, and a de-duplication key built from the name alone silently never
        # changes when the Skill is edited -- which is the one case where asking again is
        # right. Adding a key; every existing reader takes name and trust.
        return {"score": round(best_score, 3), "name": best.name, "trust": best.trust,
                "digest": best.digest}

    def unapproved(self) -> list[dict[str, Any]]:
        """Every Skill a person has not (or no longer has) approved, newest question first."""
        return [{"name": s.name, "trust": s.trust, "scope": s.scope}
                for s in self.discover() if s.trust != "trusted"]


def format_skill_list(skills: Iterable[dict[str, Any]]) -> str:
    rows = list(skills)
    if not rows:
        return "(no Skills found)"
    return "\n".join(
        f"/{row['name']} [{row['scope']}, {row['trust']}, {row['provenance']}] "
        f"- {row['description']}" for row in rows
    )


#: Hiragana that carries grammar rather than topic. Only pieces this long need naming: the
#: short ones are dropped by length, and the point of segmenting is that this list never has
#: to grow to cover the diluting BIGRAMS themselves, which are the cross product of every
#: content word with every particle and therefore unbounded.
_HIRAGANA_STOP = frozenset((
    "したい", "ください", "について",
    "ように", "しています", "しました",
    "できる", "できます", "ですか",
))


def _script_of(ch: str) -> str:
    o = ord(ch)
    if 0x3040 <= o <= 0x309F:
        return "hira"
    if 0x30A0 <= o <= 0x30FF:
        return "kata"
    return "han"


def _is_content_token(token: str) -> bool:
    """False for a token made entirely of hiragana -- a _merge_word_groups filter's unit.

    _match_tokens already drops short and stoplisted hiragana pieces, but a longer piece it
    keeps still yields boundary bigrams for its whole length, and those still carry no topic:
    a verb conjugation and a matching Skill description can share several of them by pure
    grammatical coincidence. Kanji, katakana, and ASCII tokens are never filtered here --
    the two-script segmentation in _match_tokens already keeps those whole per run, so a
    kanji/katakana bigram is always a fragment of real vocabulary, not grammar.
    """
    return not all(0x3040 <= ord(ch) <= 0x309F for ch in token)


def _merge_word_groups(tokens: set[str]) -> list[set[str]]:
    """Group _match_tokens output back into the real WORDS it fragmented, one group per word.

    _match_tokens slides a 2-character window across every kanji/katakana run, so a single
    word of 3 or more characters yields several overlapping bigrams: ロット (lot) becomes
    {ロッ, ット}, 保証期限 (guarantee period) becomes {保証, 証期, 期限}. Two-character words
    -- most kanji compounds -- already produce exactly one bigram and are untouched by this.

    Sliding-window bigrams from the SAME word always share their overlapping character:
    piece[i:i+2][1] == piece[i+1:i+3][0] by construction. Grouping tokens by that adjacency
    (union-find over the length-2 tokens; ASCII tokens are length 3+ and never chain, being
    already-whole words) recovers word identity without _match_tokens having to carry
    position information, and without changing what _match_tokens itself returns -- callers
    that inspect its raw bigram output are unaffected.

    Two bigrams from genuinely unrelated words can still chain by character coincidence
    (one word's trailing bigram happens to share a character with another's leading one),
    which UNDER-counts -- merges two real words into one group -- rather than over-counts.
    That errs toward fewer matches, the direction this matcher already prefers.
    """
    remaining = set(tokens)
    parent = {t: t for t in remaining}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    twos = [t for t in remaining if len(t) == 2]
    for i, a in enumerate(twos):
        for b in twos[i + 1:]:
            if a[1] == b[0] or b[1] == a[0]:
                union(a, b)
    groups: dict[str, set[str]] = {}
    for t in remaining:
        groups.setdefault(find(t), set()).add(t)
    return list(groups.values())


def _content_word_groups(tokens: set[str]) -> list[set[str]]:
    """_merge_word_groups, keeping only groups that carry at least one content token.

    A group made ENTIRELY of hiragana fragments (see _is_content_token) is grammar, not a
    word this matcher should count as evidence -- see SkillStore.MIN_MATCH_WORDS.
    """
    return [g for g in _merge_word_groups(tokens) if any(_is_content_token(t) for t in g)]


def _match_tokens(text: str) -> set[str]:
    """Topic tokens for matching: ASCII words, and bigrams of the CJK that carries meaning.

    WHY THE SEGMENTATION. Japanese was bigrammed straight across each run, so most tokens
    produced were boundary-straddlers -- one content character glued to a particle. They match
    nothing in any Skill's metadata and they sat in the denominator, so ADDING A DATE TO A
    QUESTION LOWERED ITS SCORE: a plain "search my mail" matched a mail Skill at 0.625 while
    the same question with a month in it scored 0.375 and was refused.

    A stop-list of those bigrams cannot work -- they are the cross product of every content
    word with every particle -- but they share a structure: they straddle into hiragana. The
    run is cut on script boundaries and the hiragana pieces dropped, which removes them
    without naming them. Katakana and kanji segments are kept whole, and the two-character
    floor means single characters fall away on their own.

    Measured on a 16-case fixture (scripts/win/skill_match_bench.py): this alone lifted the
    passing matches from 0.625 to 1.000 but left the date-bearing ones at 0.500, still under
    the bar. It is half the cure; match() supplies the other half.
    """
    lowered = (text or "").lower()
    words = set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", lowered))
    for run in re.findall(r"[぀-ヿ㐀-鿿]{2,}", lowered):
        pieces, seg, script = [], "", None
        for ch in run:
            sc = _script_of(ch)
            if sc != script and seg:
                pieces.append((script, seg))
                seg = ""
            script, seg = sc, seg + ch
        if seg:
            pieces.append((script, seg))
        for sc, piece in pieces:
            if len(piece) < 2:
                continue
            if sc == "hira" and (len(piece) <= 2 or piece in _HIRAGANA_STOP):
                continue
            words.update(piece[i:i + 2] for i in range(len(piece) - 1))
    return words
