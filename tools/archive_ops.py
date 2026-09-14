import zipfile
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path
from .security import require_unlocked

LIST_LIMIT = 500


def zip_list(path: str) -> str:
    """List the contents of a .zip without extracting.

    Args:
        path: .zip file path.
    """
    try:
        p = _validate_path(path)
        if not p.is_file():
            return f"[zip_list error: not a file: {p}]"
        with zipfile.ZipFile(p) as zf:
            infos = zf.infolist()
            lines = [f"path: {p}", f"entries: {len(infos)}", ""]
            for i, info in enumerate(infos):
                if i >= LIST_LIMIT:
                    lines.append(f"... truncated at {LIST_LIMIT} entries")
                    break
                kind = "DIR " if info.is_dir() else "FILE"
                lines.append(f"  {kind} {info.file_size:>10,}  {info.filename}")
            total_uncompressed = sum(i.file_size for i in infos)
            lines.append("")
            lines.append(f"total uncompressed: {total_uncompressed:,} bytes")
        return "\n".join(lines)
    except zipfile.BadZipFile as e:
        return f"[zip_list error: not a valid zip: {e}]"
    except Exception as e:
        return f"[zip_list error: {type(e).__name__}: {e}]"


def zip_extract(
    path: str,
    destination: str,
    members: Optional[list[str]] = None,
) -> str:
    """Extract a .zip into a destination directory (created if missing).

    Args:
        path: .zip file path.
        destination: Output directory under the allowed base.
        members: Optional list of specific archive member names to extract.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        src = _validate_path(path)
        dst = _validate_path(destination)
        dst.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as zf:
            # Reject zip-slip: ensure every target stays under dst
            target_members = members or zf.namelist()
            for name in target_members:
                target = (dst / name).resolve()
                try:
                    target.relative_to(dst.resolve())
                except ValueError:
                    return f"[zip_extract aborted: unsafe member path: {name!r}]"
            zf.extractall(path=str(dst), members=target_members)
            count = len(target_members)
        return f"Extracted {count} member(s) to {dst}"
    except zipfile.BadZipFile as e:
        return f"[zip_extract error: not a valid zip: {e}]"
    except Exception as e:
        return f"[zip_extract error: {type(e).__name__}: {e}]"


def zip_create(
    archive_path: str,
    sources: list[str],
    compression: str = "deflated",
) -> str:
    """Create a .zip from one or more source files or directories.

    Args:
        archive_path: Output .zip path under the allowed base.
        sources: Files or directories to include.
        compression: 'deflated' (default) or 'stored' (no compression).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        out = _validate_path(archive_path)
        if out.suffix.lower() != ".zip":
            return "[zip_create error: archive_path must end with .zip]"
        out.parent.mkdir(parents=True, exist_ok=True)
        mode = {"deflated": zipfile.ZIP_DEFLATED, "stored": zipfile.ZIP_STORED}.get(compression)
        if mode is None:
            return "[zip_create error: compression must be 'deflated' or 'stored']"
        added = 0
        with zipfile.ZipFile(out, "w", compression=mode) as zf:
            for src in sources:
                p = _validate_path(src)
                if p.is_file():
                    zf.write(p, arcname=p.name)
                    added += 1
                elif p.is_dir():
                    base = p
                    for child in p.rglob("*"):
                        if not child.is_file():
                            continue
                        # NEVER WRITE THE ARCHIVE INTO ITSELF. When archive_path lies inside a
                        # source directory -- which is what a caller does when it keeps its
                        # backups next to what it is backing up -- rglob hands back the output
                        # file, and zipping a file that is growing because you are zipping it
                        # does not terminate: the read chases the write until the disk runs out,
                        # and what is left is an archive with no central directory, so not one
                        # byte of it can be extracted.
                        #
                        # MEASURED, 2026-09-14. Six snapshots of one ~150MB business folder,
                        # taken into a subdirectory of that same folder, occupied 9.51GB. Four
                        # of them (0.18, 1.46, 2.42 and 5.43GB) had no end-of-central-directory
                        # record at all, and each of the three largest was found to contain an
                        # earlier snapshot nested inside it. The workers that made them reported
                        # a successful backup and went on to edit the originals -- the whole
                        # value of a backup is that it is there when it is needed, and these
                        # would not have been.
                        try:
                            if child.resolve() == out.resolve():
                                continue
                        except OSError:
                            pass
                        zf.write(child, arcname=str(child.relative_to(base.parent)))
                        added += 1
                else:
                    return f"[zip_create error: source missing: {p}]"
        return f"Wrote {out} ({added} file(s), {out.stat().st_size:,} bytes)"
    except Exception as e:
        return f"[zip_create error: {type(e).__name__}: {e}]"
