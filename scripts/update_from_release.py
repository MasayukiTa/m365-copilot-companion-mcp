"""Update a ZIP install from the latest GitHub Release asset.

This is for users who installed from M365-Companion-*.zip and do not have git.
It downloads the latest release ZIP, overlays the committed application files,
and deliberately preserves local runtime state such as .env, .venv, logs, and
setup progress. It is not a binary diff; it is a safe release snapshot refresh.

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


OWNER = "MasayukiTa"
REPO = "m365-copilot-companion-mcp"
API_LATEST = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
ASSET_PREFIX = "M365-Companion-"
ASSET_SUFFIX = ".zip"

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


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_json_url(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "m365-companion-updater"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "m365-companion-updater"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


def latest_release_asset() -> tuple[str, str, str]:
    data = read_json_url(API_LATEST)
    tag = str(data.get("tag_name") or "").strip()
    assets = data.get("assets") or []
    candidates = [
        a for a in assets
        if str(a.get("name") or "").startswith(ASSET_PREFIX)
        and str(a.get("name") or "").endswith(ASSET_SUFFIX)
    ]
    if not candidates:
        raise RuntimeError("latest release has no M365-Companion-*.zip asset")
    asset = sorted(candidates, key=lambda a: str(a.get("name") or ""))[-1]
    return tag, str(asset["name"]), str(asset["browser_download_url"])


def local_tag(root: Path) -> str:
    info = root / ".release_info.json"
    if not info.exists():
        return ""
    try:
        return str(json.loads(info.read_text(encoding="utf-8")).get("tag_name") or "")
    except Exception:
        return ""


def rel_key(path: Path) -> str:
    return path.as_posix()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(root: Path) -> dict:
    path = root / MANIFEST_NAME
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


def conflict_copy(src: Path, rel: Path, conflict_root: Path) -> None:
    conflict = conflict_root / rel
    conflict.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, conflict)


def overlay_tree(src_root: Path, dst_root: Path) -> tuple[int, int, int]:
    old_manifest = load_manifest(dst_root)
    backup_root = dst_root / ".update_backups"
    conflict_root = dst_root / CONFLICT_DIR
    copied = 0
    skipped = 0
    conflicts = 0
    for path in src_root.rglob("*"):
        rel = path.relative_to(src_root)
        if should_skip(rel):
            skipped += 1
            continue
        if rel.name == MANIFEST_NAME:
            continue
        dest = dst_root / rel
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        key = rel_key(rel)
        if dest.exists() and key in old_manifest and sha256(dest) != old_manifest[key]:
            conflict_copy(path, rel, conflict_root)
            conflicts += 1
            continue
        try:
            backup_existing(dest, rel, backup_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            copied += 1
        except OSError:
            conflict_copy(path, rel, conflict_root)
            conflicts += 1
    manifest_src = src_root / MANIFEST_NAME
    if manifest_src.exists():
        shutil.copy2(manifest_src, dst_root / MANIFEST_NAME)
    return copied, skipped, conflicts


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest_resolved = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if target != dest_resolved and dest_resolved not in target.parents:
            raise RuntimeError(f"unsafe ZIP member path: {member.filename!r}")
    zf.extractall(dest)


def unpack_root(zip_path: Path, work: Path) -> Path:
    with zipfile.ZipFile(zip_path) as zf:
        safe_extract(zf, work)
    children = [p for p in work.iterdir() if p.is_dir()]
    if len(children) == 1:
        return children[0]
    return work


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="update even when already on latest tag")
    args = parser.parse_args(argv)

    root = repo_root()
    print("Checking latest GitHub Release...")
    tag, asset_name, url = latest_release_asset()
    current = local_tag(root)
    if current == tag and not args.force:
        print(f"Already on latest release: {tag}")
        return 0

    print(f"Latest:  {tag} ({asset_name})")
    if current:
        print(f"Current: {current}")
    else:
        print("Current: unknown ZIP snapshot")

    with tempfile.TemporaryDirectory(prefix="m365-companion-update-") as td:
        tmp = Path(td)
        zip_path = tmp / asset_name
        print("Downloading release ZIP...")
        download(url, zip_path)
        print("Extracting...")
        extracted = unpack_root(zip_path, tmp / "extract")
        print("Applying files (preserving .env, .venv, memory, tools/auto, logs, and local state)...")
        copied, skipped, conflicts = overlay_tree(extracted, root)

    (root / ".release_info.json").write_text(
        json.dumps({"tag_name": tag, "asset": asset_name}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Updated {copied} files from {tag}.")
    print(f"Preserved/skipped {skipped} local state paths.")
    if conflicts:
        print(f"Detected {conflicts} locally modified file(s); new copies were written under {CONFLICT_DIR}.")
    print("Run quickstart.bat again to resume/start the companion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
