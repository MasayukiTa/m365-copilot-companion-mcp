"""folder_coder.py -- turn a folder + one instruction into a fleet of coding GOALS.

This is the "point at a folder, run coding tasks" front door (Codex / claude-code
style) for the relay fleet. You give it a repository folder and a single high-level
instruction in plain language; it walks the tree, picks the relevant source files,
and emits a list of concrete GOAL strings -- the exact line-per-goal format that
`relay.fleet_runner --goals-file <file>` consumes.

Why this is useful: the fleet drives the M365 Copilot IMPLEMENTATION agent, which
DOES have the project's MCP connector tools (read_file / write_file /
replace_in_file / list_directory / grep / run_python ...). So a goal is not just a
prompt -- it can ask Copilot to actually open and EDIT files. This module just turns
one instruction into many such goals so they run in parallel, one tab per file.

Three modes:
  * "per-file"  -- one EDIT goal per file (read + replace_in_file / write_file).
  * "review"    -- one READ-ONLY review goal per file (no edits, bullet findings).
  * "single"    -- exactly one goal for the whole folder (Copilot explores itself).

Typical flow (this module only PLANS; it never launches the fleet):

  # 1. generate the goals file
  python -m relay.folder_coder --folder C:\\proj --instruction "型ヒントを追加" --mode per-file
  # 2. run it (printed for you at the end)
  python -m relay.fleet_runner --goals-file "C:\\proj\\.fleet_goals.txt"

stdlib only -- no third-party imports, so it runs anywhere the repo's Python does.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# allow running both as `python -m relay.folder_coder` and `python relay/folder_coder.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.coding_discipline import coding_discipline_text

# Directory names that are never interesting to a coding task -- vendored deps,
# build output, VCS internals, virtualenvs, and the fleet's own scratch dirs.
# Matched against every path component, so a nested node_modules is skipped too.
SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".fleet", ".setup",
})

# Default file extensions considered "code" when the caller does not pass exts.
DEFAULT_EXTS = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".cs", ".java", ".go", ".rb", ".rs",
    ".cpp", ".c", ".h", ".css", ".html", ".sql", ".sh", ".ps1", ".md",
    ".json", ".yaml", ".yml",
)

VALID_MODES = ("per-file", "review", "single")


def _normalize_exts(exts):
    """Coerce `exts` into a lowercased set of dotted extensions.

    Accepts None (-> DEFAULT_EXTS), a comma-separated string (".py,.js" or
    "py, js"), or any iterable of strings. Leading dots are added if missing and
    surrounding whitespace is stripped. Returns a set like {".py", ".js"}.
    """
    if exts is None:
        items = DEFAULT_EXTS
    elif isinstance(exts, str):
        items = exts.split(",")
    else:
        items = list(exts)
    out = set()
    for e in items:
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        out.add(e)
    return out


def scan_folder(folder, exts=None, max_files=200):
    """Walk `folder` and return relevant file paths, relative to `folder`, sorted.

    Skips any path that contains a SKIP_DIRS component (.git, node_modules,
    .venv, venv, __pycache__, dist, build, .fleet, .setup) and keeps only files
    whose extension is in `exts` (see `_normalize_exts`; defaults to DEFAULT_EXTS).

    Paths are returned with forward slashes so the same goals file reads cleanly
    regardless of OS. At most `max_files` paths are returned (after sorting, so
    the cut is deterministic).
    """
    wanted = _normalize_exts(exts)
    root = os.path.abspath(folder)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune skip dirs in place so os.walk never descends into them
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in wanted:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            # defensive: skip if any component still matches (e.g. odd symlinks)
            if any(part in SKIP_DIRS for part in rel.split("/")):
                continue
            found.append(rel)
    found.sort()
    if max_files is not None and max_files >= 0:
        found = found[:max_files]
    return found


# Goal templates. Japanese, because the user is Japanese and the Copilot impl
# agent is driven in Japanese. {folder}/{relpath}/{instruction} are filled in.
_TPL_PER_FILE = (
    "リポジトリ {folder} の {relpath} に対して次を実施してください: {instruction}。"
    "MCPツール(read_file / replace_in_file / write_file)で実際にファイルを編集し、"
    "変更が完了したら DONE と書いてください。"
    "安全に進められない・情報不足なら FAIL と理由を書いてください。"
)
_TPL_REVIEW = (
    "リポジトリ {folder} の {relpath} を read_file で読み、"
    "{instruction} の観点でレビューして指摘を箇条書きで返してください。終わったら DONE。"
)
_TPL_SINGLE = (
    "リポジトリ {folder} 全体に対して次を実施してください: {instruction}。"
    "必要なファイルを list_directory / grep / read_file で調べ、"
    "replace_in_file / write_file で編集してください。完了したら DONE、無理なら FAIL と理由。"
)


def _wrap(text, checks, cwd):
    """A goal is a plain string when it has no acceptance check (back-compat), or a
    dict {"text","checks","cwd"} when it does -- the shape fleet_runner parses from a
    goals-file JSON line and the verification gate (spec 3-3) consumes."""
    if checks:
        return {"text": text, "checks": checks, "cwd": cwd}
    return text


def generate_goals(folder, instruction, mode="per-file", exts=None, max_files=200,
                   verify=False, check_cmd=None, import_smoke=False):
    """Turn one folder + one instruction into a list of fleet GOALs.

    `mode` is one of:
      * "per-file" -- one EDIT goal per scanned file (the default).
      * "review"   -- one READ-ONLY review goal per scanned file.
      * "single"   -- exactly one goal covering the whole folder (no scan needed).

    Acceptance gate (spec 3-3): when `verify` is set, each per-file Python EDIT goal
    gets a `py_compile` check so a self-reported DONE is only accepted if the file still
    compiles (the frame proves the edit did not break the file). `check_cmd`, if given,
    attaches a shell check (e.g. "python -m pytest -q") to every EDIT goal (and to the
    single-mode goal) so DONE means the project command actually passed. Review goals are
    read-only, so they never carry checks. Goals with a check are emitted as dicts (JSON
    lines); plain goals stay strings.

    Returns a list of goals (str or dict). Raises ValueError on bad mode.
    """
    if mode not in VALID_MODES:
        raise ValueError("mode must be one of %s, got %r" % (VALID_MODES, mode))

    folder_disp = os.path.abspath(folder)

    if mode == "single":
        text = _TPL_SINGLE.format(folder=folder_disp, instruction=instruction)
        checks = [{"type": "shell", "cmd": check_cmd}] if check_cmd else []
        # The single mode IS a cross-cutting whole-repo task (same shape as code_task):
        # when it carries a verification gate, give it the same strengthened red->green /
        # minimal-and-complete discipline. Per-file and review modes are mechanical sweeps /
        # read-only, so they do NOT get it.
        if checks:
            text += coding_discipline_text(instruction)
        return [_wrap(text, checks, folder_disp)]

    tpl = _TPL_PER_FILE if mode == "per-file" else _TPL_REVIEW
    rels = scan_folder(folder, exts=exts, max_files=max_files)
    out = []
    for rel in rels:
        text = tpl.format(folder=folder_disp, relpath=rel, instruction=instruction)
        checks = []
        if mode == "per-file":          # review is read-only -> no acceptance check
            if verify and rel.lower().endswith(".py"):
                checks.append({"type": "py_compile", "path": rel})
            if import_smoke and rel.lower().endswith(".py"):
                checks.append({"type": "import_smoke", "path": rel})
            if check_cmd:
                checks.append({"type": "shell", "cmd": check_cmd})
        out.append(_wrap(text, checks, folder_disp))
    return out


def write_goals_file(goals, path):
    """Write `goals` one per line to `path` (UTF-8, no BOM). Returns `path`.

    The format matches what `fleet_runner --goals-file` expects: one goal per line. A
    plain-string goal is written verbatim; a goal that carries an acceptance check is a
    dict and is written as a single-line JSON object (fleet_runner detects the leading
    '{' and parses it). A trailing newline is written so the file ends cleanly.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for g in goals:
            if isinstance(g, dict):
                f.write(json.dumps(g, ensure_ascii=False) + "\n")
            else:
                f.write(g + "\n")
    return path


def _repo_root():
    """Absolute path to the repo root (one level above this relay/ package)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_out(folder):
    """Pick a sensible default --out: <folder>\\.fleet_goals.txt.

    Lives next to the target folder so it is easy to find and easy to delete; the
    name is distinctive enough not to collide with project files.
    """
    return os.path.join(os.path.abspath(folder), ".fleet_goals.txt")


def main():
    """CLI: scan a folder, print numbered goals, write a goals file, print next cmd.

    This PLANS only -- it never launches the fleet. After writing the goals file it
    prints the exact `python -m relay.fleet_runner --goals-file <out>` command to run.
    """
    ap = argparse.ArgumentParser(
        prog="relay.folder_coder",
        description="Turn a folder + one instruction into fleet GOALs "
                    "(point-at-a-folder coding, Codex/claude-code style).")
    ap.add_argument("--folder", required=True,
                    help="repository folder to scan / target")
    ap.add_argument("--instruction", required=True,
                    help="high-level instruction applied to each file / the folder")
    ap.add_argument("--mode", default="per-file", choices=VALID_MODES,
                    help="per-file (edit each), review (read-only each), "
                         "single (one goal for the whole folder)")
    ap.add_argument("--ext", default=None,
                    help="comma list of extensions to include "
                         "(e.g. .py,.ts). Default: common code extensions")
    ap.add_argument("--max", type=int, default=200,
                    help="max files to turn into goals (default 200)")
    ap.add_argument("--out", default=None,
                    help="goals file to write (default: <folder>\\.fleet_goals.txt)")
    ap.add_argument("--verify", action="store_true",
                    help="attach a py_compile acceptance check to each per-file Python "
                         "EDIT goal: a self-reported DONE is only accepted if the file "
                         "still compiles (spec 3-3 verification gate)")
    ap.add_argument("--check-cmd", default=None,
                    help="shell command run as an acceptance check on every EDIT goal "
                         '(and the single-mode goal), e.g. "python -m pytest -q". DONE is '
                         "accepted only if it exits 0; on failure the real output is fed back.")
    ap.add_argument("--import-smoke", action="store_true",
                    help="attach an import_smoke check to each per-file Python EDIT goal: "
                         "the module must actually IMPORT (catches load-time errors a "
                         "compile misses). Opt-in: importing runs module-level code and may "
                         "false-fail for modules that need special context.")
    args = ap.parse_args()

    out = args.out or _default_out(args.folder)

    goals = generate_goals(args.folder, args.instruction, mode=args.mode,
                           exts=args.ext, max_files=args.max,
                           verify=args.verify, check_cmd=args.check_cmd,
                           import_smoke=args.import_smoke)

    if not goals:
        print("folder_coder: no matching files in %s -- nothing to do."
              % os.path.abspath(args.folder))
        print("  (check --ext / --max, or use --mode single)")
        return 1

    nverify = sum(1 for g in goals if isinstance(g, dict) and g.get("checks"))
    print("folder_coder: %d goal(s) (%d with acceptance check)  mode=%s  folder=%s"
          % (len(goals), nverify, args.mode, os.path.abspath(args.folder)))
    print("-" * 70)
    for i, g in enumerate(goals, 1):
        if isinstance(g, dict):
            tags = ", ".join(c.get("type", "?") for c in g.get("checks", []))
            print("%3d. [check: %s] %s" % (i, tags, g.get("text", "")))
        else:
            print("%3d. %s" % (i, g))
    print("-" * 70)

    write_goals_file(goals, out)
    print("wrote goals file: %s" % out)
    print()
    print("next, launch the fleet with:")
    print('  python -m relay.fleet_runner --goals-file "%s"' % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
