"""What the cache prune is allowed to delete, checked without a browser in the picture.

This exists because the first live run of the prune could not answer its own question. Launched
through start_companion_edge.ps1, the seeded `Default\\Local Storage` was gone afterwards -- and
there was no way to tell whether the prune had taken it or whether Edge, which rewrites its
profile on startup, had discarded a file that was 8192 zero bytes rather than a valid leveldb.
"Probably the browser did it" is not something to ship a recursive delete on.

Here the script runs against a directory and nothing else runs at all, so what survives is
attributable to exactly one thing.
"""
import os
import subprocess

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "win", "prune_edge_cache.ps1")

#: Every one of these is a real directory from a live managed profile, with the bytes it was
#: measured holding. GrShaderCache is here because the Python-side name list missed it for the
#: whole life of the project and the trim silently freed nothing.
CACHE = ["Default/Cache/Cache_Data", "Default/Code Cache/js", "GrShaderCache",
         "Default/GPUCache", "extensions_crx_cache"]

#: The sign-in and the settings. Losing any of these costs a manual Entra sign-in, which is
#: the one outcome that would make this whole change a net loss.
KEEP = ["Default/Cookies", "Default/Login Data", "Default/Preferences", "Local State",
        "Default/Local Storage/leveldb/000003.log", "Default/Network/Cookies"]


def _run(profile, cap=2, dry=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT,
            "-ProfileDir", str(profile), "-CapMB", str(cap)]
    if dry:
        args.append("-DryRun")
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def _seed(tmp_path, cache_bytes=6_000_000):
    root = tmp_path / "copilot-test-edge"
    for rel in CACHE:
        p = root / rel
        p.mkdir(parents=True, exist_ok=True)
        (p / "blob.bin").write_bytes(b"\0" * cache_bytes)
    for rel in KEEP:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"keep" * 64)
    return root


@pytest.fixture
def seeded(tmp_path):
    return _seed(tmp_path)


def test_the_sign_in_survives_the_prune(seeded):
    _run(seeded)
    missing = [rel for rel in KEEP if not (seeded / rel).exists()]
    assert missing == [], "the prune deleted files that hold the sign-in: %s" % missing


def test_the_caches_are_actually_gone(seeded):
    _run(seeded)
    left = [rel for rel in CACHE if (seeded / rel / "blob.bin").exists()]
    assert left == [], "cache directories survived the prune: %s" % left


def test_the_shader_cache_is_one_of_them(seeded):
    # Named on its own because this is the directory the original list misspelled, so a
    # regression here would show up as "the prune works" with 12 MB per profile left behind.
    _run(seeded)
    assert not (seeded / "GrShaderCache" / "blob.bin").exists()


def test_a_profile_under_the_cap_is_not_touched(tmp_path):
    root = _seed(tmp_path, cache_bytes=1000)
    out = _run(root, cap=50)
    assert "under cap" in out
    assert (root / "Default/Cache/Cache_Data/blob.bin").exists()


def test_a_dry_run_deletes_nothing(seeded):
    out = _run(seeded, dry=True)
    assert "would prune" in out
    assert (seeded / "Default/Cache/Cache_Data/blob.bin").exists()


def test_a_directory_that_merely_sits_under_a_cache_shaped_path_is_safe(tmp_path):
    # By its OWN name, not a path substring. A profile whose parent folder is called
    # "cache" must not be deleted wholesale -- that is the failure mode that takes the
    # cookies with it.
    root = tmp_path / "my cache" / "copilot-test-edge"
    (root / "Default").mkdir(parents=True)
    (root / "Default" / "Cookies").write_bytes(b"keep" * 64)
    (root / "Default" / "Cache").mkdir()
    (root / "Default" / "Cache" / "blob.bin").write_bytes(b"\0" * 6_000_000)
    _run(root)
    assert (root / "Default" / "Cookies").exists()
    assert not (root / "Default" / "Cache" / "blob.bin").exists()


def test_a_missing_profile_is_not_an_error(tmp_path):
    out = _run(tmp_path / "nope")
    assert "absent" in out
