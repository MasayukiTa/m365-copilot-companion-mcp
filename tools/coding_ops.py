import contextlib
import os
import py_compile
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import contract_gate as _cg
from .file_ops import _validate_path
from .security import require_unlocked
from .walk import iter_files, pruned_note

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


def _norm(p: str) -> str:
    """Realpath-normalise a path string for reliable comparison across symlinks/junctions."""
    try:
        return os.path.normcase(os.path.realpath(p.strip()))
    except Exception:
        return os.path.normcase(p.strip())


def _is_shared_worktree(cwd: Path):
    """Is *cwd* the shared (main) working tree, rather than a dedicated linked worktree?

    Compares `git rev-parse --git-dir` with `--git-common-dir`. When they resolve to the
    same directory the checkout shares the repository's main working tree -- the place
    where a branch switch or a wholesale stage would sweep up other workers' and the
    owner's uncommitted changes. A linked worktree has a distinct --git-dir.

    Returns True (shared), False (dedicated worktree), or None when the answer cannot be
    determined -- callers treat None as fail-open (do not block on a guess).
    """
    try:
        gd = _run(["git", "rev-parse", "--git-dir"], cwd, 15)
        cd = _run(["git", "rev-parse", "--git-common-dir"], cwd, 15)
    except Exception:
        return None
    if "[returncode:" in gd or "[returncode:" in cd:
        return None

    def _extract(out: str):
        for line in out.splitlines():
            line = line.strip()
            if line and not line.startswith("["):
                return line
        return None

    g = _extract(gd)
    c = _extract(cd)
    if not g or not c:
        return None
    base = str(cwd)
    ga = g if os.path.isabs(g) else os.path.join(base, g)
    ca = c if os.path.isabs(c) else os.path.join(base, c)
    return _norm(ga) == _norm(ca)


def _add_is_wholesale(paths: list) -> bool:
    """True when a git add stages the whole tree: -A / --all / a bare '.' path."""
    for raw in paths:
        s = str(raw).strip()
        if s in ("-A", "--all", "."):
            return True
    return False


def _current_branch(cwd: Path):
    """Current branch name, or None if detached/unknown."""
    out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd, 15)
    if "[returncode:" in out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("["):
            return line
    return None


def _note(skipped_big: int, partial_files: int, skipped_dirs=None) -> str:
    """What the search did not cover, appended to whatever it did find.

    Built here because there are TWO returns -- the normal one and the early one that fires
    when max_matches is reached -- and only the normal one carried it. The early return is
    exactly the case where matches exist elsewhere, so the disclosure went missing precisely
    when the reader had most reason to trust the result.
    """
    parts = []
    if pruned_note(skipped_dirs):
        parts.append(pruned_note(skipped_dirs))
    if skipped_big:
        parts.append("%d file(s) larger than %d MB were not searched"
                     % (skipped_big, _GREP_MAX_FILE_BYTES // (1024 * 1024)))
    if partial_files:
        parts.append("%d file(s) were searched only up to a decoding error" % partial_files)
    return ("\n[" + "; ".join(parts) + "]") if parts else ""


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
        skipped: set = set()
        files = [target] if target.is_file() else iter_files(target, skipped)
        needle = pattern if case_sensitive else pattern.lower()
        skipped_big = 0
        partial_files = 0
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
                    try:
                        for line_no, line in enumerate(fh, 1):
                            line = line.rstrip("\n").rstrip("\r")
                            hay = line if case_sensitive else line.lower()
                            if needle in hay:
                                matches.append(f"{file_path}:{line_no}:{line}")
                                if len(matches) >= max_matches:
                                    return ("\n".join(matches)
                                            + _note(skipped_big, partial_files, skipped))
                    except UnicodeDecodeError:
                        # STOPPED PART-WAY, AND SAYS SO. Reading the file whole used to mean
                        # that a single bad byte contributed nothing from that file at all;
                        # streaming means the lines before it were already searched. Neither
                        # is wrong, but "searched half of it" must not read as "searched it"
                        # -- a torn-tailed jsonl log is normal here, and those are exactly the
                        # files somebody greps when something has gone wrong.
                        partial_files += 1
            except OSError:
                continue
        return ("\n".join(matches) + _note(skipped_big, partial_files, skipped)) if matches \
            else ("(no matches)" + _note(skipped_big, partial_files, skipped))
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
    return multi_edit_local(path, edits, encoding)


def multi_edit_local(
    path: str,
    edits: list[dict],
    encoding: str = "utf-8",
) -> str:
    """Apply the edits without the unlock gate. FOR IN-PROCESS CALLERS ONLY; not a tool.

    Same reasoning as memory_save_local and runlog_append_local, and the same measured
    symptom. The gate asks "has the REMOTE caller proved possession of the password for its
    IP". A caller inside this process has no remote identity for that question to be about,
    so every in-process call is refused -- and the refusal is a returned STRING, so a caller
    that does not inspect it carries on believing the file was written.

    That is not hypothetical here: the cross-file edit cell in tools/auto/autoloop.py called
    multi_edit in-process and every single edit came back
    "[locked: no HTTP request context]". The cell would have shipped reverting a tree it had
    never changed, and reporting that it had.

    Split out rather than reimplemented, so the matching rules -- expected_replacements, the
    all-or-nothing abort, "no net changes" -- cannot drift between the two callers.
    """
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
        # (A) Refuse wholesale staging. `-A` / `--all` / a bare `.` stage every change in the
        # working tree, including other workers' and the owner's uncommitted files that this
        # run does not own. Refuse without stopping the run and say what to do instead.
        if _add_is_wholesale(paths):
            return (
                "[git_add refused: wholesale staging (-A / --all / '.') is not allowed here.\n"
                "Why: this working tree is shared by other workers and the owner. Staging the\n"
                "whole tree sweeps up their uncommitted changes into your commit.\n"
                "Instead: pass the explicit paths you changed, e.g. git_add(['tools/x.py',\n"
                "'tests/test_x.py']). Run git_status first to see exactly which files are yours.]"
            )
        # (B) Route through the destructive-op gate so an active contract can ask-before.
        _g = _cg.check_op("shell_destructive", "git add -- " + " ".join(str(p) for p in paths))
        if _g is not None:
            return _g
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
        # (A) Refuse committing directly onto a protected branch (main/master). A worker
        # should land work on its own branch and open a PR, not write straight to main.
        _br = _current_branch(cwd)
        if _br in ("main", "master"):
            return (
                "[git_commit refused: committing directly onto '%s' is not allowed.\n"
                "Why: shared history on the protected branch must not receive un-reviewed\n"
                "commits from an automated run.\n"
                "Instead: create a feature branch first (git_checkout(branch, create=True)) and\n"
                "commit there, then open a pull request.]" % _br
            )
        # (B) Route through the destructive-op gate so an active contract can ask-before.
        _g = _cg.check_op("shell_destructive", "git commit -m " + message)
        if _g is not None:
            return _g
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
        # Creating a new branch (git checkout -b) discards nothing, so it is always allowed.
        if not create:
            # (A) Refuse a branch switch in the shared (main) working tree: it would carry the
            # owner's and other workers' uncommitted changes onto another branch.
            if _is_shared_worktree(cwd) is True:
                return (
                    "[git_checkout refused: switching branches in the shared working tree is not\n"
                    "allowed.\n"
                    "Why: this checkout shares the repository's main working tree with other\n"
                    "workers and the owner. A branch switch here sweeps their uncommitted changes\n"
                    "onto the target branch.\n"
                    "Instead: create an isolated worktree for your branch, e.g.\n"
                    "  git worktree add -b <branch> ../wt/<branch> <base>\n"
                    "and work there. To start a NEW branch in place, use create=True (git\n"
                    "checkout -b), which is permitted because it discards nothing.]"
                )
            # (B) Route through the destructive-op gate so an active contract can ask-before.
            _g = _cg.check_op("shell_destructive", "git checkout " + str(branch))
            if _g is not None:
                return _g
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


# --------------------------------------------------------------------------------------
# Scoped worktree lifecycle.
#
# Isolated work belongs in a dedicated linked worktree, never in the shared (main) working
# tree -- that rule is already stated by git_checkout above. What was missing was the OTHER
# half of the lifecycle: taking the worktree down again. Two existing call sites
# (bench/pro_capture.py, bench/pro_cycle.py) each grew their own teardown, and both learned
# the same lesson the hard way on Windows:
#
#   * `shutil.rmtree` cannot unlink the locked `.git` administrative entry, so it leaves a
#     HUSK -- a directory that git no longer tracks as a worktree but whose `.git` file still
#     resolves to the MAIN repository. A later step that walks that husk reads the harness's
#     own checkout and can submit the parent repo's state as if it were the work.
#   * A teardown that deletes by PATH, without first asking whether the path is the shared
#     working tree, can delete the very tree that holds every other worker's and the owner's
#     uncommitted changes.
#
# The two functions below are the single, safe teardown those sites should route through:
# `git worktree remove --force` first (git removes its own administrative files properly),
# rmtree only as a fallback and only when doing so cannot touch the shared tree, and a
# `git worktree prune` to clear any now-stale bookkeeping. `worktree_scope` wraps add +
# guaranteed teardown so an exception mid-work still tears the worktree down.


def _resolves_into_common_dir(worktree_path: Path, repo_cwd: Path):
    """True when *worktree_path* shares the repository's main working tree / git dir.

    A husk left by a half-finished rmtree still carries a `.git` that points at the main
    repository's common dir. Removing such a directory with rmtree would be deleting inside
    (or alongside) the shared checkout. Returns True (shared -- refuse to rmtree),
    False (a genuine linked worktree, safe to fall back on), or None when git cannot answer
    and the caller must not guess.
    """
    if not worktree_path.exists():
        # Nothing on disk to be unsafe about.
        return False
    shared = _is_shared_worktree(worktree_path)
    if shared is True:
        return True
    if shared is None:
        # git could not tell us whether this is a linked worktree. Compare the two paths
        # directly as a last resort: if the worktree path IS the repo path, it is shared.
        try:
            if _norm(str(worktree_path)) == _norm(str(repo_cwd)):
                return True
        except Exception:
            return None
        return None
    return False


def worktree_remove(worktree_path: str, repo_path: str = ".", prune: bool = True) -> str:
    """Tear down a dedicated linked worktree safely.

    Routes through `git worktree remove --force`; on failure falls back to an OS delete ONLY
    when the target is provably not the shared (main) working tree, then prunes stale
    worktree bookkeeping. Refuses outright to delete the shared working tree.

    Args:
        worktree_path: Path of the linked worktree to remove.
        repo_path: The repository whose worktree list owns it (for remove/prune).
        prune: Run `git worktree prune` afterwards to clear stale administrative entries.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        wt = _validate_path(worktree_path)
        repo = _validate_path(repo_path)
        repo_cwd = repo if repo.is_dir() else repo.parent

        # (A) Never delete the shared working tree. A teardown that swept it up would take
        # every other worker's and the owner's uncommitted changes with it.
        shared = _resolves_into_common_dir(wt, repo_cwd)
        if shared is True:
            return (
                "[worktree_remove refused: the path resolves to the shared (main) working\n"
                "tree, not a dedicated linked worktree.\n"
                "Why: removing it would delete the checkout that holds other workers' and the\n"
                "owner's uncommitted changes.\n"
                "Instead: pass the path of the linked worktree you created for this run.]"
            )

        # (B) Route through the destructive-op gate so an active contract can ask-before.
        _g = _cg.check_op("shell_destructive", "git worktree remove --force " + str(wt))
        if _g is not None:
            return _g

        parts: list[str] = []
        done = _run(["git", "worktree", "remove", "--force", str(wt)], repo_cwd, 30)
        removed_by_git = "[returncode:" not in done
        parts.append("git worktree remove: " + ("ok" if removed_by_git else done.strip()))

        # (C) Fallback delete, but only when it CANNOT touch the shared tree. `shared` is
        # False (genuine linked worktree) or None (unknown). Only act on the definite case;
        # an unknown answer must not license deleting a directory that might be the main tree.
        if not removed_by_git and wt.exists():
            if shared is False:
                shutil.rmtree(str(wt), ignore_errors=True)
                if wt.exists():
                    parts.append("rmtree fallback: directory still present")
                else:
                    parts.append("rmtree fallback: removed")
            else:
                parts.append(
                    "rmtree fallback SKIPPED: could not confirm this is a linked worktree, "
                    "so an OS delete might have hit the shared tree"
                )

        if prune:
            pr = _run(["git", "worktree", "prune"], repo_cwd, 30)
            parts.append("git worktree prune: " + ("ok" if "[returncode:" not in pr
                                                    else pr.strip()))
        return "\n".join(parts)
    except Exception as e:
        return f"[worktree_remove error: {type(e).__name__}: {e}]"


def worktree_add(worktree_path: str, branch: str, base: str = "HEAD",
                 repo_path: str = ".") -> str:
    """Create a dedicated linked worktree on a NEW branch, off *base*.

    A new branch discards nothing, and a linked worktree keeps this run's edits out of the
    shared checkout -- the isolation git_checkout points callers at. Off an explicit base
    (a committed ref by default) so the new worktree never carries the shared tree's
    uncommitted changes.

    Args:
        worktree_path: Where to create the linked worktree.
        branch: New branch name to create for it.
        base: Committed ref to branch from (default HEAD).
        repo_path: The repository to add the worktree to.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        repo = _validate_path(repo_path)
        repo_cwd = repo if repo.is_dir() else repo.parent
        wt = _validate_path(worktree_path)
        _g = _cg.check_op("shell_destructive",
                          "git worktree add -b %s %s %s" % (branch, wt, base))
        if _g is not None:
            return _g
        return _run(["git", "worktree", "add", "-b", branch, str(wt), base], repo_cwd, 60)
    except Exception as e:
        return f"[worktree_add error: {type(e).__name__}: {e}]"


@contextlib.contextmanager
def worktree_scope(worktree_path: str, branch: str, base: str = "HEAD",
                   repo_path: str = "."):
    """Create an isolated linked worktree, yield its path, and ALWAYS tear it down.

    FOR IN-PROCESS CALLERS. This is the shape the two bench teardown sites should share: the
    worktree is removed in a `finally`, so an exception mid-work cannot leave a husk behind,
    and the removal goes through `worktree_remove`, which refuses to touch the shared tree
    and never lets an rmtree fallback hit it.

    Yields the worktree path on success, or None when creation failed (teardown then has
    nothing to do and is a safe no-op).

    Args:
        worktree_path: Where to create the linked worktree.
        branch: New branch name to create for it.
        base: Committed ref to branch from (default HEAD).
        repo_path: The repository to add the worktree to.
    """
    created = worktree_add(worktree_path, branch, base, repo_path)
    ok = "[returncode:" not in created and not created.startswith("[worktree_add") \
        and not created.startswith("[locked")
    try:
        yield (worktree_path if ok else None)
    finally:
        # Only tear down what we actually created. If add failed, there is no linked
        # worktree to remove -- and worktree_remove would in any case refuse anything that
        # is not a genuine linked worktree.
        if ok:
            worktree_remove(worktree_path, repo_path)
