import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from .security import require_unlocked

ALLOWED_BASE = Path(os.environ.get("MCP_ALLOWED_BASE", "~")).expanduser().resolve()


def _validate_path(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    try:
        p.relative_to(ALLOWED_BASE)
    except ValueError:
        raise PermissionError(f"Path is outside the allowed base: {path}")
    return p


def read_file(
    path: str,
    encoding: str = "utf-8",
    start_line: int = 1,
    max_lines: Optional[int] = None,
) -> str:
    """Read a text file.

    Args:
        path: File path under the allowed base directory.
        encoding: Text encoding.
        start_line: 1-based line number to start reading from.
        max_lines: Optional maximum number of lines to return.
    """
    try:
        p = _validate_path(path)
        lines = p.read_text(encoding=encoding).splitlines()
        start = max(start_line - 1, 0)
        end = None if max_lines is None else start + max_lines
        selected = lines[start:end]
        return "\n".join(f"{idx}: {line}" for idx, line in enumerate(selected, start + 1))
    except Exception as e:
        return f"[read_file error: {type(e).__name__}: {e}]"


def write_file(path: str, content: str, encoding: str = "utf-8") -> str:
    """Write a text file, creating parent directories when needed."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        p = _validate_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return f"Wrote {p} ({len(content)} characters)"
    except Exception as e:
        return f"[write_file error: {type(e).__name__}: {e}]"


def append_file(path: str, content: str, encoding: str = "utf-8") -> str:
    """Append text to a file, creating parent directories when needed."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        p = _validate_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding=encoding) as f:
            f.write(content)
        return f"Appended {len(content)} characters to {p}"
    except Exception as e:
        return f"[append_file error: {type(e).__name__}: {e}]"


def list_directory(path: str = ".", recursive: bool = False, max_entries: int = 200) -> str:
    """List files and directories.

    Args:
        path: Directory path under the allowed base.
        recursive: Recurse into subdirectories when true.
        max_entries: Maximum entries to return.
    """
    try:
        p = _validate_path(path)
        if not p.is_dir():
            return f"[list_directory error: not a directory: {p}]"
        entries = p.rglob("*") if recursive else p.iterdir()
        lines: list[str] = []
        for entry in sorted(entries, key=lambda x: str(x).lower()):
            rel = entry.relative_to(p)
            kind = "DIR" if entry.is_dir() else "FILE"
            size = "" if entry.is_dir() else f" ({entry.stat().st_size:,} bytes)"
            lines.append(f"[{kind}] {rel}{size}")
            if len(lines) >= max_entries:
                lines.append(f"... truncated at {max_entries} entries")
                break
        return "\n".join(lines) if lines else "(empty directory)"
    except Exception as e:
        return f"[list_directory error: {type(e).__name__}: {e}]"


def create_directory(path: str) -> str:
    """Create a directory and any missing parents."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        p = _validate_path(path)
        p.mkdir(parents=True, exist_ok=True)
        return f"Created directory {p}"
    except Exception as e:
        return f"[create_directory error: {type(e).__name__}: {e}]"


def copy_path(source: str, destination: str, overwrite: bool = False) -> str:
    """Copy a file or directory within the allowed base."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        src = _validate_path(source)
        dst = _validate_path(destination)
        if dst.exists() and not overwrite:
            return f"[copy skipped: destination exists: {dst}]"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        return f"Copied {src} to {dst}"
    except Exception as e:
        return f"[copy_path error: {type(e).__name__}: {e}]"


def delete_path(path: str, recursive: bool = False) -> str:
    """Delete a file or an explicitly recursive directory under the allowed base."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        p = _validate_path(path)
        if p == ALLOWED_BASE:
            return "[delete skipped: refusing to delete the allowed base directory]"
        if not p.exists():
            return f"[delete skipped: path does not exist: {p}]"
        if p.is_dir():
            if not recursive:
                return "[delete skipped: directory deletion requires recursive=True]"
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"Deleted {p}"
    except Exception as e:
        return f"[delete_path error: {type(e).__name__}: {e}]"


def move_path(source: str, destination: str, overwrite: bool = False) -> str:
    """Move (or rename) a file or directory within the allowed base.

    Args:
        source: Existing path.
        destination: Target path. If it exists and overwrite=False, the move is skipped.
        overwrite: Replace the destination if it already exists.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        src = _validate_path(source)
        dst = _validate_path(destination)
        if not src.exists():
            return f"[move skipped: source missing: {src}]"
        if src == ALLOWED_BASE:
            return "[move skipped: refusing to move the allowed base directory]"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if not overwrite:
                return f"[move skipped: destination exists: {dst}]"
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.move(str(src), str(dst))
        return f"Moved {src} -> {dst}"
    except Exception as e:
        return f"[move_path error: {type(e).__name__}: {e}]"


def trash_path(path: str) -> str:
    """Send a file or directory to the OS recycle bin (recoverable, unlike delete_path).

    Uses send2trash. On Windows the item goes to the Recycle Bin and can be
    restored from there if the operation was a mistake.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        from send2trash import send2trash

        p = _validate_path(path)
        if not p.exists():
            return f"[trash skipped: path missing: {p}]"
        if p == ALLOWED_BASE:
            return "[trash skipped: refusing to trash the allowed base directory]"
        send2trash(str(p))
        return f"Sent to recycle bin: {p}"
    except Exception as e:
        return f"[trash_path error: {type(e).__name__}: {e}]"


def hash_file(path: str, algorithm: str = "sha256") -> str:
    """Compute a hex digest of a file's contents.

    Args:
        path: File to hash.
        algorithm: 'sha256' (default), 'sha1', or 'md5'.
    """
    try:
        p = _validate_path(path)
        if not p.is_file():
            return f"[hash_file error: not a file: {p}]"
        alg = algorithm.lower()
        if alg not in {"sha256", "sha1", "md5"}:
            return "[hash_file error: algorithm must be sha256, sha1, or md5]"
        h = hashlib.new(alg)
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return f"{alg}: {h.hexdigest()}  {p}  ({p.stat().st_size:,} bytes)"
    except Exception as e:
        return f"[hash_file error: {type(e).__name__}: {e}]"


def find_duplicates(
    path: str = ".",
    min_size: int = 1024,
    max_files: int = 20_000,
) -> str:
    """Find duplicate files under a directory by size + sha256 of full content.

    Two-pass: groups files by size first to avoid hashing unique sizes.

    Args:
        path: Directory to scan recursively.
        min_size: Skip files smaller than this many bytes (avoids tiny dotfiles).
        max_files: Scan limit to keep runtime bounded.
    """
    try:
        base = _validate_path(path)
        if not base.is_dir():
            return f"[find_duplicates error: not a directory: {base}]"
        sized: dict[int, list[Path]] = {}
        count = 0
        for child in base.rglob("*"):
            if not child.is_file():
                continue
            try:
                size = child.stat().st_size
            except OSError:
                continue
            if size < min_size:
                continue
            sized.setdefault(size, []).append(child)
            count += 1
            if count >= max_files:
                break
        groups: list[list[Path]] = []
        for size, files in sized.items():
            if len(files) < 2:
                continue
            by_hash: dict[str, list[Path]] = {}
            for f in files:
                try:
                    h = hashlib.sha256()
                    with f.open("rb") as fp:
                        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                            h.update(chunk)
                    by_hash.setdefault(h.hexdigest(), []).append(f)
                except OSError:
                    continue
            for dups in by_hash.values():
                if len(dups) >= 2:
                    groups.append(dups)
        if not groups:
            return f"(no duplicates among {count} files under {base})"
        groups.sort(key=lambda g: -g[0].stat().st_size)
        lines = [f"scanned: {count} files", f"duplicate groups: {len(groups)}", ""]
        for i, group in enumerate(groups, 1):
            size = group[0].stat().st_size
            lines.append(f"[#{i}] {size:,} bytes × {len(group)} copies:")
            for f in group:
                lines.append(f"    {f}")
        return "\n".join(lines)
    except Exception as e:
        return f"[find_duplicates error: {type(e).__name__}: {e}]"


def dir_size(path: str = ".", top_n: int = 15) -> str:
    """Return total size of a directory and the largest top-N immediate children.

    Use this to figure out where disk space is going inside a folder.
    """
    try:
        base = _validate_path(path)
        if not base.is_dir():
            return f"[dir_size error: not a directory: {base}]"

        def total(p: Path) -> int:
            if p.is_file():
                try:
                    return p.stat().st_size
                except OSError:
                    return 0
            n = 0
            try:
                for child in p.iterdir():
                    n += total(child)
            except OSError:
                pass
            return n

        children = []
        for child in base.iterdir():
            children.append((total(child), child))
        children.sort(reverse=True)
        grand = sum(s for s, _ in children)
        lines = [f"total: {grand:,} bytes ({grand / (1024**3):.2f} GiB) under {base}", ""]
        for size, child in children[:top_n]:
            kind = "DIR " if child.is_dir() else "FILE"
            pct = (size / grand * 100) if grand else 0
            lines.append(f"  {pct:5.1f}%  {size:>14,}  {kind} {child.name}")
        if len(children) > top_n:
            lines.append(f"  ... {len(children) - top_n} more child(ren)")
        return "\n".join(lines)
    except Exception as e:
        return f"[dir_size error: {type(e).__name__}: {e}]"


def file_metadata(path: str) -> str:
    """Return filesystem metadata for a file: size, timestamps, attributes."""
    try:
        p = _validate_path(path)
        if not p.exists():
            return f"[file_metadata error: not found: {p}]"
        st = p.stat()
        lines = [
            f"path: {p}",
            f"type: {'directory' if p.is_dir() else 'file'}",
            f"size: {st.st_size:,} bytes",
            f"created: {datetime.fromtimestamp(st.st_ctime).isoformat(timespec='seconds')}",
            f"modified: {datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')}",
            f"accessed: {datetime.fromtimestamp(st.st_atime).isoformat(timespec='seconds')}",
        ]
        if hasattr(st, "st_file_attributes"):
            lines.append(f"win_attributes: 0x{st.st_file_attributes:08x}")
        return "\n".join(lines)
    except Exception as e:
        return f"[file_metadata error: {type(e).__name__}: {e}]"
