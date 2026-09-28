# -*- coding: utf-8 -*-
"""All unattended verifier/background spawns use the shared killable-tree policy."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_code_exec_spawns_a_killable_tree_and_uses_shared_kill():
    s = _text("tools/code_exec.py")
    block = s[s.index("def _run_with_tree_timeout"):s.index("#: This tool runs caller-supplied code", s.index("def _run_with_tree_timeout"))]
    assert "**childproc.tree_popen_kwargs(headless=True)" in block
    kill = s[s.index("def _kill_tree(proc):"):s.index("#: This tool runs caller-supplied code", s.index("def _kill_tree(proc):"))]
    assert "childproc.kill_tree(proc" in kill


def test_autoloop_spawns_and_kills_through_childproc():
    s = _text("tools/auto/autoloop.py")
    block = s[s.index("def _run(cmd, cwd, timeout_s):"):s.index("\ndef _abs(", s.index("def _run(cmd, cwd, timeout_s):"))]
    assert "**childproc.tree_popen_kwargs(headless=True)" in block
    assert "childproc.kill_tree(proc" in block
    assert 'subprocess.run(["taskkill"' not in block


def test_background_jobs_own_killable_trees_and_watchdog_kills_the_tree():
    s = _text("tools/jobs.py")
    assert s.count("**childproc.tree_popen_kwargs(headless=True)") >= 2
    wd = s[s.index("def _watchdog_fire("):s.index("def _start_watchdog(")]
    assert "childproc.kill_tree(proc" in wd
    jk = s[s.index("def job_kill("):s.index("#: This tool runs caller-supplied code", s.index("def job_kill("))]
    assert "childproc.kill_tree(job.process" in jk


def test_tree_policy_has_posix_session_and_windows_taskkill_contract():
    s = _text("tools/childproc.py")
    assert 'return {"start_new_session": True}' in s
    assert '["taskkill", "/PID", str(proc.pid), "/T", "/F"]' in s
    assert "os.killpg(int(proc.pid), signal.SIGKILL)" in s
