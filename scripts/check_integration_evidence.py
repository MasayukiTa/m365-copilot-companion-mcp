"""A new public definition has to be reachable from somewhere. Checked per change, not per repo.

WHY THIS EXISTS. A unit test proves a function does what it says. It does not prove anything
calls it, and three capabilities in one day were complete, tested, green -- and wired to
nothing. The approval gate that never ran, the reaper that was never invoked, the resume
decision nothing consulted. Each had passing tests. Each did nothing.

WHY IT IS SCOPED TO THE DIFF. The obvious version of this is a whole-repository dead-code
audit, and that was tried: it produced 43 findings of which 1 was real. At that precision
nobody reads the output, and a check nobody reads is the thing this repository keeps
rediscovering as the actual failure -- the leaked page was DETECTED at 22:40 and sat for nine
and a half hours because the detector wrote to a file no one was obliged to open.

So this asks one question about one commit: for each public definition this change ADDS, is
there a reference to it from outside the module that defines it? Any of the three counts --

    a caller in ordinary code        the capability is used
    a registration point             a table, a CLI flag, a manifest entry
    a test that exercises it         end-to-end, from the outside

-- because all three are evidence that somebody connected it to something. What fails is a
definition mentioned nowhere but where it was written.

    python scripts/check_integration_evidence.py [--base HEAD~1]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCEPTIONS = os.path.join(ROOT, ".integration_exceptions.json")

#: Where a definition has to live for this to apply. Scratch directories, one-off analysis
#: scripts and the fleet's working area are not capabilities anybody wires up.
WATCHED = ("relay/", "tools/", "scripts/win/")

#: Names that are reachable by convention rather than by reference: an entry point the runtime
#: calls, a pytest function, a dunder. Listing them is not a loophole -- each is a REGISTRATION
#: POINT of a kind a grep cannot see.
CONVENTIONAL = re.compile(r"^(main|test_|_|handle_|cmd_|on_)")


def _git(*args, base=None):
    """git output as a string, ALWAYS a string. `.stdout` can come back None -- it did, under
    pytest -- and a checker that crashes is a checker that gets disabled."""
    try:
        r = subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True,
                           text=True, timeout=120)
        return r.stdout or ""
    except Exception:
        return ""


def added_definitions(base: str):
    """Public module-level defs and classes this change adds, as {name: path}.

    Read from the FILE as it stands now rather than from the diff text, so a definition moved
    or re-indented does not read as new. The diff only says which files to look at and which
    lines are new.
    """
    out = {}
    diff = _git("diff", "--unified=0", base, "--", *WATCHED)
    if not diff.strip():
        return out
    current, added_lines = None, {}
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            added_lines[current] = set()
        elif line.startswith("@@") and current:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                for i in range(start, start + max(1, int(m.group(2) or 1))):
                    added_lines[current].add(i)
    for path, lines in added_lines.items():
        if not path.endswith(".py") or not lines:
            continue
        full = os.path.join(ROOT, path)
        if not os.path.isfile(full):
            continue
        try:
            tree = ast.parse(open(full, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in tree.body:                      # module level only
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if node.lineno not in lines:
                continue
            if CONVENTIONAL.match(node.name):
                continue
            out[node.name] = path
    return out


def _imports(tree, defining_path: str) -> bool:
    """Does this file import the module `defining_path` defines?"""
    mod = defining_path.replace(chr(92), "/")[:-3].replace("/", ".")
    tail = mod.rsplit(".", 1)[-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == mod or a.name.endswith("." + tail) or a.name == tail:
                    return True
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base == mod or base.endswith("." + tail) or base == tail:
                return True
            for a in node.names:
                if a.name == tail:
                    return True
    return False


def references(name: str, defining_path: str):
    """Where `name` is actually USED, as (path, how). Empty means nothing reaches it.

    CONFIRMED BY PARSING, NOT BY GREPPING. The first version took `git grep -w` at its word
    and reported `claim` as wired because the string appears in a memory template's JSON --
    a different `claim` entirely. A short, ordinary name will always find a textual hit
    somewhere, so a check built on grep alone passes hardest exactly where it is needed most.

    A use inside the defining module COUNTS. The question is whether the capability is
    reachable, not whether it is exported: a helper that only `main()` calls is wired up. What
    fails is a definition nothing mentions anywhere, including its own file.
    """
    try:
        raw = subprocess.run(["git", "grep", "-l", "-w", "--", name],
                             cwd=ROOT, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return []
    found = []
    for path in [p.strip().replace(chr(92), "/") for p in raw.splitlines() if p.strip()]:
        full = os.path.join(ROOT, path)
        if not os.path.isfile(full):
            continue
        if path.endswith(".py"):
            try:
                tree = ast.parse(open(full, encoding="utf-8").read())
            except (SyntaxError, OSError):
                continue
            own = path == defining_path.replace(chr(92), "/")
            # THE NAME ALONE IS NOT THE SYMBOL. `claim` appears in half a dozen unrelated
            # modules; a use of some other `claim` is not evidence that ownership.claim is
            # wired to anything. A reference counts only from a file that IMPORTS the module
            # the definition lives in -- or from that module itself.
            if not own and not _imports(tree, defining_path):
                continue
            used = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == name:
                    used = True
                elif isinstance(node, ast.Attribute) and node.attr == name:
                    used = True
                elif isinstance(node, ast.alias) and (node.name == name or node.asname == name):
                    used = True
                if used:
                    break
            if used:
                found.append((path, "used in its own module" if own else "used"))
        elif path.endswith((".ps1", ".yml", ".yaml", ".cmd", ".bat")):
            # A launcher or a workflow naming it IS the registration point -- but only if it
            # names the MODULE too. `release` matched a release-packaging workflow that has
            # nothing to do with the ownership ledger, and a launcher that really registers a
            # capability always says which file it is running.
            try:
                text = open(full, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            # AS A PATH COMPONENT, not as a substring. `lean_capture.py` matches inside
            # `test_lean_capture.py`, so registering a TEST in the CI workflow read as
            # registering the module it tests.
            leaf = re.escape(os.path.basename(defining_path))
            boundary = "(^|[/" + re.escape(chr(92)) + chr(92) + "s\"'])"
            if re.search(boundary + leaf, text, re.M):
                found.append((path, "registered"))
    return found


def load_exceptions():
    try:
        return json.load(open(EXCEPTIONS, encoding="utf-8"))
    except OSError:
        return {}
    except ValueError as e:
        print("!! %s is not valid JSON: %s" % (EXCEPTIONS, e))
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=os.environ.get("INTEGRATION_BASE", "HEAD~1"))
    args = ap.parse_args(argv)

    # A SHALLOW CLONE HAS NO HEAD~1, and a check that silently reports "no new definitions"
    # on every CI run is worse than no check: it is a green tick that means nothing. Say so.
    if not _git("rev-parse", "--verify", "--quiet", args.base + "^{commit}").strip():
        print("integration evidence: SKIPPED -- cannot resolve base %r." % args.base)
        print("  On CI this means the checkout is shallower than the range being asked "
              "about (fetch-depth).")
        return 0

    exceptions = load_exceptions()
    added = added_definitions(args.base)
    if not added:
        print("integration evidence: no new public definitions in %s..HEAD" % args.base)
        return 0

    unwired = []
    for name, path in sorted(added.items()):
        if name in exceptions:
            print("  [skip] %-34s %s" % (name, exceptions[name]))
            continue
        refs = references(name, path)
        if refs:
            print("  [ok]   %-34s %s%s"
                  % (name, ", ".join("%s (%s)" % r for r in refs[:2]),
                     " ..." if len(refs) > 2 else ""))
        else:
            unwired.append((name, path))

    if unwired:
        print("\nNEW DEFINITIONS NOTHING REFERENCES:")
        for name, path in unwired:
            print("  %s  (%s)" % (name, path))
        print("\nEach needs one of: a caller, a registration point, or a test that exercises\n"
              "it from outside. If it is deliberately unreferenced, add it to\n"
              ".integration_exceptions.json with the reason.")
        return 1
    print("integration evidence OK: %d new definition(s), all reachable" % len(added))
    return 0


if __name__ == "__main__":
    sys.exit(main())
