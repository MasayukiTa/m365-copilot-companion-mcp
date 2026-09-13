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
import re
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


#: Tracked files that are not Python but can still call into it -- PowerShell and batch
#: wrappers reaching a function through `python -c "from M import f; ..."`, which is how
#: edge_keeper.ps1 reads the managed-profile list and how three run_*.ps1 scripts call
#: bench/ui_goal_lines.py::write_ui_file. Without these the scanner reports a wired function
#: as unreached, and the only way to clear it would be a permanent exemption -- the mechanism
#: this inventory exists to dismantle.
CROSS_LANGUAGE_GLOBS = ("*.ps1", "*.bat", "*.cmd")


def cross_language_text():
    """Each tracked non-Python caller's text, as a list, or [] when git cannot answer.

    PER FILE, NOT CONCATENATED, because the test below is that ONE file names both the
    function and its module. Joining them all first would let one script's mention of a
    function pair with a different script's mention of the module.
    """
    parts = []
    for glob in CROSS_LANGUAGE_GLOBS:
        try:
            out = subprocess.run(["git", "-C", REPO, "ls-files", glob],
                                 capture_output=True, timeout=60)
        except OSError:
            return ""
        if out.returncode != 0:
            return ""
        for raw in out.stdout.decode("utf-8", "replace").splitlines():
            rel = raw.strip()
            if not rel:
                continue
            try:
                with io.open(os.path.join(REPO, rel), encoding="utf-8",
                             errors="replace") as fh:
                    parts.append(fh.read())
            except OSError:
                continue
    return parts


def reached_from_shell(name, rel, texts):
    """Whether a tracked .ps1/.bat plausibly calls `name`, defined in `rel`.

    BOTH THE FUNCTION AND ITS MODULE, IN THE SAME FILE. A bare word-boundary match on the name
    was the first version of this, and it was wrong three times out of six on the day it was
    written: "require" (the autonomy gate an adversarial review had just flagged as unwired),
    "branches" (shell scripts talk about git branches) and "health" (a health-check script
    mentions the endpoint). A false "reached" is worse than a false "unreached" -- it removes
    the row, and nobody reads what is not printed.

    A real cross-language call carries both names:

        & $py -c "from relay.edge_recover import keeper_profile_marker as k; print(k())"

    so requiring the module keeps the genuine cases and drops the coincidences.
    """
    mod = rel.rsplit("/", 1)[-1]
    if mod.endswith(".py"):
        mod = mod[:-3]
    if not mod or mod in PROTOCOL:
        return False
    fn_re = re.compile(r"\b%s\b" % re.escape(name))
    mod_re = re.compile(r"\b%s\b" % re.escape(mod))
    for text in texts:
        if fn_re.search(text) and mod_re.search(text):
            return True
    return False


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
    # REGISTERED, NOT CALLED. A decorator that is itself a CALL hands the function to something
    # -- `@mcp.custom_route("/health", methods=["GET"])` puts it in Starlette's routing table at
    # import time, and no Python line ever names it again. That is a call site this scan cannot
    # represent, and unlike a word in a shell script it is a fact about the code. A bare-name
    # decorator (@property, @staticmethod) does NOT count: it transforms the function rather
    # than handing it to a registry.
    decorated = {}
    for rel, tree in trees.items():
        if is_test(rel):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(isinstance(d, ast.Call) for d in node.decorator_list):
                    decorated["%s::%s" % (rel, node.name)] = True

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

    # A CALLER THAT IS NOT PYTHON IS STILL A CALLER. Read once, not per name: this is a
    # handful of small shell scripts, and a scan per candidate would re-read them ninety times.
    cross = cross_language_text()

    rows = []
    for name, places in defs.items():
        # A NAME DEFINED TWICE USED TO DISAPPEAR, WHICH IS THE WORST OF THE THREE OUTCOMES.
        # The reference count is by bare name, so with two definitions it cannot say WHICH one
        # a call reached -- and the answer was to drop the name entirely. Measured 2026-09-13:
        # adding a function called `require` to relay/invariants.py silently removed
        # `relay/selfimprove/autonomy.py::require` -- the hard autonomy gate an adversarial
        # review had flagged -- from the inventory. A new function's NAME could retire an
        # existing finding, and nothing said so.
        #
        # ZERO REFERENCES RESOLVES IT WITHOUT RESOLVING THE NAME. If the count is zero, no
        # definition of that name is reached, whichever one a call would have meant, so every
        # place is reported. Only a non-zero count is genuinely ambiguous, and that is the one
        # case still skipped. Same rule as the rest of this file: prefer noise over silence,
        # because a false "reached" is a row nobody ever sees.
        if len(places) != 1 and prod_refs[name]:
            continue                      # some definition IS reached; a name count cannot say which
        if prod_refs[name]:
            continue
        for rel, lineno, span in places:
            if cross and reached_from_shell(name, rel, cross):
                continue                  # reached from a .ps1/.bat wrapper
            if decorated.get("%s::%s" % (rel, name)):
                continue                  # handed to a registry by a decorator
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
