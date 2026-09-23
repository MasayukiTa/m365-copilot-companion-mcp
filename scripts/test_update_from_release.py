"""Tests for scripts/update_from_release.py.

All network calls (fetch_release_data / download / download_text) are stubbed
with a small in-memory "fake GitHub release" built from a real ZIP so the
checksum/manifest logic runs for real. No network access happens.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import update_from_release as ufr


PACKAGE = "M365-Companion-v9.9.9"
TAG = "v9.9.9"
OLD_TAG = "v9.9.8"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_release_zip(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, dict]:
    """Build a fake release ZIP (like package_release.ps1 produces) and return
    (zip_path, files_manifest) where files_manifest maps rel-posix-path -> sha256.
    """
    zip_path = tmp_path / f"{PACKAGE}.zip"
    manifest_files = {}
    with zipfile.ZipFile(zip_path, "w") as zf:
        for rel, content in files.items():
            zf.writestr(f"{PACKAGE}/{rel}", content)
            manifest_files[rel] = _sha(content)
        info = json.dumps({"tag_name": TAG, "commit": "deadbeef", "package": f"{PACKAGE}.zip"})
        zf.writestr(f"{PACKAGE}/.release_info.json", info)
        manifest = json.dumps({"tag_name": TAG, "commit": "deadbeef", "files": manifest_files})
        zf.writestr(f"{PACKAGE}/.release_manifest.json", manifest)
    return zip_path, manifest_files


def fake_release_data(zip_name: str, include_sha_asset: bool = True) -> dict:
    assets = [{"name": zip_name, "browser_download_url": "https://example.invalid/zip"}]
    if include_sha_asset:
        assets.append(
            {"name": ufr.SHA256SUMS_NAME, "browser_download_url": "https://example.invalid/sha"}
        )
    return {"tag_name": TAG, "assets": assets}


def install_old_root(root: Path, files: dict[str, bytes], tag: str = OLD_TAG) -> dict:
    """Set up `root` as an existing install at `tag` with the given file
    contents on disk, and a matching .release_manifest.json / .release_info.json.
    Returns the old manifest dict.
    """
    root.mkdir(parents=True, exist_ok=True)
    old_manifest = {}
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        old_manifest[rel] = _sha(content)
    (root / ufr.MANIFEST_NAME).write_text(
        json.dumps({"tag_name": tag, "commit": "old", "files": old_manifest}), encoding="utf-8"
    )
    (root / ".release_info.json").write_text(
        json.dumps({"tag_name": tag, "asset": f"M365-Companion-{tag}.zip"}), encoding="utf-8"
    )
    return old_manifest


def patch_network(monkeypatch, zip_path: Path, sha_text: str, release_data: dict) -> None:
    monkeypatch.setattr(ufr, "fetch_release_data", lambda: release_data)
    monkeypatch.setattr(ufr, "download_text", lambda url: sha_text)

    def fake_download(url, dest):
        dest.write_bytes(zip_path.read_bytes())

    monkeypatch.setattr(ufr, "download", fake_download)


def sums_text_for(zip_path: Path, zip_name: str, digest: str | None = None) -> str:
    if digest is None:
        digest = _sha(zip_path.read_bytes())
    return f"{digest}  {zip_name}\n"


# ---------------------------------------------------------------------------
# UPD-11: checksum must be downloaded and verified
# ---------------------------------------------------------------------------


def test_missing_sha256sums_asset_refuses_and_leaves_tree_untouched(tmp_path, monkeypatch):
    root = tmp_path / "install"
    install_old_root(root, {"main.py": b"old main\n"})
    before = (root / "main.py").read_bytes()

    zip_path, _ = build_release_zip(tmp_path, {"main.py": b"new main\n"})
    release_data = fake_release_data(f"{PACKAGE}.zip", include_sha_asset=False)
    patch_network(monkeypatch, zip_path, sums_text_for(zip_path, f"{PACKAGE}.zip"), release_data)

    rc = ufr.run_update(root, force=False)

    assert rc != 0
    assert (root / "main.py").read_bytes() == before
    assert ufr.local_tag(root) == OLD_TAG
    assert not (root / ufr.JOURNAL_NAME).exists() or json.loads(
        (root / ufr.JOURNAL_NAME).read_text()
    ).get("status") != "complete"


def test_checksum_mismatch_refuses_and_leaves_tree_untouched(tmp_path, monkeypatch):
    root = tmp_path / "install"
    install_old_root(root, {"main.py": b"old main\n"})
    before = (root / "main.py").read_bytes()

    zip_path, _ = build_release_zip(tmp_path, {"main.py": b"new main\n"})
    release_data = fake_release_data(f"{PACKAGE}.zip")
    bad_sha_text = sums_text_for(zip_path, f"{PACKAGE}.zip", digest="0" * 64)
    patch_network(monkeypatch, zip_path, bad_sha_text, release_data)

    rc = ufr.run_update(root, force=False)

    assert rc != 0
    assert (root / "main.py").read_bytes() == before
    assert ufr.local_tag(root) == OLD_TAG
    assert not (root / ufr.CONFLICT_DIR).exists()


# ---------------------------------------------------------------------------
# RES-12: interrupted apply resumes instead of raising false conflicts;
# the tag/success is only recorded once everything is applied.
# ---------------------------------------------------------------------------


def test_interrupted_copy_then_rerun_completes_without_false_conflicts(tmp_path, monkeypatch):
    root = tmp_path / "install"
    install_old_root(
        root,
        {"main.py": b"old main\n", "a.txt": b"a old\n", "b.txt": b"b old\n", "c.txt": b"c old\n"},
    )

    zip_path, _ = build_release_zip(
        tmp_path,
        {"main.py": b"new main\n", "a.txt": b"a new\n", "b.txt": b"b new\n", "c.txt": b"c new\n"},
    )
    release_data = fake_release_data(f"{PACKAGE}.zip")
    sha_text = sums_text_for(zip_path, f"{PACKAGE}.zip")
    patch_network(monkeypatch, zip_path, sha_text, release_data)

    real_copy2 = ufr.shutil.copy2
    calls = {"n": 0}

    def flaky_copy2(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("simulated crash mid-copy")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(ufr.shutil, "copy2", flaky_copy2)

    rc1 = ufr.run_update(root, force=False)
    assert rc1 != 0
    assert ufr.local_tag(root) == OLD_TAG  # tag NOT advanced on the interrupted run

    journal = json.loads((root / ufr.JOURNAL_NAME).read_text())
    assert journal["status"] == "in_progress"
    assert journal["tag_name"] == TAG
    partially_applied = set(journal["applied"])
    assert 0 < len(partially_applied) < 4

    # Restore real copy2 and rerun: should resume and finish cleanly.
    monkeypatch.setattr(ufr.shutil, "copy2", real_copy2)
    rc2 = ufr.run_update(root, force=False)

    assert rc2 == 0
    assert ufr.local_tag(root) == TAG
    assert (root / "main.py").read_bytes() == b"new main\n"
    assert (root / "a.txt").read_bytes() == b"a new\n"
    assert (root / "b.txt").read_bytes() == b"b new\n"
    assert (root / "c.txt").read_bytes() == b"c new\n"
    assert not (root / ufr.CONFLICT_DIR).exists()
    assert not (root / ufr.JOURNAL_NAME).exists()


# ---------------------------------------------------------------------------
# A genuine local edit must become a conflict, not be clobbered.
# ---------------------------------------------------------------------------


def test_local_edit_becomes_conflict_and_tag_is_not_advanced(tmp_path, monkeypatch):
    root = tmp_path / "install"
    install_old_root(root, {"main.py": b"old main\n", "config.py": b"old config\n"})
    # User hand-edited config.py; it no longer matches the old release's hash.
    (root / "config.py").write_bytes(b"USER EDITED config\n")

    zip_path, _ = build_release_zip(
        tmp_path, {"main.py": b"new main\n", "config.py": b"new config\n"}
    )
    release_data = fake_release_data(f"{PACKAGE}.zip")
    sha_text = sums_text_for(zip_path, f"{PACKAGE}.zip")
    patch_network(monkeypatch, zip_path, sha_text, release_data)

    rc = ufr.run_update(root, force=False)

    assert rc != 0
    assert ufr.local_tag(root) == OLD_TAG
    # main.py had no local edits: it should have been updated.
    assert (root / "main.py").read_bytes() == b"new main\n"
    # config.py must be untouched, and the new version saved to conflicts.
    assert (root / "config.py").read_bytes() == b"USER EDITED config\n"
    conflict_file = root / ufr.CONFLICT_DIR / "config.py"
    assert conflict_file.exists()
    assert conflict_file.read_bytes() == b"new config\n"


# ---------------------------------------------------------------------------
# UPD-13: files removed upstream
# ---------------------------------------------------------------------------


def test_removed_upstream_file_deleted_when_unmodified_moved_when_modified(tmp_path, monkeypatch):
    root = tmp_path / "install"
    install_old_root(
        root,
        {
            "main.py": b"old main\n",
            "gone_unmodified.txt": b"stale content\n",
            "gone_modified.txt": b"stale content 2\n",
        },
    )
    # Locally modify one of the files that the new release removes.
    (root / "gone_modified.txt").write_bytes(b"USER EDITED stale content 2\n")

    # New release drops both "gone_*.txt" files.
    zip_path, _ = build_release_zip(tmp_path, {"main.py": b"new main\n"})
    release_data = fake_release_data(f"{PACKAGE}.zip")
    sha_text = sums_text_for(zip_path, f"{PACKAGE}.zip")
    patch_network(monkeypatch, zip_path, sha_text, release_data)

    rc = ufr.run_update(root, force=False)

    # gone_modified.txt is a real conflict -> overall run reports failure and
    # does not advance the tag, per the "never succeed with conflicts" rule.
    assert rc != 0
    assert ufr.local_tag(root) == OLD_TAG

    assert not (root / "gone_unmodified.txt").exists()
    assert not (root / "gone_modified.txt").exists()
    moved = root / ufr.CONFLICT_DIR / "gone_modified.txt"
    assert moved.exists()
    assert moved.read_bytes() == b"USER EDITED stale content 2\n"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
