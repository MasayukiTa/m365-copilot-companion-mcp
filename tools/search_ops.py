import fnmatch
import heapq
import stat
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
            return f"0 matches under {base}"
        # 件数を先頭で返す。返していなかった頃は、呼び出し側が一覧を目で数えて
        # 同じ問いに 14/15/16/17 と答えていた(正解は16、実測10回)。数えるのは
        # こちらの仕事で、読み取り専用なので unlock も要らない。
        # どこを見た件数なのかを書く。省略時の "." はサーバの作業ディレクトリで、
        # 呼び出し側が意図する場所とまず一致しない。書いていなかった頃、別の場所の
        # 結果を見て混乱し、やり直しの過程で手計算に落ちて数を外した回があった。
        head = f"{len(results)} matches under {base}" + (
            f" (truncated at {max_results}; more exist)" if truncated else ""
        )
        return "\n".join([head] + [str(p) for p in results])
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
        for p in base.rglob("*"):
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
            return "(no matches)"
        out = [t[2] for t in sorted(best, reverse=True)]
        if truncated:
            out.append(f"... truncated at {max_results} entries")
        return "\n".join(out)
    except Exception as e:
        return f"[find_files error: {type(e).__name__}: {e}]"
