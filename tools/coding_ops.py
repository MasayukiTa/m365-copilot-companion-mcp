import os
import py_compile
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path
from .security import require_unlocked

#: Largest file the pure-Python grep fallback will open. Not a policy about what is worth
#: searching -- a bound on what one tool call can cost the server, which is shared and
#: long-lived. Files above it are reported, never silently dropped. Raise it with
#: MCP_GREP_MAX_FILE_MB when a genuinely large file has to be searched; installing ripgrep
#: removes the fallback (and this bound) altogether.
_GREP_MAX_FILE_BYTES = int(float(os.environ.get("MCP_GREP_MAX_FILE_MB", "8")) * 1024 * 1024)


def _run(args: list[str], cwd: Optional[Path], timeout: int) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else os.getcwd(),
        shell=False,
    )
    output = ""
    if result.stdout:
        output += f"[stdout]\n{result.stdout}"
    if result.stderr:
        output += f"[stderr]\n{result.stderr}"
    if result.returncode != 0:
        output += f"\n[returncode: {result.returncode}]"
    return output or "(no output)"


def grep(
    pattern: str,
    path: str = ".",
    glob: Optional[str] = None,
    max_matches: int = 100,
    case_sensitive: bool = False,
) -> str:
    """Search text files under a directory using ripgrep when available.

    Args:
        pattern: Text or regex pattern to search for.
        path: File or directory to search. Must be under the allowed user directory.
        glob: Optional file glob, for example '*.py' or '*.ts'.
        max_matches: Maximum matching lines to return.
        case_sensitive: Use case-sensitive matching when true.
    """
    try:
        target = _validate_path(path)
        if shutil.which("rg"):
            args = ["rg", "--line-number", "--no-heading", "--color", "never"]
            if not case_sensitive:
                args.append("--ignore-case")
            if glob:
                args.extend(["--glob", glob])
            args.extend(["--max-count", str(max_matches), pattern, str(target)])
            return _run(args, None, 30)

        # STREAMED, AND BOUNDED BY FILE SIZE. This fallback used to do
        # `read_text().splitlines()`, which holds the WHOLE file as one str and then a list of
        # every line on top of it -- roughly five times the file on disk, per file, per thread.
        #
        # Measured 2026-08-25: `rg` is not on PATH on this machine, so every call lands here;
        # the repository carries a 48 MB faulthandler.log plus ~40 MB of other large files; and
        # the fleet calls this tool from several AnyIO worker threads at once. The MCP server
        # grew from 222 MB to 2.4 GB in five minutes, then to over 5 GB, until free RAM fell
        # under the fleet's own recycle floor and a run hard-reset the shared browser out from
        # under its sibling. py-spy on the live process is what named this line.
        matches: list[str] = []
        files = ([target] if target.is_file()
                 else (p for p in target.rglob("*") if p.is_file()))
        needle = pattern if case_sensitive else pattern.lower()
        skipped_big = 0
        for file_path in files:
            if glob and not file_path.match(glob):
                continue
            try:
                if file_path.stat().st_size > _GREP_MAX_FILE_BYTES:
                    # NAMED, NOT DROPPED. A search that silently skipped the biggest files
                    # would read as "no matches" -- the one answer a grep must never fake.
                    skipped_big += 1
                    continue
                with open(file_path, encoding="utf-8", errors="strict") as fh:
                    for line_no, line in enumerate(fh, 1):
                        line = line.rstrip("\n").rstrip("\r")
                        hay = line if case_sensitive else line.lower()
                        if needle in hay:
                            matches.append(f"{file_path}:{line_no}:{line}")
                            if len(matches) >= max_matches:
                                return "\n".join(matches)
            except (UnicodeDecodeError, OSError):
                continue
        note = ("" if not skipped_big else
                "\n[%d file(s) larger than %d MB were not searched]"
                % (skipped_big, _GREP_MAX_FILE_BYTES // (1024 * 1024)))
        return ("\n".join(matches) + note) if matches else ("(no matches)" + note)
    except Exception as e:
        return f"[grep error: {type(e).__name__}: {e}]"


def replace_in_file(
    path: str,
    old: str,
    new: str,
    expected_replacements: Optional[int] = None,
    encoding: str = "utf-8",
) -> str:
    """Replace exact text in one file.

    Args:
        path: File path to edit.
        old: Exact text to replace.
        new: Replacement text.
        expected_replacements: Optional safety count. If the actual count differs, no write occurs.
        encoding: File encoding.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        p = _validate_path(path)
        original = p.read_text(encoding=encoding)
        count = original.count(old)
        if count == 0:
            return "[replace skipped: old text was not found]"
        if expected_replacements is not None and count != expected_replacements:
            return f"[replace skipped: expected {expected_replacements}, found {count}]"
        p.write_text(original.replace(old, new), encoding=encoding)
        return f"Replaced {count} occurrence(s) in {p}"
    except Exception as e:
        return f"[replace error: {type(e).__name__}: {e}]"


def python_check(path: str) -> str:
    """Compile-check one Python file without running it."""
    try:
        p = _validate_path(path)
        if not p.is_file():
            return f"[python_check error: file not found: {p}]"
        with tempfile.TemporaryDirectory() as tmp:
            py_compile.compile(str(p), cfile=str(Path(tmp) / "check.pyc"), doraise=True)
        return f"OK: {p}"
    except Exception as e:
        return f"[python_check error: {type(e).__name__}: {e}]"


def git_status(path: str = ".") -> str:
    """Return git status for a repository or subdirectory."""
    try:
        p = _validate_path(path)
        cwd = p if p.is_dir() else p.parent
        return _run(["git", "status", "--short", "--branch"], cwd, 30)
    except Exception as e:
        return f"[git_status error: {type(e).__name__}: {e}]"


def git_diff(
    path: str = ".",
    staged: bool = False,
    max_lines: int = 800,
) -> str:
    """Show the working-tree or staged diff for a repository or subdirectory.

    Args:
        path: Repository root or subdirectory.
        staged: True to show staged changes only, false for unstaged.
        max_lines: Truncate output to this many lines.
    """
    try:
        p = _validate_path(path)
        cwd = p if p.is_dir() else p.parent
        args = ["git", "diff", "--no-color"]
        if staged:
            args.append("--cached")
        out = _run(args, cwd, 45)
        lines = out.splitlines()
        if len(lines) > max_lines:
            head = "\n".join(lines[:max_lines])
            return f"{head}\n... truncated at {max_lines} lines (total {len(lines)})"
        return out
    except Exception as e:
        return f"[git_diff error: {type(e).__name__}: {e}]"


def git_log(path: str = ".", limit: int = 20) -> str:
    """Show recent git commits in a compact one-line format.

    Args:
        path: Repository root or subdirectory.
        limit: Number of commits to show.
    """
    try:
        p = _validate_path(path)
        cwd = p if p.is_dir() else p.parent
        return _run(
            [
                "git",
                "log",
                f"-n{limit}",
                "--no-color",
                "--pretty=format:%h  %ad  %an  %s",
                "--date=short",
            ],
            cwd,
            30,
        )
    except Exception as e:
        return f"[git_log error: {type(e).__name__}: {e}]"


def multi_edit(
    path: str,
    edits: list[dict],
    encoding: str = "utf-8",
) -> str:
    """Apply multiple exact-string edits to one file atomically.

    Each edit is a dict with keys: old (required), new (required),
    expected_replacements (optional int, defaults to 1). Edits are applied in
    order against the result of the previous edit. If any edit fails to match
    its expected count, no changes are written.

    Args:
        path: File to edit.
        edits: Ordered list of {old, new, expected_replacements} dicts.
        encoding: File encoding.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        p = _validate_path(path)
        original = p.read_text(encoding=encoding)
        current = original
        applied: list[str] = []
        for idx, edit in enumerate(edits, 1):
            if not isinstance(edit, dict):
                return f"[multi_edit error: edit #{idx} is not an object]"
            old = edit.get("old")
            new = edit.get("new")
            if old is None or new is None:
                return f"[multi_edit error: edit #{idx} missing 'old' or 'new']"
            expected = edit.get("expected_replacements", 1)
            count = current.count(old)
            if count != expected:
                return (
                    f"[multi_edit aborted: edit #{idx} expected {expected} match(es), "
                    f"found {count}. No changes written.]"
                )
            current = current.replace(old, new)
            applied.append(f"#{idx}: {count} replacement(s)")
        if current == original:
            return "[multi_edit skipped: no net changes]"
        p.write_text(current, encoding=encoding)
        return f"Applied {len(edits)} edit(s) to {p}\n" + "\n".join(applied)
    except Exception as e:
        return f"[multi_edit error: {type(e).__name__}: {e}]"


def git_add(paths: list[str], repo_path: str = ".") -> str:
    """Stage one or more paths in a git repository.

    Args:
        paths: Files or directories to stage (relative to repo_path or absolute under allowed base).
        repo_path: Repository root.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        repo = _validate_path(repo_path)
        cwd = repo if repo.is_dir() else repo.parent
        if not isinstance(paths, list) or not paths:
            return "[git_add error: 'paths' must be a non-empty list]"
        args = ["git", "add", "--"]
        for raw in paths:
            args.append(raw)
        return _run(args, cwd, 30)
    except Exception as e:
        return f"[git_add error: {type(e).__name__}: {e}]"


def git_commit(message: str, repo_path: str = ".", allow_empty: bool = False) -> str:
    """Create a new commit with the staged changes.

    Args:
        message: Commit message.
        repo_path: Repository root.
        allow_empty: Allow a commit with no staged changes (rarely useful).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not message or not message.strip():
            return "[git_commit error: message is required]"
        repo = _validate_path(repo_path)
        cwd = repo if repo.is_dir() else repo.parent
        args = ["git", "commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        return _run(args, cwd, 30)
    except Exception as e:
        return f"[git_commit error: {type(e).__name__}: {e}]"


def git_branch(repo_path: str = ".", all_branches: bool = False) -> str:
    """List branches in a repository.

    Args:
        repo_path: Repository root.
        all_branches: Include remote-tracking branches when true.
    """
    try:
        repo = _validate_path(repo_path)
        cwd = repo if repo.is_dir() else repo.parent
        args = ["git", "branch", "--no-color"]
        if all_branches:
            args.append("-a")
        return _run(args, cwd, 15)
    except Exception as e:
        return f"[git_branch error: {type(e).__name__}: {e}]"


def git_checkout(branch: str, repo_path: str = ".", create: bool = False) -> str:
    """Switch branches.

    Args:
        branch: Branch name to switch to.
        repo_path: Repository root.
        create: Create the branch if it does not exist (git checkout -b).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        repo = _validate_path(repo_path)
        cwd = repo if repo.is_dir() else repo.parent
        args = ["git", "checkout"]
        if create:
            args.append("-b")
        args.append(branch)
        return _run(args, cwd, 30)
    except Exception as e:
        return f"[git_checkout error: {type(e).__name__}: {e}]"


def git_blame(path: str, line_start: Optional[int] = None, line_end: Optional[int] = None) -> str:
    """Run git blame on a file (optionally restricted to a line range).

    Args:
        path: File path inside the repository.
        line_start: 1-based start line.
        line_end: 1-based end line.
    """
    try:
        p = _validate_path(path)
        if not p.is_file():
            return f"[git_blame error: not a file: {p}]"
        cwd = p.parent
        args = ["git", "blame", "--no-color"]
        if line_start is not None and line_end is not None:
            args.extend(["-L", f"{line_start},{line_end}"])
        args.append(p.name)
        return _run(args, cwd, 30)
    except Exception as e:
        return f"[git_blame error: {type(e).__name__}: {e}]"


def diff_files(path_a: str, path_b: str, max_lines: int = 400) -> str:
    """Show a unified diff between two files.

    Args:
        path_a: Original file path.
        path_b: Updated file path.
        max_lines: Truncate output to this many lines.
    """
    try:
        import difflib

        a = _validate_path(path_a)
        b = _validate_path(path_b)
        text_a = a.read_text(encoding="utf-8").splitlines()
        text_b = b.read_text(encoding="utf-8").splitlines()
        diff = list(
            difflib.unified_diff(text_a, text_b, fromfile=str(a), tofile=str(b), lineterm="")
        )
        if not diff:
            return "(no differences)"
        if len(diff) > max_lines:
            head = "\n".join(diff[:max_lines])
            return f"{head}\n... truncated at {max_lines} lines (total {len(diff)})"
        return "\n".join(diff)
    except Exception as e:
        return f"[diff_files error: {type(e).__name__}: {e}]"
