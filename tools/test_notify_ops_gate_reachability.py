# -*- coding: utf-8 -*-
"""A window that exists only to say it failed.

REAL FAILURE, ON THE OWNER'S DESKTOP: FleetCockpit.exe's approval prompt opened, then
immediately showed

    承認ゲートを開けませんでした。
    approval gate is not in the gate directory:
    C:\\Users\\...\\AppData\\Local\\Temp\\tierbench_1e_zcl1p\\gates
    (expected C:\\Users\\<user>\\.companion_gates)

and closed. A benchmark script had built a SkillStore with a temp gate directory (via
MCP_SKILLS_GATE_DIR or SkillStore(gate_dir=...)) and called SkillStore.request_approval ->
relay/skills.py notify_approval_gate(...) -- without also setting MCP_GATE_DIR or
MCP_SUPPRESS_GUI. tools/notify_ops.notify_approval_gate then spawned
`FleetCockpit.exe --approval-gate <path>` unconditionally. ApprovalPromptWindow
(ui/FleetCockpit.cs) only ever accepts a gate found inside
`CockpitWindow.ResolveGateDirectory("", "")`'s answer, so the window's entire lifetime was
painting that one failure message. Any writer with a non-default gate directory hits this same
shape: relay/skills.py, tools/gate_ops.py, tools/contract_gate.py, relay/task_router.py all
funnel through this one function.

THE FIX IS AT THE LAUNCHER, because its promise -- "spawning this opens something the operator
can act on" -- was wrong for every caller whose gate directory disagrees with the cockpit's.
tools/notify_ops.resolve_gate_directory mirrors ui/FleetCockpit.cs's
`CockpitWindow.ResolveGateDirectory` in the exact same order (MCP_GATE_DIR, then
MCP_ALLOWED_BASE's first root with the same '~'/'*'/empty/drive-letter rules, then home), and
tools/notify_ops.gate_is_reachable answers the same question ApprovalPromptWindow's constructor
asks before painting anything. notify_approval_gate calls it before subprocess.Popen and, when
the gate is not reachable, returns a status describing why instead of opening a window that can
only fail.

Run: pytest -q tools/test_notify_ops_gate_reachability.py
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import tools.notify_ops as N  # noqa: E402

CS_PATH = os.path.join(REPO, "ui", "FleetCockpit.cs")


# ── resolve_gate_directory: the pure resolver, tested directly ──────────────────────────────

def test_mcp_gate_dir_wins_outright(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_GATE_DIR", str(tmp_path / "over"))
    monkeypatch.delenv("MCP_ALLOWED_BASE", raising=False)
    assert N.resolve_gate_directory() == os.path.abspath(str(tmp_path / "over"))


def test_mcp_gate_dir_is_trimmed_of_whitespace_and_quotes(monkeypatch, tmp_path):
    target = tmp_path / "over"
    monkeypatch.setenv("MCP_GATE_DIR", '  "%s"  ' % target)
    assert N.resolve_gate_directory() == os.path.abspath(str(target))


def test_blank_mcp_gate_dir_falls_through_to_allowed_base(monkeypatch, tmp_path):
    """A blank override must not win over the fallback -- the C# side checks
    IsNullOrWhiteSpace, not just IsNullOrEmpty."""
    monkeypatch.setenv("MCP_GATE_DIR", "   ")
    monkeypatch.setenv("MCP_ALLOWED_BASE", str(tmp_path))
    assert N.resolve_gate_directory() == os.path.join(os.path.abspath(str(tmp_path)),
                                                       ".companion_gates")


def test_empty_allowed_base_means_home(monkeypatch):
    monkeypatch.delenv("MCP_GATE_DIR", raising=False)
    monkeypatch.setenv("MCP_ALLOWED_BASE", "")
    assert N.resolve_gate_directory() == os.path.join(N._user_profile(), ".companion_gates")


def test_star_allowed_base_also_means_home(monkeypatch):
    monkeypatch.delenv("MCP_GATE_DIR", raising=False)
    monkeypatch.setenv("MCP_ALLOWED_BASE", "*")
    assert N.resolve_gate_directory() == os.path.join(N._user_profile(), ".companion_gates")


def test_tilde_alone_means_home(monkeypatch):
    monkeypatch.delenv("MCP_GATE_DIR", raising=False)
    monkeypatch.setenv("MCP_ALLOWED_BASE", "~")
    assert N.resolve_gate_directory() == os.path.join(N._user_profile(), ".companion_gates")


def test_tilde_slash_expands_under_home(monkeypatch):
    monkeypatch.delenv("MCP_GATE_DIR", raising=False)
    monkeypatch.setenv("MCP_ALLOWED_BASE", "~/scratch")
    expected = os.path.join(os.path.abspath(os.path.join(N._user_profile(), "scratch")),
                            ".companion_gates")
    assert N.resolve_gate_directory() == expected


@pytest.mark.skipif(os.name != "nt", reason="';' is only Path.PathSeparator (and "
                    "os.pathsep) on Windows; on POSIX resolve_gate_directory's os.pathsep "
                    "split never fires and this asserts a Windows-only path shape")
def test_only_the_first_root_of_a_semicolon_list_is_the_gate_root(monkeypatch, tmp_path):
    """Path.PathSeparator is ';' on Windows, and the C# side takes roots[0] -- a second or
    third allowed root must not silently become the gate directory."""
    root_a = tmp_path / "rootA"
    root_b = tmp_path / "rootB"
    monkeypatch.delenv("MCP_GATE_DIR", raising=False)
    monkeypatch.setenv("MCP_ALLOWED_BASE", "%s;%s" % (root_a, root_b))
    assert N.resolve_gate_directory() == os.path.join(os.path.abspath(str(root_a)),
                                                       ".companion_gates")


@pytest.mark.skipif(os.name != "nt", reason="a bare drive letter ('C:') is a Windows path "
                    "concept -- POSIX has no drive letters, and both this test's expected "
                    "value and resolve_gate_directory's own handling of it key off os.sep, "
                    "which is not '\\' off Windows")
def test_a_bare_drive_letter_gets_a_separator_before_the_folder(monkeypatch):
    monkeypatch.delenv("MCP_GATE_DIR", raising=False)
    monkeypatch.setenv("MCP_ALLOWED_BASE", "C:")
    result = N.resolve_gate_directory()
    assert result == os.path.join(os.path.abspath("C:" + os.sep), ".companion_gates")


def test_the_process_environment_beats_the_settings_file_fallback(monkeypatch, tmp_path):
    """ApprovalPromptWindow calls ResolveGateDirectory("", "") -- the settings-file fallback
    arguments are never read for the window this launcher spawns. A value handed in through
    file_base/file_gate_dir must lose to an environment variable, exactly like the C# side."""
    monkeypatch.setenv("MCP_GATE_DIR", str(tmp_path / "env_wins"))
    result = N.resolve_gate_directory(file_base="ignored", file_gate_dir=str(tmp_path / "file"))
    assert result == os.path.abspath(str(tmp_path / "env_wins"))


# ── gate_is_reachable: the decision ApprovalPromptWindow's constructor makes ────────────────

def test_a_gate_inside_the_resolved_directory_is_reachable(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_GATE_DIR", str(tmp_path))
    gate = tmp_path / "gate_x.json"
    gate.write_text("{}", encoding="utf-8")
    ok, gate_dir, allowed, reason = N.gate_is_reachable(gate)
    assert ok is True, reason
    assert reason == ""
    assert os.path.samefile(gate_dir, allowed)


def test_a_gate_outside_the_resolved_directory_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_GATE_DIR", str(tmp_path / "allowed"))
    (tmp_path / "allowed").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    gate = elsewhere / "gate_x.json"
    gate.write_text("{}", encoding="utf-8")
    ok, gate_dir, allowed, reason = N.gate_is_reachable(gate)
    assert ok is False
    assert reason == "outside the prompt's directory"
    assert gate_dir == os.path.abspath(str(elsewhere))
    assert allowed == os.path.abspath(str(tmp_path / "allowed"))


def test_this_is_the_exact_reported_shape(monkeypatch, tmp_path):
    """The real report named a tierbench temp dir as the gate's folder and
    C:\\Users\\<user>\\.companion_gates as the one expected -- reproduced here with the
    default resolution (no MCP_GATE_DIR override) standing in for the operator's real default."""
    monkeypatch.delenv("MCP_GATE_DIR", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_BASE", raising=False)
    tierbench = tmp_path / "tierbench_1e_zcl1p" / "gates"
    tierbench.mkdir(parents=True)
    gate = tierbench / "gate_x.json"
    gate.write_text("{}", encoding="utf-8")
    ok, gate_dir, allowed, reason = N.gate_is_reachable(gate)
    assert ok is False
    assert allowed == os.path.join(N._user_profile(), ".companion_gates")


def test_a_filename_that_is_not_a_gate_is_rejected_by_shape_first(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_GATE_DIR", str(tmp_path))
    not_a_gate = tmp_path / "not_a_gate.json"
    not_a_gate.write_text("{}", encoding="utf-8")
    ok, _gate_dir, _allowed, reason = N.gate_is_reachable(not_a_gate)
    assert ok is False
    assert reason == "invalid gate filename"


def test_a_gate_that_does_not_exist_yet_is_not_reachable(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_GATE_DIR", str(tmp_path))
    missing = tmp_path / "gate_never_written.json"
    ok, _gate_dir, _allowed, reason = N.gate_is_reachable(missing)
    assert ok is False
    assert reason == "gate file does not exist"


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names and GetShortPathNameW "
                    "(ctypes.windll) only exist on Windows")
def test_a_short_and_long_spelling_of_the_same_directory_still_match(monkeypatch, tmp_path):
    """THE BUG THIS GUARDS AGAINST IN ui/FleetCockpit.cs ApprovalPromptWindow: a gate directory
    under %TEMP% is handed back in its 8.3 short form by some callers while the resolved
    directory is spelled long (or the reverse). Comparing the two directory STRINGS failed
    exactly there; gate_is_reachable must not repeat that mistake."""
    long_dir = tmp_path / "a_long_directory_name_for_8dot3_aliasing"
    long_dir.mkdir()
    buf = ctypes.create_unicode_buffer(260)
    rc = ctypes.windll.kernel32.GetShortPathNameW(str(long_dir), buf, 260)
    short_dir = buf.value
    if not rc or short_dir == str(long_dir):
        pytest.skip("8.3 short names are disabled on this volume (fsutil 8dot3name)")

    gate_long = long_dir / "gate_x.json"
    gate_long.write_text("{}", encoding="utf-8")

    # The resolved (allowed) directory is spelled SHORT; the gate handed to gate_is_reachable
    # is spelled LONG. A naive string comparison of the two directories would disagree even
    # though they name the same place on disk.
    monkeypatch.setenv("MCP_GATE_DIR", short_dir)
    ok, gate_dir, allowed, reason = N.gate_is_reachable(gate_long)
    assert ok is True, reason
    assert gate_dir != allowed, "the spellings must actually differ for this test to mean anything"


# ── notify_approval_gate: refuses to spawn rather than open a window that can only fail ─────

def test_the_launcher_does_not_spawn_for_an_unreachable_gate(monkeypatch, tmp_path):
    """Popen is patched BEFORE the guard env vars are removed, so nothing can spawn a real
    process even if a code path here is wrong."""
    monkeypatch.setattr(N.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("a real process must never be spawned by this test")))
    monkeypatch.setattr(N, "notify_desktop", lambda *a, **k: "toast")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("MCP_SUPPRESS_GUI", "0")

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("MCP_GATE_DIR", str(allowed))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    gate = elsewhere / "gate_x.json"
    gate.write_text("{}", encoding="utf-8")

    result = N.notify_approval_gate("t", "b", str(gate))
    assert "approval prompt not opened" in result, result
    assert "outside" in result, result
    assert str(allowed.resolve()) in result or str(allowed) in result, result


def test_the_launcher_still_spawns_for_a_reachable_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(N, "notify_desktop", lambda *a, **k: "toast")
    monkeypatch.setattr(N.Path, "is_file", lambda self: True)
    launched = []
    monkeypatch.setattr(N.subprocess, "Popen", lambda *a, **k: launched.append(a) or None)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("MCP_SUPPRESS_GUI", "0")

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("MCP_GATE_DIR", str(allowed))
    gate = allowed / "gate_x.json"
    gate.write_text("{}", encoding="utf-8")

    result = N.notify_approval_gate("t", "b", str(gate))
    assert launched, result
    assert "actionable approval prompt opened" in result, result


# ── cross-language: the python order must match ui/FleetCockpit.cs's order ──────────────────

def _strip_csharp_comments(src: str) -> str:
    """// line comments and /* block */ comments removed. Good enough for this one method,
    which contains no string literal with a `/` in it to confuse the block-comment regex."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def _extract_csharp_method(src: str, signature: str) -> str:
    start = src.index(signature)
    brace_open = src.index("{", start)
    depth = 0
    i = brace_open
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace_open:i + 1]
        i += 1


@pytest.mark.skipif(not os.path.isfile(CS_PATH), reason="ui/FleetCockpit.cs not present here")
def test_the_python_resolver_follows_the_same_order_as_the_csharp_one():
    cs_src = Path(CS_PATH).read_text(encoding="utf-8")
    cs_body = _strip_csharp_comments(
        _extract_csharp_method(cs_src, "internal static string ResolveGateDirectory"))

    # ORDER: MCP_GATE_DIR is consulted, and returned on, before MCP_ALLOWED_BASE is even read.
    assert cs_body.index("MCP_GATE_DIR") < cs_body.index("MCP_ALLOWED_BASE")
    assert cs_body.index("MCP_ALLOWED_BASE") < cs_body.index(".companion_gates")
    # THE RULES the base-path branch applies, in the C# source, that the python port must
    # mirror: empty-or-star means home, "~" means home, and a bare drive letter gets a
    # separator appended before ".companion_gates" is joined on.
    assert '"*"' in cs_body
    assert '"~"' in cs_body
    assert "Length == 2" in cs_body and "':'" in cs_body

    # THE DOCSTRING QUOTES BOTH NAMES OUT OF ORDER while explaining the drift this file exists
    # to prevent, so the order check has to run on the EXECUTABLE code only, exactly the
    # problem tests/_srcprobe.py exists to solve (see its module docstring).
    sys.path.insert(0, os.path.join(REPO, "tests"))
    from _srcprobe import executable_source
    # ast.unparse renders string literals with single quotes regardless of how the source
    # spelled them, so the two checks below intentionally differ in quote style from the
    # C# ones above -- both are checking for the same literal value.
    py_src = executable_source(N.resolve_gate_directory)
    assert py_src.index("MCP_GATE_DIR") < py_src.index("MCP_ALLOWED_BASE")
    assert py_src.index("MCP_ALLOWED_BASE") < py_src.index(".companion_gates")
    assert "'*'" in py_src
    assert "'~'" in py_src
    assert "len(base_path) == 2" in py_src


@pytest.mark.skipif(not os.path.isfile(CS_PATH), reason="ui/FleetCockpit.cs not present here")
def test_the_approval_window_call_site_passes_no_settings_fallback():
    """ApprovalPromptWindow -- the ONLY caller that matters for --approval-gate, which is what
    this launcher spawns -- must still call ResolveGateDirectory("", ""), i.e. resolve purely
    from the process environment this launcher's child inherits. If that call site ever starts
    passing a settings-file fallback, gate_is_reachable's mirror (which also always passes
    empty strings) silently stops matching what the real window does."""
    cs_src = _strip_csharp_comments(Path(CS_PATH).read_text(encoding="utf-8"))
    assert 'CockpitWindow.ResolveGateDirectory("", "")' in cs_src
