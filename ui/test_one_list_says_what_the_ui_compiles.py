# -*- coding: utf-8 -*-
"""UI のソース一覧は一箇所。コピーは必ず腐る。

## 2026-09-22 に起きたこと

`ui/FleetCommands.cs` を足した。`ui/rebuild_ui.ps1` の2つの Build 行には入れた。
**同じ一覧を持っている他の4ファイルには入れなかった**。ローカルは緑（この箱では C# を
コンパイルする経路が走らない）、main へ push して CI が落ちた:

    ui\\CopilotChat.cs(4898,16): error CS0103: The name 'FleetCommands' does not exist

そして CI が言わなかったほうが悪い。`ui/build_cockpit.bat`・`ui/build_and_run.bat`・
`ui/_buildcheck.bat` も**その瞬間から壊れていた**。誰も走らせていなかったので誰も知らない。
`ui/test_deployed_cockpit_matches_its_source.py` の `_SOURCES` は
「build scripts' own list から読む」と書いてありながら**コピーだった**。

## 既に答えは repo にあった

`bench/ui_build_check.py` は同じ穴を先に塞いでいて、理由まで書いてある:

    READ FROM THE REAL BUILD, NOT COPIED FROM IT. ... The same omission-by-hand has broken
    this project's UI before.

直し方は新発明ではなく、**その規則を残りに適用すること**だった。

## ここが強制すること

csc を呼ぶ tracked なスクリプトは、`rebuild_ui.ps1` の `Build` 行を**読む**か、
理由つきで `EXEMPT` に載っているか、どちらか。新しい csc 呼び出しはどちらかを選ぶまで赤。
そして `EXEMPT` は片道ではない — 消えた項目も赤にする（両方向ラチェット）。
"""
from __future__ import annotations

import io
import os
import re
import subprocess

import pytest

_UI = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_UI)

#: The one definition: the script that actually produces the shipped binaries.
OWNER = "ui/rebuild_ui.ps1"

#: csc being RUN, in the shapes this repository writes it -- not csc being mentioned.
#:
#: The first version matched the string "csc.exe" anywhere, which swept in scripts/doctor.ps1
#: (it Test-Paths csc.exe to tell a first build from a missing .NET Framework) and
#: scripts/start_all.ps1 (it names csc.exe in an error message). Neither compiles anything, and
#: demanding they read a source list would have been a guard inventing work.
_INVOKES_CSC = re.compile(r'&\s*"?\$csc\b|%CSC%|\[\s*CSC\s*,|csc\.exe"?\s+/nologo', re.I)

#: Evidence that a file DERIVES the list: it PARSES the owner's Build lines, or it RUNS the
#: owner (or a tool that already reads it).
#:
#: NOT "the file mentions rebuild_ui.ps1", which is what this checked first. Every one of the
#: four stale copies carried a comment pointing at the real build -- that is how they described
#: themselves while restating the list underneath. Mutation-tested: pointing
#: .github/scripts/build-csharp-codeql.ps1 at a different file left this GREEN, because the
#: prose in its own header still named the owner. A guard satisfied by a comment checks comments.
#:
#: So the marker is the Build-line pattern itself, as LITERAL TEXT: a file that parses the owner
#: contains it, and prose about the owner does not.
_PARSES = r'Build\s+"([A-Za-z0-9_]+)"'
_RUNS_OWNER = re.compile(r'(-File|&|python|powershell)[^\n]*(rebuild_ui\.ps1|ui_build_check\.py)',
                         re.I)


def _derives(src: str) -> bool:
    """True when `src` gets the list from somewhere rather than holding one of its own."""
    return bool(_RUNS_OWNER.search(src) or _PARSES in src)

#: A `Build "<name>" @("a.cs", ...)` line in the owner.
_BUILD_LINE = re.compile(r'^\s*Build\s+"([A-Za-z0-9_]+)"\s+@\((.*)\)\s*$')

#: csc callers that legitimately do NOT build a UI binary, each with why. Not a convenience
#: list: every entry names a file that compiles something OTHER than FleetCockpit.exe /
#: CopilotChat.exe, so rebuild_ui.ps1's list is not the list it needs.
EXEMPT = {
    "scripts/win/move_companion_to_desktop.ps1":
        "compiles thirdparty/VirtualDesktop11.cs, a single vendored file that is not part of "
        "either UI binary",
    "tests/_install_path_harness.py":
        "compiles a throwaway uv.exe STUB from a one-file C# snippet it writes itself, so the "
        "install-path tests can run setup.bat without downloading uv; not a UI binary",
    "scripts/test_preflight_wsh_and_start_all_fallback.py":
        "compiles a throwaway wscript.exe STUB from a C# snippet it writes itself (it logs its "
        "arguments and exits with a chosen code), so start_all.bat's fallback when Windows Script "
        "Host is disabled can be exercised; not a UI binary",
    "scripts/test_setup_devtunnel_signature.py":
        "compiles a throwaway, deliberately UNSIGNED devtunnel.exe stub from a one-line C# "
        "snippet it writes itself, so setup_devtunnel.ps1's Authenticode check can be shown to "
        "refuse it; not a UI binary",
    "ui/test_the_ui_and_the_fleet_agree_on_the_command_channel.py":
        "compiles ui/FleetCommands.cs ALONE into a throwaway console exe, on purpose: it is "
        "the cross-language contract test for that one file's writer, not a build of the UI",
    "ui/test_the_chat_window_sends_what_was_typed.py":
        "compiles ui/ChatSend.cs + ui/FleetCommands.cs with the test-only oracle and harness in "
        "ui/testdata/ into a throwaway console exe: it runs the chat's send path without WPF, "
        "which is the reason ChatSend.cs is its own file, not a build of the UI",
    "ui/test_a_fleet_interrupt_survives_a_supervisor_restart.py":
        "compiles the same ui/ChatSend.cs + ui/FleetCommands.cs with the test-only oracle and "
        "harness in ui/testdata/ (identical SOURCES list to "
        "ui/test_the_chat_window_sends_what_was_typed.py) into a throwaway console exe: it runs "
        "the send path's restart-time interrupt decision without WPF, not a build of the UI",
    "ui/test_the_bridge_client_sends_the_token.py":
        "compiles ui/BridgeClient.cs with the test-only driver ui/testdata/BridgeClientHarness.cs "
        "into a throwaway console exe and runs it against a real bridge Handler on a free port: "
        "it proves the chat's bridge calls carry the token, which is the reason BridgeClient.cs "
        "is its own WPF-free file, not a build of the UI",
    "ui/test_a_submitted_task_is_on_top_at_once.py":
        "compiles ui/SubmittedTasks.cs + ui/FleetCommands.cs with the test-only driver in "
        "ui/harness/ into a throwaway console exe: it runs the cockpit's submitted-group merge "
        "and list order against real files without WPF, which is the reason SubmittedTasks.cs "
        "is its own file, not a build of the UI",
    "ui/test_the_startup_loop_has_a_backstop.py":
        "compiles the SHIPPED ui/SelfImproveDashboard.cs + ui/Theme.cs (with WPF references, "
        "since SelfImproveDashboardWindow extends Window) plus the test-only driver in "
        "ui/harness/ into a throwaway console exe: it drives StartupGate.GateAutomaticLaunch "
        "and AutoFixBudget.Decide, the pure startup/auto-repair policy, directly -- not a "
        "build of either shipped UI binary",
}

#: ui/*.cs that are compiled by a TEST and by no Build line, on purpose -- each with the test
#: that compiles it. Not shipped, so rebuild_ui.ps1 must not list them; and checked both ways
#: below: the file exists, and the named test really names it.
COMPILED_BY_A_TEST = {
    "ui/testdata/ChatDecisionsOriginal.cs": "ui/test_the_chat_window_sends_what_was_typed.py",
    "ui/testdata/ChatSendHarness.cs": "ui/test_the_chat_window_sends_what_was_typed.py",
    "ui/testdata/BridgeClientHarness.cs": "ui/test_the_bridge_client_sends_the_token.py",
    "ui/harness/SubmittedTasksHarness.cs": "ui/test_a_submitted_task_is_on_top_at_once.py",
    "ui/harness/StartupGateHarness.cs": "ui/test_the_startup_loop_has_a_backstop.py",
}


def _tracked():
    """CI only ever sees tracked files, so that is the universe. A local-only file swept here
    would make this pass or fail for reasons CI cannot reproduce."""
    try:
        r = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.decode("utf-8", "replace").split("\n") if p.strip()]


def _read(rel: str) -> str:
    try:
        return io.open(os.path.join(REPO, rel), encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def _csc_callers():
    tracked = _tracked()
    if tracked is None:
        pytest.skip("git could not list the tracked files here")
    out = []
    for rel in tracked:
        if os.path.splitext(rel)[1].lower() not in (".ps1", ".bat", ".cmd", ".py"):
            continue
        if _INVOKES_CSC.search(_read(rel)):
            out.append(rel)
    return out


def targets():
    """{name: (sources,)} parsed out of the owner."""
    found = {}
    for line in _read(OWNER).splitlines():
        m = _BUILD_LINE.match(line)
        if m:
            found[m.group(1)] = tuple(re.findall(r'"([^"]+\.cs)"', m.group(2)))
    return found


# ── the rule ──────────────────────────────────────────────────────────────────────────────

def test_every_csc_caller_reads_the_list_or_says_why_it_does_not():
    """**新しい csc 呼び出しは、一覧を読むか、理由を書くか。**黙って5つ目のコピーになる道を塞ぐ。"""
    offenders = []
    for rel in _csc_callers():
        if rel == OWNER or rel in EXEMPT:
            continue
        if not _derives(_read(rel)):
            offenders.append(rel)
    assert not offenders, (
        "these compile C# without reading %s's Build lines, so they carry their own copy of the "
        "source list -- the copy that went stale on 2026-09-22 and broke CI plus three .bats at "
        "once. Parse the Build lines (see bench/ui_build_check.py), delegate to the owner, or "
        "add an EXEMPT entry saying what they build instead: %s" % (OWNER, offenders))


def test_an_exemption_names_a_file_that_still_calls_csc():
    """**両方向。** 消えた項目・csc をやめた項目が残ると、この一覧は現状ではなく履歴になる。"""
    callers = set(_csc_callers())
    stale = sorted(p for p in EXEMPT if p not in callers)
    assert not stale, (
        "these EXEMPT entries no longer call csc at all -- delete them, or the next reader takes "
        "this list for the current state when it is a historical one: %s" % stale)


def test_every_ui_source_is_compiled_into_something():
    """**足し忘れを出口ではなく入口で捕まえる。** 今日の欠陥は「一覧に入れ忘れた」であって
    「一覧が間違っていた」ではない。ui/ に置かれた .cs がどのターゲットにも入っていなければ、
    それはビルドされないコード = 誰も読まないうちに腐る。"""
    tracked = _tracked()
    if tracked is None:
        pytest.skip("git could not list the tracked files here")
    on_disk = {os.path.basename(p) for p in tracked
               if p.startswith("ui/") and p.endswith(".cs") and p not in COMPILED_BY_A_TEST}
    compiled = {s for srcs in targets().values() for s in srcs}
    orphans = sorted(on_disk - compiled)
    assert not orphans, (
        "these ui/*.cs are in no build target in %s, so nothing compiles them: %s" % (OWNER, orphans))


def test_a_test_only_source_is_really_compiled_by_its_test():
    """**両方向。** COMPILED_BY_A_TEST の項目は、実在し、名指したテストが実際にそのパスを
    書いていること。消えた項目・誰も読まなくなった項目が残れば、一覧は免罪符になる。"""
    for src, test in sorted(COMPILED_BY_A_TEST.items()):
        assert os.path.isfile(os.path.join(REPO, src)), "%s is listed but does not exist" % src
        body = _read(test)
        parts = src.split("/")[1:]          # ui/testdata/X.cs -> "testdata", "X.cs"
        assert all('"%s"' % p in body for p in parts), (
            "%s claims %s compiles it, but that test does not name it" % (src, test))
    shipped = {s for srcs in targets().values() for s in srcs}
    both = sorted(p for p in COMPILED_BY_A_TEST if os.path.basename(p) in shipped)
    assert not both, "test-only sources ended up in a shipped Build line: %s" % both


def test_every_named_source_exists():
    """逆向き。消したファイルが一覧に残ればビルドは落ちるが、**落ちるのは csc のある箱だけ**で、
    この箱で緑・CI で赤という 2026-09-22 の形にそのまま戻る。"""
    missing = sorted(s for srcs in targets().values() for s in srcs
                     if not os.path.isfile(os.path.join(_UI, s)))
    assert not missing, ("%s names sources that do not exist: %s" % (OWNER, missing))


def test_the_owner_still_has_build_lines_to_read():
    """このファイルの全部が `targets()` に乗っている。**空を読めば全部が緑になる** —
    読めない一覧は空ではなく赤、という規則を bench/ui_build_check.py と揃える。"""
    found = targets()
    assert found, ("no Build lines found in %s; every check here would pass vacuously" % OWNER)
    assert len(found) >= 2, (
        "expected both UI binaries to be built by %s, found %r" % (OWNER, sorted(found)))
