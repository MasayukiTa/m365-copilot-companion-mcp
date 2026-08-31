"""Claude-compatible Skill discovery, validation, trust, and rendering.

The important boundary is deliberate:

* trusting a Skill permits its instructions to be loaded;
* it never grants shell, file mutation, or outbound permissions;
* those side effects continue through the existing MCP unlock/contract gates.

Approval is keyed to a digest of the whole Skill directory.  Any change to
``SKILL.md`` or a bundled script/reference/asset invalidates prior approval.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
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


class SkillStore:
    """SQLite trust store and Skill registry for one project/user."""

    def __init__(self, project_root: str | Path, db_path: str | Path | None = None,
                 gate_dir: str | Path | None = None):
        self.project_root = Path(project_root).expanduser().resolve()
        self.db_path = Path(db_path).expanduser().resolve() if db_path else default_state_db()
        self.gate_dir = Path(gate_dir).expanduser().resolve() if gate_dir else self._default_gate_dir()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _default_gate_dir(self) -> Path:
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
        self.invalid: dict[str, str] = {}
        for scope, root in self.roots():
            if not root.is_dir():
                continue
            for skill_md in sorted(root.glob("*/SKILL.md")):
                try:
                    skill = load_bundle(skill_md.parent, scope)
                    provenance, trust = self._state_for(skill)
                    selected[skill.name] = Skill(**{
                        **skill.__dict__, "provenance": provenance, "trust": trust
                    })
                except SkillError as exc:
                    # The bundle is never partially exposed to the model, but the
                    # reason is recorded so a human can be told what to fix. Record it
                    # under the folder name AND under the `name:` the file declares:
                    # a broken bundle is usually looked up by the name its author
                    # wrote, which need not match the folder it sits in.
                    self.invalid[skill_md.parent.name] = str(exc)
                    declared = _declared_name(skill_md)
                    if declared and declared != skill_md.parent.name:
                        self.invalid[declared] = str(exc)
                    continue
        return sorted(selected.values(), key=lambda s: s.name)

    def invalid_bundles(self) -> dict[str, str]:
        """Folder name -> why its SKILL.md could not be loaded (after discover())."""
        if not hasattr(self, "invalid"):
            self.discover()
        return dict(self.invalid)

    def get(self, name: str) -> Skill:
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
        skill = self.get(name)
        if skill.trust == "trusted":
            return {"status": "already-trusted", "skill": skill.public_metadata()}
        now = time.time()
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
        skill = self.get(name)  # Re-hash immediately before trusting.
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM approval_challenges WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if not row:
                raise SkillError("approval token is invalid")
            if row["expires_at"] < now:
                db.execute("DELETE FROM approval_challenges WHERE token_hash=?", (token_hash,))
                raise SkillError("approval token has expired")
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
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
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
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, json.JSONDecodeError):
            return

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
            tmp = gate_path.with_suffix(gate_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, gate_path)
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

    def render(self, name: str, arguments: str = "") -> str:
        skill = self.get(name)
        if skill.trust != "trusted":
            raise SkillError(
                f"Skill {name} is {skill.trust}; a human must approve its current digest"
            )
        rendered = skill.body.replace("$ARGUMENTS", arguments)
        parts = arguments.split()
        for index in range(10):
            rendered = rendered.replace(f"${index}", parts[index] if index < len(parts) else "")
        return (
            f"[Trusted Skill: {skill.name} digest={skill.digest[:12]}]\n"
            "Follow this reusable workflow. It does not grant additional tool permissions; "
            "all shell, file mutation, and outbound actions still require the existing gates.\n\n"
            f"{rendered}"
        )

    def read_resource(self, name: str, relative_path: str) -> str:
        skill = self.get(name)
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
        fresh = self.get(name)
        if fresh.trust != "trusted" or fresh.digest != skill.digest:
            raise SkillError("Skill changed while its resource was being read")
        return text

    #: A match must rest on this many distinct tokens. Two bigrams of ONE word are not two
    #: pieces of evidence: every fail-open measured rested on a single shared word -- a query
    #: about copper prices landing on the copper SURVEY, one about a mail server landing on
    #: mail lookup.
    MIN_MATCH_TOKENS = 3

    def match(self, text: str) -> dict[str, Any] | None:
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
        """
        query = _match_tokens(text)
        if len(query) < 2:
            return None
        candidates = [s for s in self.discover()
                      if s.trust == "trusted"
                      and s.metadata.get("disable-model-invocation") is not True]
        vocabulary: set[str] = set()
        for skill in candidates:
            vocabulary |= _match_tokens(
                skill.description + " " + str(skill.metadata.get("when_to_use") or ""))
        known = query & vocabulary
        if len(known) < self.MIN_MATCH_TOKENS:
            return None
        scored: list[tuple[float, Skill]] = []
        for skill in candidates:
            haystack = skill.description + " " + str(skill.metadata.get("when_to_use") or "")
            terms = _match_tokens(haystack)
            if not terms:
                continue
            overlap = query & terms
            if len(overlap) < self.MIN_MATCH_TOKENS:
                continue
            score = len(overlap) / max(1, len(known))
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
        return {"score": round(best_score, 3), "name": best.name, "trust": best.trust}

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
