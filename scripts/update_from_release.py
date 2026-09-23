"""Update a ZIP install from the latest GitHub Release asset.

This is for users who installed from M365-Companion-*.zip and do not have git.
It downloads the latest release ZIP, verifies it against the release's published
SHA256SUMS.txt, stages the new tree, overlays the committed application files,
and deliberately preserves local runtime state such as .env, .venv, logs, and
setup progress. It is not a binary diff; it is a safe release snapshot refresh.

The SHA256SUMS digest is downloaded from the same release as the ZIP. Matching
it proves the ZIP is byte-for-byte what that release published (integrity). It
is not a proof of who published the release (authenticity) -- that would need
a signature and key management, which is a separate, owner-level decision and
is not implemented here.

The updater journals its progress (.update_journal.json) as it applies files,
so a run interrupted by Ctrl+C or a reboot can be resumed by simply running the
updater again: already-applied files are recognized by content hash and are not
re-flagged as conflicts. The new release tag is only recorded, and success is
only reported, once every file has been applied with zero conflicts.

ASCII / ENGLISH ONLY. This script must run on a fresh Windows install with only
the standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional


OWNER = "MasayukiTa"
REPO = "m365-copilot-companion-mcp"
API_LATEST = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
ASSET_PREFIX = "M365-Companion-"
ASSET_SUFFIX = ".zip"
SHA256SUMS_NAME = "SHA256SUMS.txt"

PRESERVE_NAMES = {
    ".env",
    ".venv",
    ".setup",
    ".fleet",
    ".companion_runs",
    ".companion_gates",
    ".memory_state.json",
    ".procedural_memory.json",
    ".procedural_memory_aliases.json",
    ".unlock_state.json",
    ".todo_state.json",
    "__pycache__",
    "logs",
    "output",
    "out",
    "exports",
    "data",
}

PRESERVE_PREFIXES = {
    Path("agent_memory/facts"),
    Path("agent_memory/topics"),
    Path("agent_memory/sessions"),
    Path("agent_memory/index.json"),
    Path("tools/auto"),
}

MANIFEST_NAME = ".release_manifest.json"
CONFLICT_DIR = ".update_conflicts"
BACKUP_DIR = ".update_backups"
JOURNAL_NAME = ".update_journal.json"


class UpdateError(Exception):
    """Raised for conditions that must abort the update with nothing applied."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_json_url(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "m365-companion-updater"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "m365-companion-updater"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "m365-companion-updater"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


def fetch_release_data() -> dict:
    return read_json_url(API_LATEST)


def pick_zip_asset(data: dict) -> tuple[str, str]:
    assets = data.get("assets") or []
    candidates = [
        a for a in assets
        if str(a.get("name") or "").startswith(ASSET_PREFIX)
        and str(a.get("name") or "").endswith(ASSET_SUFFIX)
    ]
    if not candidates:
        raise UpdateError("latest release has no M365-Companion-*.zip asset")
    asset = sorted(candidates, key=lambda a: str(a.get("name") or ""))[-1]
    return str(asset["name"]), str(asset["browser_download_url"])


def pick_sha_asset(data: dict) -> Optional[tuple[str, str]]:
    assets = data.get("assets") or []
    for a in assets:
        if str(a.get("name") or "") == SHA256SUMS_NAME:
            return str(a["name"]), str(a["browser_download_url"])
    return None


def parse_sha256sums(text: str) -> dict:
    """Parse a 'sha256sum'-style listing: '<hex digest>  <filename>' per line."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        name = name.strip().lstrip("*").strip()
        result[name] = digest.lower()
    return result


def verify_zip_checksum(zip_path: Path, asset_name: str, sha_text: str) -> None:
    sums = parse_sha256sums(sha_text)
    expected = sums.get(asset_name)
    if not expected:
        raise UpdateError(
            f"{SHA256SUMS_NAME} does not list {asset_name}; refusing to apply an "
            "unverified download"
        )
    actual = sha256(zip_path)
    if actual.lower() != expected.lower():
        raise UpdateError(
            f"checksum mismatch for {asset_name}: expected {expected}, got {actual}"
        )


def local_tag(root: Path) -> str:
    info = root / ".release_info.json"
    if not info.exists():
        return ""
    try:
        return str(json.loads(info.read_text(encoding="utf-8")).get("tag_name") or "")
    except Exception:
        return ""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data.get("files") or {}
        return {str(k): str(v) for k, v in files.items()}
    except Exception:
        return {}


def should_skip(rel: Path) -> bool:
    parts = rel.parts
    if parts and parts[0] in PRESERVE_NAMES:
        return True
    rel_norm = Path(*parts) if parts else rel
    for prefix in PRESERVE_PREFIXES:
        if rel_norm == prefix or prefix in rel_norm.parents:
            return True
    return False


def backup_existing(dest: Path, rel: Path, backup_root: Path) -> None:
    if not dest.exists() or dest.is_dir():
        return
    backup = backup_root / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dest, backup)


def conflict_copy(src: Path, rel: Path, conflict_root: Path) -> Path:
    conflict = conflict_root / rel
    conflict.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, conflict)
    return conflict


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest_resolved = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if target != dest_resolved and dest_resolved not in target.parents:
            raise UpdateError(f"unsafe ZIP member path: {member.filename!r}")
    zf.extractall(dest)


def unpack_root(zip_path: Path, work: Path) -> Path:
    with zipfile.ZipFile(zip_path) as zf:
        safe_extract(zf, work)
    children = [p for p in work.iterdir() if p.is_dir()]
    if len(children) == 1:
        return children[0]
    return work


def load_journal(root: Path) -> Optional[dict]:
    path = root / JOURNAL_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_journal(root: Path, data: dict) -> None:
    path = root / JOURNAL_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def clear_journal(root: Path) -> None:
    path = root / JOURNAL_NAME
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def apply_files(
    root: Path,
    staged_root: Path,
    new_manifest: dict,
    old_manifest: dict,
    journal: dict,
) -> tuple[int, int, int, list]:
    """Copy staged files into root.

    Returns (copied, already_applied, skipped, conflicts). `conflicts` is a
    list of [rel_path, conflict_copy_path] for files whose on-disk content
    differs from both the old release's recorded hash and the new release's
    content -- i.e. a local edit that would otherwise be silently clobbered.

    Progress is journaled after every file so a crash or Ctrl+C partway
    through can be resumed: on the next run, a file whose on-disk hash
    already equals the new release's hash is recognized as done rather than
    flagged as a conflict.
    """
    backup_root = root / BACKUP_DIR
    conflict_root = root / CONFLICT_DIR
    applied = set(journal.setdefault("applied", []))
    conflicts = journal.setdefault("conflicts", [])
    conflicted_paths = {c[0] for c in conflicts}
    copied = 0
    already = 0
    skipped = 0

    for rel_str in sorted(new_manifest):
        rel = Path(rel_str)
        new_hash = new_manifest[rel_str]
        if should_skip(rel):
            skipped += 1
            continue
        if rel_str in conflicted_paths:
            continue

        dest = root / rel
        if rel_str in applied:
            if dest.exists() and dest.is_file() and sha256(dest) == new_hash:
                already += 1
                continue
            applied.discard(rel_str)

        src = staged_root / rel
        if dest.exists() and dest.is_file():
            dest_hash = sha256(dest)
            if dest_hash == new_hash:
                applied.add(rel_str)
                journal["applied"] = sorted(applied)
                save_journal(root, journal)
                already += 1
                continue
            if rel_str in old_manifest and dest_hash == old_manifest[rel_str]:
                backup_existing(dest, rel, backup_root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                applied.add(rel_str)
                journal["applied"] = sorted(applied)
                save_journal(root, journal)
                copied += 1
                continue
            conflict_path = conflict_copy(src, rel, conflict_root)
            conflicts.append([rel_str, str(conflict_path)])
            conflicted_paths.add(rel_str)
            journal["conflicts"] = conflicts
            save_journal(root, journal)
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        applied.add(rel_str)
        journal["applied"] = sorted(applied)
        save_journal(root, journal)
        copied += 1

    return copied, already, skipped, conflicts


def remove_stale_files(
    root: Path, old_manifest: dict, new_manifest: dict, journal: dict
) -> tuple[list, list]:
    """Delete files the new release no longer ships.

    A path removed only when the on-disk file still matches the OLD
    release's recorded hash (i.e. never touched locally). A locally modified
    file that upstream removed is moved into the conflicts area instead of
    being deleted, and reported.
    """
    conflict_root = root / CONFLICT_DIR
    deleted = list(journal.setdefault("deleted", []))
    removed_conflicts = journal.setdefault("removed_conflicts", [])
    handled = set(deleted) | {c[0] for c in removed_conflicts}

    for rel_str in sorted(old_manifest):
        if rel_str in new_manifest or rel_str in handled:
            continue
        old_hash = old_manifest[rel_str]
        rel = Path(rel_str)
        if should_skip(rel):
            continue
        dest = root / rel
        if not dest.exists():
            deleted.append(rel_str)
            journal["deleted"] = deleted
            save_journal(root, journal)
            continue
        try:
            dest_hash = sha256(dest)
        except OSError:
            continue
        if dest_hash == old_hash:
            dest.unlink()
            deleted.append(rel_str)
            journal["deleted"] = deleted
        else:
            conflict_path = conflict_root / rel
            conflict_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(conflict_path))
            removed_conflicts.append([rel_str, str(conflict_path)])
            journal["removed_conflicts"] = removed_conflicts
        save_journal(root, journal)

    return deleted, removed_conflicts


def run_update(root: Path, force: bool = False) -> int:
    print("Checking latest GitHub Release...")
    try:
        data = fetch_release_data()
    except Exception as exc:
        print(f"Error: could not check the latest release: {exc}", file=sys.stderr)
        return 1

    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        print("Error: latest release has no tag_name", file=sys.stderr)
        return 1

    try:
        asset_name, asset_url = pick_zip_asset(data)
    except UpdateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    sha_asset = pick_sha_asset(data)
    current = local_tag(root)
    journal = load_journal(root)
    resuming = bool(journal) and journal.get("tag_name") == tag and journal.get("status") != "complete"

    if current == tag and not force and not resuming:
        print(f"Already on latest release: {tag}")
        return 0

    print(f"Latest:  {tag} ({asset_name})")
    print(f"Current: {current}" if current else "Current: unknown ZIP snapshot")
    if resuming:
        print("Resuming a previously interrupted update...")

    if sha_asset is None:
        print(
            f"Error: release {tag} has no {SHA256SUMS_NAME} asset; refusing to apply "
            "an unverified download. Nothing was changed.\n"
            "(A digest from the same release proves integrity, not authenticity --\n"
            "publish a SHA256SUMS.txt asset with the release, or ask the maintainer to.)",
            file=sys.stderr,
        )
        return 1
    _, sha_url = sha_asset

    if not journal or journal.get("tag_name") != tag:
        journal = {
            "tag_name": tag,
            "status": "in_progress",
            "applied": [],
            "conflicts": [],
            "deleted": [],
            "removed_conflicts": [],
        }
        save_journal(root, journal)

    staging_dir = root.parent / f".{root.name}.update-staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="m365-companion-update-") as td:
            tmp = Path(td)
            zip_path = tmp / asset_name

            print("Downloading release checksums...")
            try:
                sha_text = download_text(sha_url)
            except Exception as exc:
                print(f"Error: could not download {SHA256SUMS_NAME}: {exc}", file=sys.stderr)
                return 1

            print("Downloading release ZIP...")
            try:
                download(asset_url, zip_path)
            except Exception as exc:
                print(f"Error: could not download {asset_name}: {exc}", file=sys.stderr)
                return 1

            print("Verifying checksum...")
            try:
                verify_zip_checksum(zip_path, asset_name, sha_text)
            except UpdateError as exc:
                print(f"Error: {exc}. Nothing was applied.", file=sys.stderr)
                return 1

            print("Staging new release tree...")
            extracted = unpack_root(zip_path, staging_dir / "extract")

        new_manifest = load_manifest(extracted / MANIFEST_NAME)
        old_manifest = load_manifest(root / MANIFEST_NAME)

        print("Applying files (preserving .env, .venv, memory, tools/auto, logs, and local state)...")
        try:
            copied, already, skipped, conflicts = apply_files(
                root, extracted, new_manifest, old_manifest, journal
            )
            deleted, removed_conflicts = remove_stale_files(
                root, old_manifest, new_manifest, journal
            )
        except Exception as exc:
            print(f"Error: update interrupted while applying files: {exc}", file=sys.stderr)
            print("No release tag was recorded. Re-run the updater to resume.", file=sys.stderr)
            return 1

        all_conflicts = list(conflicts) + list(removed_conflicts)
        if all_conflicts:
            print(
                f"Detected {len(all_conflicts)} conflict(s); {tag} was NOT recorded as "
                "installed:"
            )
            for rel_str, conflict_path in all_conflicts:
                print(f"  CONFLICT: {rel_str} -> new version saved to {conflict_path}")
            print("Resolve the conflicts (compare with the saved copies), then re-run the updater.")
            return 1

        manifest_src = extracted / MANIFEST_NAME
        if manifest_src.exists():
            shutil.copy2(manifest_src, root / MANIFEST_NAME)

        (root / ".release_info.json").write_text(
            json.dumps({"tag_name": tag, "asset": asset_name}, indent=2) + "\n",
            encoding="utf-8",
        )
        journal["status"] = "complete"
        save_journal(root, journal)
        clear_journal(root)

        print(f"Updated {copied} file(s) from {tag} ({already} already up to date).")
        print(f"Preserved/skipped {skipped} local state path(s).")
        if deleted:
            print(f"Removed {len(deleted)} file(s) no longer part of the release.")
        print("Run quickstart.bat again to resume/start the companion.")
        return 0
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="update even when already on latest tag")
    args = parser.parse_args(argv)
    return run_update(repo_root(), force=args.force)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
