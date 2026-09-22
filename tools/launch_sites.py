# -*- coding: utf-8 -*-
"""Every place tracked Python starts a child process, and whether it says how.

WHY A SCANNER AND NOT A GREP. The question is not "does the word subprocess appear" but
"does this call decide its own console policy", which needs the call's keywords and the
function it sits in. A grep gives neither, and the identity it produces -- a line number --
moves every time somebody edits the file above it.

WHAT IT DOES NOT SEE, stated here because a list that does not say this gets read as complete:

  * a launch through a name this module does not know: `Popen` imported directly, an alias,
    `asyncio.create_subprocess_exec`, a third-party launcher, `multiprocessing` with a spawn
    start method (which re-executes sys.executable on Windows)
  * a launch inside a .bat, a shell string, or any grandchild. `shell=True` hands the whole
    question to cmd.exe, and a child that starts its own child escapes entirely
  * whether a flag that IS passed is the right one. `creationflags=0` counts as decided here,
    because deciding on zero is a decision; this module reports, it does not judge

WHY "DECIDED" RATHER THAN "SAFE". A console window appears when a console program is started
by a parent that has no console -- measured 2026-09-22 from a console-less parent: with no
flags the child's console window is VISIBLE, with CREATE_NO_WINDOW there is no console at
all. Whether that matters at a given site depends on who starts the PARENT, which no static
scan can answer. So the output here is "this site states a policy" / "this site inherits
whatever it is given", and the judgement of which sites must state one is a human's.
"""
from __future__ import annotations

import ast
import io
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: `subprocess.X(...)` that starts a child. `getoutput`/`getstatusoutput` go through the
#: shell and are included for that reason.
_SUBPROCESS_STARTERS = frozenset({
    "Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput",
})

#: `os.X(...)` that starts a child.
_OS_STARTERS = frozenset({
    "system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe", "startfile",
})

#: Modules whose job IS to launch, so a call into them is a site that has already decided.
_WRAPPERS = frozenset({"childproc"})


def tracked_python(repo=REPO, include_tests: bool = False) -> list:
    """Tracked .py paths, from git. NEVER a filesystem walk.

    The working tree carries large untracked and gitignored trees, and a walk both hangs and
    reports files that are not part of the repository -- which is the difference between a
    local result and what CI will see.
    """
    # DECIDED, NOT BASELINED -- and this module's own launch is how the ratchet first proved
    # it works. The baseline was generated while this file was still untracked, and
    # `tracked_python` enumerates through `git ls-files`, so the scanner could not see itself:
    # committing it made its own subprocess.run appear as a new undecided site and CI went red
    # on the very test it belongs to. A scanner that cannot see itself until it is committed
    # is worth knowing about; the answer here is the one the failure message asks for, which
    # is to state a policy rather than to add a row. `git ls-files` prompts for nothing and
    # its output is captured, so it is the unattended case headless_creationflags is for.
    from tools import childproc
    out = subprocess.run(["git", "-C", repo, "ls-files", "*.py"],
                         capture_output=True,
                         creationflags=childproc.headless_creationflags())
    rels = [l.strip() for l in (out.stdout or b"").decode("utf-8", "replace").splitlines()
            if l.strip()]
    if include_tests:
        return sorted(rels)
    return sorted(r for r in rels if not _is_test(r))


def _is_test(rel: str) -> bool:
    base = os.path.basename(rel)
    return base.startswith("test_") or base.endswith("_test.py")


def _callee(node: ast.Call) -> str:
    """`subprocess.Popen` / `os.system` / `childproc.run`, or "" for anything else."""
    fn = node.func
    if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
        return ""
    mod, attr = fn.value.id, fn.attr
    if mod == "subprocess" and attr in _SUBPROCESS_STARTERS:
        return "subprocess.%s" % attr
    if mod == "os" and attr in _OS_STARTERS:
        return "os.%s" % attr
    if mod in _WRAPPERS:
        return "%s.%s" % (mod, attr)
    return ""


def _enclosing(tree: ast.AST) -> dict:
    """node -> nearest enclosing def/class name, or "<module>"."""
    owner = {}
    def walk(node, name):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, child.name)
            else:
                owner[child] = name
                walk(child, name)
    walk(tree, "<module>")
    return owner


def scan_file(rel: str, repo=REPO) -> list:
    """Launch sites in one file, as dicts. Unparseable files yield nothing, not an error."""
    path = os.path.join(repo, rel)
    try:
        src = io.open(path, encoding="utf-8", errors="replace").read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []
    owner = _enclosing(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node)
        if not callee:
            continue
        kw = set(k.arg for k in node.keywords if k.arg)
        found.append({
            "rel": rel,
            "func": owner.get(node, "<module>"),
            "callee": callee,
            "line": node.lineno,
            # A call into a launching wrapper has already made the decision somewhere the
            # wrapper owns, so it is never "inherits whatever it is given".
            "decided": callee.split(".")[0] in _WRAPPERS or "creationflags" in kw,
            "shell": any(k.arg == "shell" for k in node.keywords),
        })
    return found


def key(site: dict) -> str:
    """Identity of a launch SITE, stable across edits above it.

    Not the line number, which moves. Not the bare path, which cannot tell two launches in
    one file apart. `path::function::callee` survives everything except renaming the function
    or moving the call, both of which are edits to the site itself.
    """
    return "%s::%s::%s" % (site["rel"], site["func"], site["callee"])


def inventory(repo=REPO, include_tests: bool = False) -> dict:
    """{key: count} for every undecided launch site. Counted, because one function may
    legitimately hold several and a set would silently absorb a new one."""
    counts = {}
    for rel in tracked_python(repo, include_tests=include_tests):
        for site in scan_file(rel, repo):
            if site["decided"]:
                continue
            counts[key(site)] = counts.get(key(site), 0) + 1
    return counts


if __name__ == "__main__":                                   # pragma: no cover - by hand
    inv = inventory()
    for k in sorted(inv):
        print("%3d  %s" % (inv[k], k))
    print("\n%d undecided launch sites in %d keys"
          % (sum(inv.values()), len(inv)))
