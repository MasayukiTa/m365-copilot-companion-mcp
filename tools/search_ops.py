import fnmatch
import heapq
import stat
import threading
import os
import re
import time
from functools import wraps
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path
from .walk import (
    PRUNED_DIRS,
    WalkDeadline,
    is_pruned,
    iter_files,
    pruned_note,
    search_concurrency,
)

#: The cap on simultaneous searches. Sized once at import from the settings value (default 4,
#: the pre-#328 constant) because a BoundedSemaphore cannot be resized in place; raising the
#: setting takes effect on the next server start. The number is declared in
#: tools/settings_keys.py (search_concurrency) with the reason it exists: an unbounded
#: find_files / glob jammed the gateway three times on 2026-10-27..28, recoverable only by a
#: restart, so a hard cap on how many such walks can run at once is one of the guards.
SEARCH_CONCURRENCY = search_concurrency()
_SEARCH_SLOTS = threading.BoundedSemaphore(SEARCH_CONCURRENCY)


# 2026-11-01, this Windows host, depth 4 and 10,000 entries/root: Downloads
# 7,189 entries/0.657s; work 0/0s; m365-work 10,000/0.843s (capped);
# companion/work 0/0s. Depth 4 bounds nested trees (485 Downloads cutoffs);
# the entry cap bounds large roots. Five seconds globally limits worker delay;
# 30 results bounds prompt size. Filesystem calls can individually exceed this soft deadline.
def case_reference_search(goal, roots=None, timeout=5.0, max_results=30):
    """Resolve a short person_case-id-topic reference within local case roots.

    Incomplete walks are reported to the caller. Symlink directories are not followed.
    """
    match = re.match(r"^\s*([^\s/\\]{1,40}?[ _＿-]+(\d{2,})[^\s/\\]{2,40})\s+", goal)
    if not match or len(goal) > 150:
        return None
    title, case_id = match.group(1), match.group(2)
    if roots is None:
        configured = os.environ.get("M365_CASE_ROOTS", "")
        roots = ([Path(p) for p in configured.split(os.pathsep) if p] if configured else [
            Path.home() / "Downloads",
            Path.cwd() / "work",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Temp" / "m365-work",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Temp" / "m365-companion" / "work",
        ])
    result = {"candidates": [], "errors": [], "truncated": False}
    deadline = time.monotonic() + max(0, timeout)
    if len(roots) > 8:
        result["errors"].append("root limit exceeded")
        roots = roots[:8]
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        stack = [(root, 0)]
        scanned = 0
        while stack:
            if time.monotonic() >= deadline:
                result["errors"].append("search timeout")
                return result
            directory, depth = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > 10000:
                            result["truncated"] = True
                            stack.clear()
                            break
                        if time.monotonic() >= deadline:
                            result["errors"].append("search timeout")
                            return result
                        if entry.is_symlink() or entry.name.startswith('.'):
                            continue
                        is_dir = entry.is_dir(follow_symlinks=False)
                        if is_dir and entry.name not in {"node_modules", "__pycache__", ".git"}:
                            if depth < 4:
                                stack.append((Path(entry.path), depth + 1))
                            else:
                                result["truncated"] = True
                        # Full title avoids treating the same numeric ID as a task identity.
                        if title.casefold() not in entry.name.casefold():
                            continue
                        path = Path(entry.path)
                        source = path / "やりたいこと.txt" if is_dir else path
                        excerpt = ""
                        if source.is_file() and source.suffix.lower() in {".txt", ".md"}:
                            with source.open("r", encoding="utf-8-sig", errors="replace") as f:
                                excerpt = f.read(600)
                        result["candidates"].append({"path": str(path), "kind": "directory" if is_dir else "file", "request": str(source) if is_dir and source.is_file() else "", "excerpt": excerpt, "root": str(root), "case_id": case_id})
                        if time.monotonic() >= deadline:
                            result["errors"].append("search timeout")
                            return result
                        if len(result["candidates"]) >= max_results:
                            result["truncated"] = True
                            return result
            except OSError as exc:
                result["errors"].append("%s: %s" % (directory, exc))
    return result


def _bounded_search(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not _SEARCH_SLOTS.acquire(blocking=False):
            # FAIL FAST, DO NOT QUEUE. issue #328 proposal 4: a search that cannot get a slot
            # is refused immediately with a message that says why, rather than blocking a
            # gateway thread for an unbounded time waiting behind the ones already running.
            return f"busy: {SEARCH_CONCURRENCY} searches already running, retry later"
        try:
            return func(*args, **kwargs)
        finally:
            _SEARCH_SLOTS.release()
    return wrapped


def _cut_note(deadline):
    return f"[search cut after {deadline.seconds} seconds; results may be incomplete]"



@_bounded_search
def glob(
    pattern: str,
    path: str = ".",
    max_results: int = 200,
    include_hidden: bool = False,
    max_depth: Optional[int] = None,
) -> str:
    """Find files matching a glob pattern, sorted by most recently modified.

    Args:
        pattern: Glob pattern. Supports ** for recursive descent, for example "**/*.py"
            or "src/**/*.{ts,tsx}". Brace expansion is supported manually here.
        path: Base directory to search under. Must be inside the allowed base.
        max_results: Maximum number of matches to return.
        include_hidden: When false, skip dotfiles and dot-directories.
        max_depth: For a recursive (`**`) pattern only, the deepest directory level below
            `path` to descend into, counted from `path` (depth 0 == directly under `path`).
            None (default) descends without a depth limit unless the owner has set the
            search_max_depth setting. A non-recursive pattern does not walk, so this does not
            apply to it (issue #328).
    """
    try:
        # 呼び出し側は path ではなく pattern 側にパスごと書くことが多い
        # (glob("~/Desktop/*.md"))。これを黙って 0 matches で返していたので、
        # 「~ が展開されない」と誤解されて別経路のフォールバックを誘発していた。
        # 実測10回のうち誤答6回がすべてこの分岐から始まっていた。
        pattern, path = _split_rooted_pattern(pattern, path)
        if pattern.startswith("~") or Path(pattern).is_absolute():
            # 分割されずに残った = 根の実体が無い。そのまま基点の下を探すと
            # 0 matches になり、綴り違いが「該当なし」に化ける。
            return (f"[glob error: not found: "
                    f"{Path(pattern.replace(chr(92), '/')).parent.expanduser()}]")

        base = _validate_path(path)
        if not base.is_dir():
            # 「該当なし」と「そんな場所は無い」を混ぜない。混ぜていた頃は、
            # 綴り違いのパスに 0 matches が返り、無いものを無いと信じられた。
            what = "not found" if not base.exists() else "not a directory"
            return f"[glob error: {what}: {base}]"

        patterns = _expand_braces(pattern)
        # PRUNED LIKE THE OTHER TWO. glob was left walking everything when grep and
        # find_files stopped, which is worse than either state on its own: the same question
        # asked three ways gave two answers that skip the vendored tree and one that does not,
        # with nothing saying so.
        #
        # Only recursive patterns take the pruned walk. `**` is what makes a glob descend, so
        # it is also what makes it expensive, and confining the change there leaves every
        # non-recursive pattern matching exactly as pathlib matched it before.
        seen: set[Path] = set()
        gskipped: set = set()
        deadline = WalkDeadline()
        for pat in patterns:
            if deadline.expired():
                break
            if "**" in pat:
                tail = pat.replace("\\", "/").split("**/", 1)[-1]
                for match in iter_files(base, gskipped, deadline, **({"max_depth": max_depth} if max_depth is not None else {})):
                    try:
                        rel = match.relative_to(base).as_posix()
                    except ValueError:
                        continue
                    if not (fnmatch.fnmatch(rel, tail) or fnmatch.fnmatch(match.name, tail)):
                        continue
                    if not include_hidden and _is_hidden(match, base):
                        continue
                    seen.add(match)
                continue
            for match in base.glob(pat):
                if deadline.expired():
                    break
                if not match.is_file():
                    continue
                if not include_hidden and _is_hidden(match, base):
                    continue
                if is_pruned(match, base):
                    gskipped.update(
                        part for part in match.relative_to(base).parts[:-1]
                        if part in PRUNED_DIRS)
                    continue
                seen.add(match)

        results = sorted(seen, key=lambda p: p.stat().st_mtime, reverse=True)
        truncated = len(results) > max_results
        results = results[:max_results]
        if not results:
            return f"0 matches under {base}" + ("\n" + _cut_note(deadline) if deadline.cut else "")
        # 件数を先頭で返す。返していなかった頃は、呼び出し側が一覧を目で数えて
        # 同じ問いに 14/15/16/17 と答えていた(正解は16、実測10回)。数えるのは
        # こちらの仕事で、読み取り専用なので unlock も要らない。
        # どこを見た件数なのかを書く。省略時の "." はサーバの作業ディレクトリで、
        # 呼び出し側が意図する場所とまず一致しない。書いていなかった頃、別の場所の
        # 結果を見て混乱し、やり直しの過程で手計算に落ちて数を外した回があった。
        head = f"{len(results)} matches under {base}" + (
            f" (truncated at {max_results}; more exist)" if truncated else ""
        )
        # ON ITS OWN LINE. The first line is the count, and callers read it as such -- three
        # tests pin that exact string. A disclosure that has to be appended somewhere does not
        # get to be appended to a line that already means something else.
        tail = ["[" + pruned_note(gskipped) + "]"] if pruned_note(gskipped) else []
        if deadline.cut:
            tail.append(_cut_note(deadline))
        return "\n".join([head] + [str(p) for p in results] + tail)
    except Exception as e:
        return f"[glob error: {type(e).__name__}: {e}]"


def _split_rooted_pattern(pattern: str, path: str) -> tuple[str, str]:
    """"~/Desktop/*.md" のような根つきパターンを (パターン, 基点) に割る。

    ワイルドカードより手前の、実在するディレクトリまでを基点に寄せる。割れない
    ものは触らない。根つきパターンは場所を自分で言い切っているので、path が
    別に渡されていてもパターン側を採る。基点を足しようがないため。

    存在しないディレクトリを指していたときは分割せず、そのまま返す。呼び出し元が
    「該当なし」ではなく「そんな場所は無い」と言えるようにするため。
    """
    rooted = pattern.startswith("~") or Path(pattern).is_absolute()
    if not rooted:
        return pattern, path

    parts = Path(pattern.replace("\\", "/")).parts
    fixed: list[str] = []
    for part in parts:
        if any(c in part for c in "*?["):
            break
        fixed.append(part)
    if not fixed:
        return pattern, path

    base = Path(*fixed)
    rest = parts[len(fixed):]
    if not rest:
        return pattern, path          # ワイルドカード無し。glob の仕事ではない
    try:
        if not base.expanduser().is_dir():
            return pattern, path
    except Exception:
        return pattern, path
    return "/".join(rest), str(base)


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


@_bounded_search
def find_files(
    name_contains: str,
    path: str = ".",
    max_results: int = 200,
    max_depth: Optional[int] = None,
) -> str:
    """Find files whose name contains a substring. Case-insensitive.

    Useful when you want a name-based search without writing a glob.

    Args:
        name_contains: Substring to look for in filenames.
        path: Base directory.
        max_results: Maximum matches.
        max_depth: Deepest directory level below `path` to descend into, counted from `path`
            (a file directly under `path` is depth 0, so max_depth=0 searches only `path`
            itself). None (default) descends without a depth limit unless the owner has set a
            floor-wide cap via the search_max_depth setting. Use this to bound a search to a
            known sub-tree instead of walking an entire drive (issue #328).
    """
    try:
        base = _validate_path(path)
        if not base.is_dir():
            return f"[find_files error: not a directory: {base}]"
        # ONLY THE ANSWER IS HELD, NOT THE SEARCH. This used to build a list of every matching
        # path in the tree, stat() each one to sort by mtime, and only then cut it down to
        # max_results -- so the peak was set by how many files matched, and the caller's cap
        # bounded nothing at all. Under this repository (its own .venv and .git included) with
        # several fleet workers calling at once, that was the second half of the MCP server's
        # climb; py-spy found three threads inside this comprehension at the same moment.
        #
        # The contract is unchanged -- still the newest max_results matches across the WHOLE
        # tree -- because a bounded heap keeps the largest N of a stream. Memory is now fixed
        # by the cap the caller already passes.
        needle = name_contains.lower()
        best: list[tuple[float, int, str]] = []
        seen = 0
        truncated = False
        skipped: set = set()
        deadline = WalkDeadline()
        for p in iter_files(base, skipped, deadline, **({"max_depth": max_depth} if max_depth is not None else {})):
            if needle not in p.name.lower():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            # `stat()` once, and read is-it-a-file off the same result. The old code paid a
            # separate is_file() syscall per candidate and then stat()'d the survivors again.
            seen += 1
            # -seen SO EQUAL MTIMES KEEP THE ONE FOUND FIRST. The heap keeps the LARGEST N, so
            # a plain counter made the tie-break "last walked wins" -- the reverse of the old
            # stable sort, and visible in any directory of same-second files (an extracted
            # archive, a generated tree).
            item = (st.st_mtime, -seen, str(p))
            if len(best) < max_results:
                heapq.heappush(best, item)
            else:
                heapq.heappushpop(best, item)
                truncated = True
        if not best:
            return "(no matches)" + ("\n" + _cut_note(deadline) if deadline.cut else "")
        out = [t[2] for t in sorted(best, reverse=True)]
        if truncated:
            out.append(f"... truncated at {max_results} entries")
        if pruned_note(skipped):
            out.append("[" + pruned_note(skipped) + "]")
        if deadline.cut:
            out.append(_cut_note(deadline))
        return "\n".join(out)
    except Exception as e:
        return f"[find_files error: {type(e).__name__}: {e}]"
