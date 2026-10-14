"""Every third-party module the chart tools import is declared in requirements.txt.

setup / quickstart / start_all all install from requirements.txt, so a module that a tool
imports but requirements.txt does not name works on the one PC where it was installed by hand
and fails on a fresh PC. (xlsxwriter was exactly that.) This reads the imports out of the
module source with ast -- including the lazy imports inside functions and the importlib-style
`_need("name")` calls -- and compares them with the declared distributions.

To cover another module, add its path to MODULES.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MODULES = ["tools/chart_ops.py"]

#: import name -> distribution name, where they differ.
DIST = {"yaml": "pyyaml", "PIL": "pillow", "docx": "python-docx", "pptx": "python-pptx",
        "dotenv": "python-dotenv", "win32com": "pywin32"}


def _declared() -> set[str]:
    names = set()
    for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"[A-Za-z0-9_.\-]+", line)
        if m:
            names.add(m.group(0).lower().replace("_", "-"))
    return names


def _imported_top_level(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "_need" and node.args
              and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            found.add(node.args[0].value.split(".")[0])
    return found


def _third_party(names: set[str]) -> set[str]:
    local = {p.name for p in REPO.iterdir() if p.is_dir()} | {p.stem for p in REPO.glob("*.py")}
    return {n for n in names
            if n not in sys.stdlib_module_names and n not in local and n != "__future__"}


def test_every_third_party_import_of_the_chart_tools_is_in_requirements():
    declared = _declared()
    missing = []
    for rel in MODULES:
        for mod in sorted(_third_party(_imported_top_level(REPO / rel))):
            dist = DIST.get(mod, mod).lower().replace("_", "-")
            if dist not in declared:
                missing.append(f"{rel}: imports {mod!r} but requirements.txt has no {dist!r}")
    assert not missing, "\n".join(missing)


def test_the_chart_libraries_are_found_by_the_scan():
    found = _third_party(_imported_top_level(REPO / "tools/chart_ops.py"))
    assert {"pandas", "matplotlib", "openpyxl", "xlsxwriter"} <= found


def test_third_party_imports_are_lazy():
    """No third-party import at module level: a PC missing one must still import the module."""
    tree = ast.parse((REPO / "tools/chart_ops.py").read_text(encoding="utf-8"))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top.add(node.module.split(".")[0])
    assert not _third_party(top), f"module-level third-party imports: {_third_party(top)}"
