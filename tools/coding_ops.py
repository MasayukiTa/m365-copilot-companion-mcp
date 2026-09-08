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


def _realpath_strict(p: str) -> str:
    """Realpath-normalise *p*, RAISING if the real path cannot be resolved.

    `_norm` above swallows a realpath failure and falls back to the raw string. That is fine
    for a best-effort comparison, but it is exactly the wrong default for a guard that decides
    whether a DESTRUCTIVE operation may proceed: an unresolvable path would then compare equal
    to itself and read as "confirmed". This variant keeps the failure so the caller can treat
    "could not confirm" as "do not allow" (fail-closed) rather than as "safe".

    On Windows a checkout may be reached through a symlink, a junction, a differently-cased
    drive letter, or a trailing separator; os.path.realpath resolves the link/junction and
    os.path.normcase folds case and separators so two spellings of one directory match.
    """
    real = os.path.realpath(p)
    # realpath never raises on a non-existent tail, so verify the target actually resolves to
    # something on disk; "the path we are about to operate on does not exist" is a
    # cannot-confirm, not a safe.
    if not os.path.exists(real):
        raise OSError(f"path does not resolve to an existing location: {p!r}")
    return os.path.normcase(real)


def _count_local_changes(cwd: Path) -> Optional[int]:
    """How many tracked files have uncommitted changes in *cwd*'s working tree.

    Used to WARN before an operation that would discard them, so nothing is thrown away
    silently. Counts porcelain lines (staged or unstaged, plus untracked). Returns None when
    the count cannot be taken -- a None must be surfaced as "unknown", never as zero.
    """
    out = _run(["git", "status", "--porcelain"], cwd, 15)
    if "[returncode:" in out:
        return None
    # `_run` returns the sentinel "(no output)" when the command printed nothing, which for
    # `status --porcelain` means a CLEAN tree. Counting that sentinel as a change would report
    # 1 for a repository with nothing to discard -- so map it to zero explicitly.
    if out.strip() == "(no output)":
        return 0
    return sum(1 for line in out.splitlines() if line.strip() and not line.startswith("["))


def _dedicated_root_ok(op_path: str, cwd: Path):
    """Is *op_path* positively confirmed to be a DEDICATED repo root that may take a
    destructive git operation?

    This is the affirmative counterpart to `_is_shared_worktree`. That predicate only blocks
    when it can PROVE the tree is shared and fail-opens (returns None) otherwise; for a
    destructive operation that is too weak. Here the default is deny: a call is allowed only
    when both of these hold, and refused with a reason otherwise.

      1. `git rev-parse --show-toplevel` and *op_path* resolve, via realpath + normcase, to
         the SAME directory -- so the operation is at the checkout root, not a subdirectory or
         a look-alike path. If either realpath cannot be resolved, this fails CLOSED: a path
         we could not confirm is not treated as safe (a past incident here read a confirmation
         failure as "safe" and stopped a healthy process).
      2. `_is_shared_worktree(cwd)` is exactly False -- the tree is affirmatively a dedicated
         linked worktree, not the shared main tree and not an undetermined guess.

    Returns (True, "") when allowed, or (False, reason) where *reason* says why it was refused
    and what to do instead. The caller is an agent, so a reason lets it pick another route.
    """
    try:
        top = _run(["git", "rev-parse", "--show-toplevel"], cwd, 15)
    except Exception as e:  # pragma: no cover - subprocess launch failure
        return (False, _dedicated_deny_msg(
            f"could not run 'git rev-parse --show-toplevel' ({type(e).__name__}). "
            "The repository root could not be confirmed."))
    if "[returncode:" in top:
        return (False, _dedicated_deny_msg(
            "'git rev-parse --show-toplevel' failed; this may not be a git working tree. "
            "The repository root could not be confirmed."))
    toplevel = None
    for line in top.splitlines():
        line = line.strip()
        if line and not line.startswith("["):
            toplevel = line
            break
    if not toplevel:
        return (False, _dedicated_deny_msg(
            "'git rev-parse --show-toplevel' returned no path. "
            "The repository root could not be confirmed."))

    # FAIL-CLOSED on any realpath resolution failure. "Could not confirm" is not "safe".
    try:
        real_top = _realpath_strict(toplevel)
        real_op = _realpath_strict(str(op_path))
    except OSError as e:
        return (False, _dedicated_deny_msg(
            f"the real path could not be resolved ({e}). A checkout that cannot be resolved "
            "to a concrete on-disk root is not treated as safe."))

    if real_top != real_op:
        return (False, _dedicated_deny_msg(
            "the path being operated on is not the checkout root. Destructive operations are "
            "only allowed at the repository top level, run them from '%s'." % toplevel))

    shared = _is_shared_worktree(cwd)
    if shared is not False:
        detail = ("this is the shared (main) working tree" if shared is True
                  else "whether this checkout is dedicated could not be determined")
        return (False, _dedicated_deny_msg(
            detail + ". A destructive operation is only allowed in a checkout affirmatively "
            "confirmed to be a dedicated linked worktree."))

    return (True, "")


def _dedicated_deny_msg(reason: str) -> str:
    """A refusal an agent can act on: why it was refused, and the concrete alternative."""
    return (
        "[refused: this checkout is not a confirmed dedicated repository root.\n"
        "Why: %s\n"
        "Instead: create an isolated worktree for your branch and run the operation there:\n"
        "  git worktree add -b <branch> ../wt/<branch> <base>\n"
        "then operate from that worktree's root. To start a NEW branch in place without "
        "discarding anything, use create=True (git checkout -b).]" % reason
    )


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
            # (A2) Affirmatively confirm this checkout is a dedicated repo root before a
            # discarding switch. _is_shared_worktree above only blocks a PROVEN shared tree;
            # this predicate refuses unless the checkout is positively confirmed dedicated
            # AND the operating path realpath-matches the checkout root, and it fails closed
            # when confirmation is impossible.
            _ok, _reason = _dedicated_root_ok(str(cwd), cwd)
            if not _ok:
                # A branch switch discards nothing from the index but replaces the working
                # tree; count what is uncommitted so it is not swept away silently.
                _n = _count_local_changes(cwd)
                _warn = ""
                if _n is None:
                    _warn = ("\n[warning: the number of uncommitted changes could not be "
                             "determined; treating as unknown, not zero.]")
                elif _n > 0:
                    _warn = ("\n[warning: %d uncommitted change(s) in this working tree would "
                             "be at risk from a branch switch; none were discarded because the "
                             "switch was refused.]" % _n)
                return _reason.replace("[refused:", "[git_checkout refused:", 1) + _warn
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
