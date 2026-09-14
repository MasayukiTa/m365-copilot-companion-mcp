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


class _Rows(list):
    """The scan's rows, plus the names it could not attribute.

    A plain list everywhere it is already used -- `len()`, indexing, iteration and the frozen
    comparison all read unchanged -- with `.ambiguous` beside it, because a name the tool
    refused to judge is exactly as invisible as a row it never printed if nothing carries it
    out.
    """

    ambiguous: list = []


def _without(tree, dead_names):
    """`tree` with the named top-level functions removed, for the next counting pass.

    PRUNING THE TREE RATHER THAN RESTRUCTURING THE WALK. The counting phase is delicate --
    aliases, decorators, attribution -- and it has already shipped two bugs this week that only
    measurement caught. Handing it a tree with the dead functions taken out leaves every one of
    those rules reading exactly as it did, and the change is one list comprehension instead of a
    condition threaded through four loops.

    Imports inside a removed function go with it, which is correct: a dead function's import is
    not a live reference to anything.
    """
    if not dead_names:
        return tree
    kept = [n for n in tree.body
            if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name in dead_names)]
    if len(kept) == len(tree.body):
        return tree
    return ast.Module(body=kept, type_ignores=[])


def _module_of(dotted, files):
    """'relay.turn_outcome' -> 'relay/turn_outcome.py', when that file is one we scanned."""
    if not dotted:
        return None
    cand = dotted.replace(".", "/") + ".py"
    if cand in files:
        return cand
    cand = dotted.replace(".", "/") + "/__init__.py"
    return cand if cand in files else None


def _attribute_calls(rel, tree, files, qualified, unattributable):
    """Credit each CALL in `rel` to the module the AST says it names, or record that it cannot.

    ONLY CALL POSITIONS, and that is the difference between a rule worth having and one that is
    not. Counting every `ast.Name`/`ast.Attribute` -- which is what the bare-name count above
    does, correctly, for its own purpose -- puts every local variable and attribute that happens
    to share a spelling into the "cannot attribute" bucket, and 411 of 421 definitions stay
    ambiguous. Measured 2026-09-14:

        every Name/Attribute                                411 ambiguous,  3 reportable
        call positions only                                 401,           11
        + self./cls. cannot reach a module-level function   401,           11
        + a bare call resolves to a definition HERE         103,           28

    The last rule is ordinary Python scoping rather than a heuristic: a bare `summarise(...)`
    inside a file that defines `summarise` reaches THAT one, and says nothing about anyone
    else's. Five of the six files calling a bare `summarise` define their own.
    """
    mod_alias, func_from = {}, {}
    star = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                mod_alias[al.asname or al.name.split(".")[0]] = al.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            for al in node.names:
                if al.name == "*":
                    star = True           # cannot know what it bound; see below
                    continue
                # `from relay import turn_outcome` binds a MODULE and
                # `from relay.turn_outcome import summarise` binds a FUNCTION. They are the same
                # node shape, so both readings are recorded and whichever resolves to a real
                # file is the one used.
                # THE ALIAS IS THE LOCAL NAME; THE CREDIT BELONGS TO THE ORIGINAL. Keying the
                # credit on `al.asname` reintroduced the blind spot this file already fixed once:
                # `from relay.profile_token import capture_fn as _choose_capture` followed by
                # `_choose_capture()` credited `_choose_capture`, which nothing defines, and
                # reported `profile_token::capture_fn` as unreached while relay_fleet.py:1323
                # called it. So the map stores (module, ORIGINAL name) and the call site credits
                # that.
                mod_alias[al.asname or al.name] = (base + "." + al.name) if base else al.name
                func_from[al.asname or al.name] = (base, al.name)

    local = {n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    # A REGISTRATION IS A USE, AND IT IS NOT A CALL. `main.py` does
    # `from tools.data_ops import read_json` and then lists `read_json` in the TOOLS tuple; the
    # gateway calls it later, by dispatch. Crediting only call positions reported read_json,
    # write_json, restore_point, roll_back and edit_and_verify as unreached -- five tools the
    # server registers. So any reference the AST can ATTRIBUTE counts as reaching that
    # definition, whether or not it is a call.
    #
    # The ambiguity bucket below stays CALL-ONLY, deliberately: it is what decides whether a
    # name is reportable at all, and widening it to every Name/Attribute is the version measured
    # at 411 ambiguous / 3 reportable. Crediting widely and doubting narrowly is the safe pair --
    # it can only produce false "reached", which is the error this file already prefers to make
    # visible rather than silent.
    # The callee of a Call is skipped here and credited by the call pass below, or the same
    # reference would be counted twice. The counts are only ever read as booleans, but a number
    # that is wrong by construction is the kind of thing the next person builds a threshold on.
    callees = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if id(node) in callees:
            continue
        if isinstance(node, ast.Name) and not isinstance(getattr(node, "ctx", None), ast.Store):
            src = func_from.get(node.id)
            target = _module_of(src[0], files) if src else None
            if target:
                qualified[(target, src[1])] += 1
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id not in ("self", "cls"):
                target = _module_of(mod_alias.get(node.value.id), files)
                if target:
                    qualified[(target, node.attr)] += 1

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            base = fn.value
            if isinstance(base, ast.Name) and base.id in ("self", "cls"):
                continue                  # a method call cannot reach a module-level function
            target = _module_of(mod_alias.get(getattr(base, "id", "")), files) \
                if isinstance(base, ast.Name) else None
            if target:
                qualified[(target, fn.attr)] += 1
            else:
                unattributable[fn.attr] += 1
        elif isinstance(fn, ast.Name):
            src = func_from.get(fn.id)
            target = _module_of(src[0], files) if src else None
            if target:
                qualified[(target, src[1])] += 1
            elif fn.id in local:
                qualified[(rel, fn.id)] += 1
            else:
                unattributable[fn.id] += 1

    if star:
        # A star import binds names this scan cannot enumerate, so every call in this file could
        # have meant one of them. Recorded as ambiguity rather than guessed at.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                nm = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if nm:
                    unattributable[nm] += 1


def scan(files=None, iterate=True):
    """[(key, name, rel, lineno, span, test_refs)] sorted biggest-first.

    `key` is "path::name" -- a bare name would collide across modules, and a line number would
    move on every edit above it.

    `iterate=False` asks the ATTRIBUTION question alone: does any line reach this name, counting
    aliased imports, shell callers and registry decorators? `iterate=True` (the default, and the
    inventory's question) additionally drops what has been reported and counts again, so a
    function whose only caller is itself unreached is reported too.

    THE TWO ARE DIFFERENT QUESTIONS AND A TEST HAS TO SAY WHICH IT IS ASKING. Three tests failed
    when the iteration landed, all of them about attribution: the alias test scans a two-file
    slice in which `solve_policy::plan_solve` -- the aliased caller -- has no caller of its own,
    so the whole chain is legitimately dead and `diversify` is reported for a reason that has
    nothing to do with aliases. Inverting such a test would make it pass while the alias credit
    it exists to protect was broken.
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

    # ITERATED TO A FIXED POINT. A reference count is not a reachability analysis: a function
    # called only by ANOTHER unreached function counts as reached, so a cluster of mutually dead
    # code shows only its entry point. Three were confirmed by hand before this was built --
    # compare::versions_differ behind transport_versions_differ, fleet_toolset::mode behind
    # check, and coding_ops::worktree_add / worktree_remove behind worktree_scope.
    #
    # So: report, drop what was reported, count again, repeat. Measured on this repository it
    # settles in four rounds and finds about 15% more. The bound is a guard against a bug in
    # this loop rather than a property of the data.
    #
    # `main` is in PROTOCOL and is therefore never reported, so an entrypoint can never be
    # dropped and seed the iteration -- which is what keeps `bench/retry_floor.py::report`,
    # reached only from its own `main()`, correctly out of this.
    dead, rows, ambiguous = set(), [], []
    for _round in range(12):
        active = {rel: _without(tree, {k.split("::", 1)[1] for k in dead
                                       if k.startswith(rel + "::")})
                  for rel, tree in trees.items()}
        prod_refs = defaultdict(int)
        test_refs = defaultdict(int)
        qualified = defaultdict(int)        # (rel, name) -> calls the AST could attribute to it
        unattributable = defaultdict(int)   # name -> calls it could not
        for rel, tree in active.items():
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

            # WHICH MODULE A CALL MEANT, where the AST can say. Used ONLY to resolve a name defined
            # in more than one place (see the `continue` below); the counts above still decide
            # every single-definition case, unchanged.
            if bucket is prod_refs:
                _attribute_calls(rel, tree, files, qualified, unattributable)

        # A CALLER THAT IS NOT PYTHON IS STILL A CALLER. Read once, not per name: this is a
        # handful of small shell scripts, and a scan per candidate would re-read them ninety times.
        cross = cross_language_text()

        rows = []
        ambiguous = []
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
            # ATTRIBUTE BEFORE GIVING UP. A bare-name count cannot say which definition a call
            # meant; the AST usually can. Measured 2026-09-14: 421 definitions sat behind this
            # branch, and attributing them reports more while leaving the rest genuinely ambiguous.
            # See `_attribute_calls` for the rule, and for why the obvious version of it -- counting
            # every Name/Attribute rather than call positions -- would have found three.
            #
            # `resolved` EXISTS BECAUSE THE FIRST VERSION OF THIS WAS A NO-OP: it filtered `places`
            # and then fell through to `if prod_refs[name]: continue`, which is true by construction
            # for every name that reaches here. The scan reported exactly the same 68 names and the
            # whole attribution was thrown away one line later.
            resolved = False
            if len(places) != 1 and prod_refs[name]:
                if unattributable[name]:
                    ambiguous.append(name)    # printed, not silent: silence is the defect here
                    continue
                places = [(rel, lineno, span) for (rel, lineno, span) in places
                          if not qualified[(rel, name)]]
                if not places:
                    continue
                resolved = True
            if not resolved and prod_refs[name]:
                continue
            for rel, lineno, span in places:
                if cross and reached_from_shell(name, rel, cross):
                    continue                  # reached from a .ps1/.bat wrapper
                if decorated.get("%s::%s" % (rel, name)):
                    continue                  # handed to a registry by a decorator
                rows.append(("%s::%s" % (rel, name), name, rel, lineno, span, test_refs[name]))
        found = {r[0] for r in rows}
        if not iterate or found == dead:
            break
        dead = found
    rows.sort(key=lambda r: (-r[4], r[0]))
    # WHAT THE TOOL COULD NOT DECIDE, HANDED BACK RATHER THAN DROPPED. A name skipped for
    # ambiguity is the same shape as a row nobody prints -- which is the defect this whole file
    # exists to stop -- so `scan()` carries the list as an attribute of its result. An attribute
    # rather than a second return value because every existing caller unpacks six-tuples, and a
    # changed signature would be a wider edit than the fact is worth.
    try:
        rows = _Rows(rows)
        rows.ambiguous = sorted(set(ambiguous))
    except Exception:                     # pragma: no cover - a list subclass cannot fail here
        pass
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
    # SAY WHAT COULD NOT BE DECIDED. These are names defined in several modules where at least
    # one call could not be attributed to any of them -- a bare call the AST cannot resolve, or
    # a star import. They are neither reached nor reported, and until 2026-09-14 that state was
    # silent, which is the failure this whole tool is about.
    unresolved = getattr(rows, "ambiguous", [])
    if unresolved:
        print()
        print("defined in more than one module and not attributable (%d): %s"
              % (len(unresolved), ", ".join(unresolved)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
