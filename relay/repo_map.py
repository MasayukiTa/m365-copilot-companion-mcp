"""repo_map.py -- a compact, structured map of a code folder to PRIME the agent.

Claude Code / aider feel smart partly because they don't start blind: they carry a map
of the repository (files + the functions/classes in them) so the model navigates instead
of guessing. The Copilot impl agent has grep/read_file but a bounded context window, so
handing it a small map up front makes it spend its turns editing the RIGHT files rather
than rediscovering the layout.

build_map(folder) returns a short text block: a file tree plus, for each Python file, its
top-level defs/classes with signatures (via the stdlib `ast`, so no third-party deps and
no code execution). Non-Python code files are listed by path. The whole thing is capped
(default ~4000 chars) so it fits inside a goal prompt without blowing the context.
"""
from __future__ import annotations

import ast
import os

_SKIP = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__",
                   "dist", "build", ".fleet", ".setup", ".companion_runs"})

_CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".cs", ".java", ".go", ".rb",
              ".rs", ".cpp", ".c", ".h", ".sql", ".sh", ".ps1")


def _first_doc_line(node):
    try:
        doc = ast.get_docstring(node)
    except Exception:
        doc = None
    if not doc:
        return ""
    return " ".join(doc.strip().splitlines()[0].split())[:80]


def _sig(fn):
    """A readable signature 'name(a, b, c=...)' from a FunctionDef node."""
    a = fn.args
    parts = []
    for arg in a.posonlyargs:
        parts.append(arg.arg)
    if a.posonlyargs:
        parts.append("/")
    for arg in a.args:
        parts.append(arg.arg)
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    for arg in a.kwonlyargs:
        parts.append(arg.arg + "=...")
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    # mark default-valued positional args with =... (best effort)
    ndef = len(a.defaults)
    if ndef:
        npos = len(a.posonlyargs) + len(a.args)
        for i in range(npos - ndef, npos):
            if 0 <= i < len(parts):
                if not parts[i].endswith("/") and "=" not in parts[i] and not parts[i].startswith("*"):
                    parts[i] = parts[i] + "=..."
    return "%s(%s)" % (fn.name, ", ".join(parts))


def _python_outline(path):
    """Top-level defs/classes (with method names) of a Python file, as indented lines.
    Returns [] if the file can't be parsed (it's still listed by path by the caller)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except Exception:
        return None
    lines = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = _first_doc_line(node)
            lines.append("  def %s%s" % (_sig(node), ("  - " + doc) if doc else ""))
        elif isinstance(node, ast.ClassDef):
            doc = _first_doc_line(node)
            lines.append("  class %s%s" % (node.name, ("  - " + doc) if doc else ""))
            methods = [b for b in node.body
                       if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for m in methods[:12]:
                lines.append("    def %s" % _sig(m))
            if len(methods) > 12:
                lines.append("    ... (%d more methods)" % (len(methods) - 12))
    return lines


def build_map(folder, max_files=300, max_chars=4000):
    """Return a compact text map of `folder`. Caps both the number of files scanned and
    the output size so it is safe to prepend to a prompt."""
    root = os.path.abspath(folder)
    if not os.path.isdir(root):
        return ""
    rows = []
    seen = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP)
        for name in sorted(filenames):
            ext = os.path.splitext(name)[1].lower()
            if ext not in _CODE_EXTS and ext not in (".md", ".json", ".yaml", ".yml", ".txt"):
                continue
            seen += 1
            if seen > max_files:
                truncated = True
                break
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            rows.append(rel)
            if ext == ".py":
                outline = _python_outline(full)
                if outline:
                    rows.extend(outline)
        if truncated:
            break

    header = "リポジトリ地図 (%s):" % root
    out = [header]
    used = len(header)
    for r in rows:
        if used + len(r) + 1 > max_chars:
            out.append("... (map truncated)")
            break
        out.append(r)
        used += len(r) + 1
    if truncated:
        out.append("... (%d+ files; map limited)" % max_files)
    return "\n".join(out)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Print a compact repo map of a folder.")
    ap.add_argument("--folder", required=True)
    ap.add_argument("--max-chars", type=int, default=4000)
    args = ap.parse_args()
    print(build_map(args.folder, max_chars=args.max_chars))


if __name__ == "__main__":
    main()
