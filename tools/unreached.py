# -*- coding: utf-8 -*-
"""Which module-level public functions have no caller anywhere in non-test code.

THE REPOSITORY'S SIGNATURE DEFECT, found one at a time and each time expensively:

  * looped-transformer step 0            -- implemented, registered, 0 calls
  * the four fan-in functions            -- 0 callers, campaign ledger idle
  * relay/turn_outcome.py                -- 147 lines, 0 callers outside tests
  * relay/supervisor_verify.py           -- reachable only from a shadow bench path
  * contract_gate `budget_turns`         -- the reader existed, the writer had no parameter,
                                            so the branch was structurally unreachable
  * socket_driver.conversation_ids       -- its own docstring said "NOT PERSISTENCE, just the
                                            ability to be asked"; nothing asked, and a fleet
                                            conversation could not be continued for weeks
  * tools/childproc.run_ok               -- added by me on 2026-09-12 and unreferenced within
                                            the hour. Deleted rather than listed.

Each of those was a separate investigation. This measures the class instead of the instance.

WHAT IT COUNTS. A module-level `def` whose name is not `_private` and is never referenced --
as a call, an attribute, or a bare value handed to something else -- in any non-test file,
including the one that defines it and the repository root (`main.py` imports every MCP tool by
name; leaving the root out is what made a first version report 229 perfectly-wired tools).

WHAT IT CANNOT SEE, stated rather than discovered later:
  * a name reached through `getattr(obj, "name")` or a table of handler strings
  * a name that shadows a stdlib or library method, which reads as used from any call on any
    object
  * class methods -- excluded entirely, because instances make a name-based count meaningless
So this is a LIST TO LOOK AT. A row is a question ("what calls this?"), not a verdict, and the
answer is as often "delete it" as "wire it".
"""
from __future__ import annotations

import argparse
import ast
import io
import os
import subprocess
import sys
import warnings
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOTS = ("relay", "bench", "tools", "bridge", "scripts")
SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}

#: Names something other than this repository calls: the interpreter, BaseHTTPRequestHandler,
#: pytest, the file protocol. A count over these says nothing.
PROTOCOL = frozenset({"main", "run", "setup", "teardown", "do_GET", "do_POST", "log_message",
                      "handle", "close", "write", "read", "flush"})


def tracked_files():
    """The .py files git actually has, or None when git cannot answer.

    TRACKED, NOT WALKED. An inventory built from the working tree disagrees with CI in both
    directions -- a locally-excluded file exists here and not there, and an uncommitted change
    makes the count differ from HEAD. Both happened on 2026-09-12 and both turned CI red.
    """
    try:
        out = subprocess.run(["git", "-C", REPO, "ls-files", "*.py"],
                             capture_output=True, timeout=60)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    files = []
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        rel = line.strip().replace("\\", "/")
        if not rel:
            continue
        top = rel.split("/")[0]
        if "/" in rel and top not in ROOTS:
            continue                      # only the named roots, plus the repo root itself
        if any(part in SKIP_DIRS or part.startswith("wt_") for part in rel.split("/")[:-1]):
            continue
        files.append(rel)
    return files


def is_test(rel):
    base = rel.rsplit("/", 1)[-1]
    return base.startswith("test_") or base == "conftest.py"


def scan(files=None):
    """[(key, name, rel, lineno, span, test_refs)] sorted biggest-first.

    `key` is "path::name" -- a bare name would collide across modules, and a line number would
    move on every edit above it.
    """
    files = tracked_files() if files is None else files
    if files is None:
        return None
    trees = {}
    # PARSING IS DIAGNOSTIC AND ITS WARNINGS BELONG TO THE FILES, NOT TO THE CALLER. Compiling
    # the tree raises DeprecationWarning for every invalid escape sequence in the repository
    # (53 of them today), which would drown whatever a test runner is actually reporting. They
    # are real latent defects -- \C and \S become errors in a future Python -- but this is
    # not the instrument that should be reporting them.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.simplefilter("ignore", SyntaxWarning)
        for rel in files:
            try:
                trees[rel] = ast.parse(
                    io.open(os.path.join(REPO, rel), encoding="utf-8").read())
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue

    defs = defaultdict(list)
    for rel, tree in trees.items():
        if is_test(rel):
            continue
        for node in tree.body:            # module level only; methods are excluded
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_") or node.name in PROTOCOL:
                    continue
                end = getattr(node, "end_lineno", node.lineno) or node.lineno
                defs[node.name].append((rel, node.lineno, end - node.lineno + 1))

    prod_refs = defaultdict(int)
    test_refs = defaultdict(int)
    for rel, tree in trees.items():
        bucket = test_refs if is_test(rel) else prod_refs
        # A CALL MADE UNDER AN ALIAS IS STILL A CALL, and this walk could not see one.
        # References are counted as `ast.Name` / `ast.Attribute`, and an aliased import is
        # neither -- it is `ast.alias(name=..., asname=...)`. So
        #     from relay.selfimprove.diversify import diversify as _diversify
        #     genomes = _diversify(base, n)
        # recorded the Name `_diversify` and credited `diversify` with nothing. Measured
        # 2026-09-13: that exact function sat in this repository's unreached BASELINE, frozen
        # as known-dead, while relay/solve_policy.py:56 called it in production. An inventory
        # with false entries cannot justify deleting anything.
        #
        # AN IMPORT IS STILL NOT A CALL. Only a USE of the alias credits the original name;
        # `from M import f as g` with `g` never used credits nothing. Counting the import
        # itself would hide the very defect this tool exists to find -- a function imported by
        # its tests and called by no one, which is what campaigns_from_ledger was.
        # CREDITED ONLY WHERE THE ALIAS IS CALLED, not merely referenced -- and the first
        # version of this fix got that wrong, which is why the rule is spelled out. Crediting
        # every reference hid `relay/selfimprove/harness_tree.py::branches`, because
        # `from relay.selfimprove import branches as BR` imports a MODULE that happens to
        # share the function's name. `from X import y as z` cannot be told from a module
        # import syntactically -- both are ast.alias -- so the discriminator is USE: a
        # function alias gets called (`_diversify(base, n)`), a module alias gets attributed
        # (`BR.something()`). Widening a blind spot into a blind eye is the worse trade: a
        # false negative here is a live unreached function that never appears at all.
        aliased = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for al in node.names:
                    if al.asname and al.name:
                        aliased[al.asname] = al.name.rsplit(".", 1)[-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                bucket[node.id] += 1
            elif isinstance(node, ast.Attribute):
                bucket[node.attr] += 1
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                orig = aliased.get(node.func.id)
                if orig:
                    bucket[orig] += 1

    rows = []
    for name, places in defs.items():
        if len(places) != 1:
            continue                      # defined in two modules: a name count cannot resolve
        rel, lineno, span = places[0]
        if prod_refs[name]:
            continue
        rows.append(("%s::%s" % (rel, name), name, rel, lineno, span, test_refs[name]))
    rows.sort(key=lambda r: (-r[4], r[0]))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=0, help="print only the N largest (0 = all)")
    a = ap.parse_args(argv)
    rows = scan()
    if rows is None:
        print("git could not list the tracked files; nothing measured")
        return 1
    print("module-level public functions with no reference in non-test code: %d" % len(rows))
    print()
    print("%-6s %-42s %-46s %s" % ("lines", "name", "file:line", "refs in tests"))
    for key, name, rel, lineno, span, tref in (rows[:a.limit] if a.limit else rows):
        print("%-6d %-42s %-46s %d" % (span, name, "%s:%d" % (rel, lineno), tref))
    return 0


if __name__ == "__main__":
    sys.exit(main())
