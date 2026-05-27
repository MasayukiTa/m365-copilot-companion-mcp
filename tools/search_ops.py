import fnmatch
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path


def glob(
    pattern: str,
    path: str = ".",
    max_results: int = 200,
    include_hidden: bool = False,
) -> str:
    """Find files matching a glob pattern, sorted by most recently modified.

    Args:
        pattern: Glob pattern. Supports ** for recursive descent, for example "**/*.py"
            or "src/**/*.{ts,tsx}". Brace expansion is supported manually here.
        path: Base directory to search under. Must be inside the allowed base.
        max_results: Maximum number of matches to return.
        include_hidden: When false, skip dotfiles and dot-directories.
    """
    try:
        base = _validate_path(path)
        if not base.is_dir():
            return f"[glob error: not a directory: {base}]"

        patterns = _expand_braces(pattern)
        seen: set[Path] = set()
        for pat in patterns:
            for match in base.glob(pat):
                if not match.is_file():
                    continue
                if not include_hidden and _is_hidden(match, base):
                    continue
                seen.add(match)

        results = sorted(seen, key=lambda p: p.stat().st_mtime, reverse=True)
        truncated = len(results) > max_results
        results = results[:max_results]
        if not results:
            return "(no matches)"
        lines = [str(p) for p in results]
        if truncated:
            lines.append(f"... truncated at {max_results} entries")
        return "\n".join(lines)
    except Exception as e:
        return f"[glob error: {type(e).__name__}: {e}]"


def _is_hidden(p: Path, base: Path) -> bool:
    try:
        rel = p.relative_to(base)
    except ValueError:
        rel = p
    return any(part.startswith(".") for part in rel.parts)


def _expand_braces(pattern: str) -> list[str]:
    """Minimal brace expansion: a/{b,c}/d -> [a/b/d, a/c/d]. Single level only."""
    start = pattern.find("{")
    if start == -1:
        return [pattern]
    end = pattern.find("}", start)
    if end == -1:
        return [pattern]
    prefix = pattern[:start]
    suffix = pattern[end + 1 :]
    options = pattern[start + 1 : end].split(",")
    expanded: list[str] = []
    for opt in options:
        expanded.extend(_expand_braces(prefix + opt.strip() + suffix))
    return expanded


def find_files(
    name_contains: str,
    path: str = ".",
    max_results: int = 200,
) -> str:
    """Find files whose name contains a substring. Case-insensitive.

    Useful when you want a name-based search without writing a glob.

    Args:
        name_contains: Substring to look for in filenames.
        path: Base directory.
        max_results: Maximum matches.
    """
    try:
        base = _validate_path(path)
        if not base.is_dir():
            return f"[find_files error: not a directory: {base}]"
        needle = name_contains.lower()
        matches = [
            p
            for p in base.rglob("*")
            if p.is_file() and needle in p.name.lower()
        ]
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            return "(no matches)"
        truncated = len(matches) > max_results
        matches = matches[:max_results]
        out = [str(m) for m in matches]
        if truncated:
            out.append(f"... truncated at {max_results} entries")
        return "\n".join(out)
    except Exception as e:
        return f"[find_files error: {type(e).__name__}: {e}]"
