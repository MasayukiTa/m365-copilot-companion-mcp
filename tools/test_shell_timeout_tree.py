# -*- coding: utf-8 -*-
"""A timed-out command must not leave its children running.

COUNTED ON THE OPERATOR'S MACHINE, 2026-08-31: 44 node processes, 40 of them hung `npx eslint`
and `npx tsc` from benchmark instances, the oldest 33 hours old. They held 164 MB of RSS and,
through npx cache handles, nearly a gigabyte of disk -- killing them took C: from 2.10 GB free
to 3.03 GB, which is the difference between a run that starts and one that refuses.

WHY THEY EXISTED. `subprocess.run(..., shell=True, timeout=N)` kills its DIRECT child. On
Windows that is cmd.exe, and `npx eslint` has cmd.exe spawn node. The timeout killed the shell
and left node running -- forever, because npx prompts before installing a package it does not
have, and a tool call has no stdin to answer with. Two defects compounding: an unanswerable
prompt, and a kill that stopped one level short.

WHY THE REAPER COULD NOT CLEAN THEM UP. relay/orphan_reaper.py attributes a process through its
ancestry, and the ancestor it would attribute through is the cmd.exe the timeout already
killed. By the time anything looked, the evidence of ownership was gone. So the fix has to be
at the moment the timeout fires, while the parent is still known.
"""
import os
import subprocess
import sys
import time

import pytest

from tools import code_exec as CE

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the tree-kill path measured here is Windows")


def _alive(pid):
    out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                         capture_output=True, text=True, timeout=20).stdout
    return str(pid) in out


def _spawn_grandchild_command(marker_path):
    """A shell command whose real work happens in a GRANDCHILD, like `npx <anything>` does.

    The child is cmd.exe; it starts python, which writes its own pid and then sleeps well past
    the timeout. Killing only the shell leaves that python running, which is the defect.
    """
    py = sys.executable.replace("\\", "/")
    inner = (
        "import os,time,sys;"
        "open(r'%s','w').write(str(os.getpid()));"
        "time.sleep(120)" % marker_path
    )
    return '"%s" -c "%s"' % (py, inner)


def test_a_timed_out_command_kills_its_grandchild(tmp_path):
    marker = str(tmp_path / "pid.txt").replace("\\", "/")
    out = CE._run_with_tree_timeout(_spawn_grandchild_command(marker), 3, str(tmp_path))

    assert "timeout" in out
    assert os.path.isfile(marker), "the grandchild never started; the test proves nothing"
    pid = int(open(marker).read().strip())

    # Give the kill a moment to land, then insist.
    for _ in range(20):
        if not _alive(pid):
            break
        time.sleep(0.5)
    else:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        finally:
            pytest.fail("the grandchild (pid %d) survived the timeout -- this is the leak that "
                        "left 40 hung node processes on the machine" % pid)


def test_the_message_says_the_children_were_killed_too():
    """An operator reading '[timeout]' has no reason to go looking for survivors. The message
    has to say what was done, or the old behaviour and the new one read identically."""
    out = CE._run_with_tree_timeout("ping -n 20 127.0.0.1", 2, os.getcwd())
    assert "timeout" in out
    assert "every process it started" in out


def test_stdin_is_closed_so_a_prompt_cannot_hang_forever(tmp_path):
    """THE OTHER HALF. npx asks 'Ok to proceed?' when the package is absent and waits. With no
    stdin the read fails immediately instead of parking a process for a day."""
    py = sys.executable.replace("\\", "/")
    cmd = '"%s" -c "import sys; d=sys.stdin.read(); print(\'READ:%%r\' %% d)"' % py
    started = time.time()
    out = CE._run_with_tree_timeout(cmd, 20, str(tmp_path))
    assert time.time() - started < 15, "a read from stdin blocked; it should have returned at once"
    assert "timeout" not in out, out
    assert "READ:''" in out, out


def test_an_ordinary_command_still_returns_its_output(tmp_path):
    out = CE._run_with_tree_timeout('echo hello-from-the-shell', 30, str(tmp_path))
    assert "hello-from-the-shell" in out
    assert "timeout" not in out


def test_a_failing_command_still_reports_its_return_code(tmp_path):
    out = CE._run_with_tree_timeout('exit 3', 30, str(tmp_path))
    assert "returncode: 3" in out


def test_shell_exec_uses_the_tree_killing_path(monkeypatch, tmp_path):
    """The wiring, not just the helper -- the helper existing while shell_exec still called
    subprocess.run would fix nothing."""
    seen = {}

    def _fake(command, timeout, cwd):
        seen["called"] = (command, timeout, cwd)
        return "[stdout]\nok"

    monkeypatch.setattr(CE, "_run_with_tree_timeout", _fake)
    monkeypatch.setattr(CE, "require_unlocked", lambda: None)
    CE.shell_exec("echo hi", timeout=7, working_dir=str(tmp_path))
    assert seen.get("called"), "shell_exec no longer routes through the tree-killing runner"
    assert seen["called"][1] == 7
