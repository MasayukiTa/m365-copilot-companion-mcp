# -*- coding: utf-8 -*-
"""新しい子プロセス起動が、コンソールの扱いを**黙って親任せにする**ことを止める。

## これはラチェットであって、移行ゲートではない

最初に書きかけた版は「`tools.childproc` を通らない起動は全部落とす」だった。それは
**既存123箇所を一度に赤にする**ので、ラチェットの形をした移行ゲートでしかない。
敵対的レビューで潰された。既存は**同一性でベースラインに載せ、増えたときだけ落とす**。

**そして減ったときも落とす。** 「到達不能／未対応」を記録した一覧は、誰かが直した瞬間から
嘘になる。片方向のラチェットはその嘘を検出できない。ここでは、どれかの起動地点が
`creationflags` を持つようになったら**このテストが落ちて、表から消すことを要求する**。
表が現状より長生きしない唯一の方法がこれ。

## 「decided」であって「safe」ではない

窓が出るのは、**コンソールを持たない親**がコンソールアプリを起こしたとき。実測
(2026-09-22、`pythonw.exe` を親に): フラグ無し → 子のコンソール窓が**見えている**、
`CREATE_NO_WINDOW` → コンソールごと無い。
（`tools/test_a_windowless_launch_really_has_no_window.py` がこれを毎回走らせている）

だが**どの地点でそれが問題になるかは、親を誰が起こすかで決まる**。静的走査は答えられない。
いま窓が出ていない地点の多くは `scripts/supervisor.ps1` が `-WindowStyle Hidden` で木の根を
起こし、窓の無いコンソールが下へ継承されているからで、**その依存はどこにも書かれていない**。
だからここが言うのは「この地点は方針を述べている / 与えられたものを継承している」だけ。
`creationflags=0` も**述べている**側に数える — ゼロと決めたのも決定だから。

## 一括変換をしない理由

`CREATE_NO_WINDOW` は「親のコンソールを隠す」ではなく「**コンソールに接続しない**」。
CONIN$ を読むもの、ssh のパスフレーズ、git の資格情報プロンプトは接続が無いと動かない。
数箇所の窓のために123箇所の I/O 契約を変えるのは取引として成立しない。
ベースラインは**分類が済んだものから減らす**。減らせばこのテストが要求してくる。
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import launch_sites as L  # noqa: E402


#: 2026-09-22 の実測。`path::function::callee` -> その関数内の未決定な起動の数。
#:
#: 行番号ではない。行番号は上を1行足すたびに動くので、同一性としては使えない
#: (在庫テスト tools/test_nothing_new_is_built_without_a_caller.py が同じ理由で
#: `path::name` を採っている)。関数名を変えたり呼び出しを動かしたりすればここも動くが、
#: それはその地点自身への編集なので、見直す理由として正しい。
#:
#: 数であって集合なのは、1つの関数が正当に複数持つことがあるから。集合にすると
#: **2つ目が黙って吸収される**。
BASELINE = {
    "bench/companionbench/fleet_agent.py::_run_child_in::subprocess.run": 1,
    "bench/companionbench/job_authority.py::__enter__::subprocess.Popen": 1,
    "bench/evalhost_batch_grade.py::<module>::subprocess.run": 1,
    "bench/evalhost_batch_grade.py::_supported_flags::subprocess.run": 1,
    "bench/gaia/retry_controller.py::restart_8011::subprocess.Popen": 1,
    "bench/gaia/retry_controller.py::run_chunk::subprocess.run": 1,
    "bench/gaia/run_pipeline.py::run_full_eval::subprocess.run": 1,
    "bench/gaia/run_pipeline.py::run_retry_controller::subprocess.run": 1,
    "bench/pro_capture.py::main::subprocess.run": 4,
    "bench/pro_cycle.py::_clear_toolchain_caches::subprocess.run": 1,
    "bench/pro_cycle.py::run::subprocess.run": 1,
    "bench/pro_run_50.py::main::subprocess.run": 2,
    "bench/pro_stage_goals.py::run::subprocess.run": 1,
    "bench/remote/broker_client.py::call::subprocess.run": 1,
    "bench/review_build_goals.py::enumerate_files::subprocess.run": 1,
    "bench/review_fix.py::git_bonus_branch::subprocess.run": 1,
    "bench/review_fix.py::git_bonus_commit::subprocess.run": 2,
    "bench/review_fix.py::git_bonus_precheck::subprocess.run": 2,
    "bench/review_run.py::run_fleet::subprocess.Popen": 1,
    "bench/run_oracle_ab.py::run_one::subprocess.run": 1,
    "bench/swe_batch_setup.py::main::subprocess.run": 2,
    "bench/swe_check.py::wsl::subprocess.run": 1,
    "bench/swe_check_remote.py::_scp::subprocess.run": 1,
    "bench/swe_check_remote.py::_scp_from::subprocess.run": 1,
    "bench/swe_check_remote.py::_ssh_ps::subprocess.run": 1,
    "bench/swe_check_remote.py::main::subprocess.run": 1,
    "bench/swe_check_selftest.py::<module>::subprocess.run": 5,
    "bench/swe_clean_setup.py::main::subprocess.run": 1,
    "bench/swe_lite300_miss_bundle.py::pull_logs::subprocess.run": 1,
    "bench/swe_procs.py::<module>::subprocess.run": 1,
    "bench/swe_procs2.py::<module>::subprocess.run": 1,
    "bench/swe_repos_setup_batch.py::run::subprocess.run": 1,
    "bench/swe_rerun_setup.py::<module>::subprocess.run": 2,
    "bench/swe_run_until_done.py::_all_python_rows::subprocess.run": 1,
    "bench/swe_run_until_done.py::acquire_lock::subprocess.run": 1,
    "bench/swe_run_until_done.py::classify_failure::subprocess.run": 1,
    "bench/swe_run_until_done.py::cleanup_repo_env::subprocess.run": 1,
    "bench/swe_run_until_done.py::kill_pids::subprocess.run": 1,
    "bench/swe_run_until_done.py::run_round::subprocess.Popen": 1,
    "bench/swe_run_until_done.py::setup_round::subprocess.run": 1,
    "bench/swe_singleshot.py::grade::subprocess.run": 1,
    "bench/swe_singleshot.py::reset_wt::subprocess.run": 2,
    "bench/swe_singleshot.py::solve::subprocess.run": 1,
    "bench/swe_solve_decoupled.py::acquire_lock::subprocess.run": 1,
    "bench/swe_solve_decoupled.py::capture::subprocess.run": 1,
    "bench/swe_solve_decoupled.py::release::subprocess.run": 2,
    "bench/swe_solve_decoupled.py::run_fleet::subprocess.Popen": 1,
    "bench/swe_solve_decoupled.py::stage::subprocess.run": 1,
    "bench/swe_split.py::load_lite::subprocess.run": 1,
    "bench/swe_status.py::run::subprocess.run": 1,
    "bench/ui_deploy.py::<module>::subprocess.run": 3,
    "bridge/copilot_bridge.py::_review_stream::subprocess.Popen": 1,
    "bridge/copilot_bridge.py::_run_fix_subprocess::subprocess.Popen": 1,
    "relay/agent_profiles.py::prompt_for_agent_url::subprocess.run": 1,
    "relay/code_task.py::main::subprocess.call": 1,
    "relay/orphan_reaper.py::candidates::subprocess.run": 1,
    "relay/orphan_reaper.py::reap::subprocess.run": 1,
    "relay/selfimprove/apply.py::safe_commit::subprocess.run": 2,
    "relay/selfimprove/frozen.py::_diff_stat::subprocess.run": 2,
    "relay/selfimprove/guards.py::_proc_alive_cim::subprocess.run": 1,
    "relay/task_router.py::autostart_fleet::subprocess.Popen": 1,
    "scripts/bootstrap.py::_provision_dev_tunnel::subprocess.Popen": 1,
    "scripts/bootstrap.py::_seed_pip::subprocess.call": 1,
    "scripts/bootstrap.py::step_ensure_venv::subprocess.call": 1,
    # 2026-09-24: step_install_deps became a thin lock-acquire wrapper around
    # _install_deps_locked (7034f0b, "installs repair what pip breaks"), which also added a
    # third call: a --force-reinstall pass for distributions pip's own upgrade left broken
    # (see the comment above the `before = {spec for spec, _ in _broken_distributions(py)}`
    # line in scripts/bootstrap.py::_install_deps_locked). None of the three decide a console
    # policy, same as every other pip-install call already in this table.
    "scripts/bootstrap.py::_install_deps_locked::subprocess.call": 3,
    "scripts/change_scope.py::_git::subprocess.run": 1,
    "scripts/check_integration_evidence.py::_git::subprocess.run": 1,
    "scripts/check_integration_evidence.py::references::subprocess.run": 1,
    "scripts/diag_warmup_bias.py::main::subprocess.Popen": 2,
    "scripts/diag_warmup_bias.py::main::subprocess.run": 1,
    "scripts/preflight.py::_run::subprocess.call": 1,
    "scripts/prove_basic_desktop_actions.py::_close::subprocess.run": 1,
    "scripts/prove_click_and_type.py::main::subprocess.Popen": 1,
    "scripts/prove_click_and_type.py::main::subprocess.run": 1,
    "scripts/run_script_style_tests.py::run_one::subprocess.run": 1,
    "scripts/run_transport_series.py::run_one::subprocess.run": 1,
    "scripts/status.py::processes::subprocess.run": 1,
    "tools/auto/autoloop.py::_run::subprocess.Popen": 1,
    "tools/auto/autoloop.py::_run::subprocess.run": 1,
    "tools/childproc.py::run::subprocess.run": 1,
    "tools/code_exec.py::_kill_tree::subprocess.run": 1,
    "tools/code_exec.py::_run_with_tree_timeout::subprocess.Popen": 1,
    "tools/code_exec.py::run_python::subprocess.run": 1,
    "tools/coding_ops.py::_git_raw::subprocess.run": 1,
    "tools/env_ops.py::_pip_version::subprocess.run": 1,
    "tools/env_ops.py::pip_install::subprocess.run": 1,
    "tools/jobs.py::run_in_background::subprocess.Popen": 1,
    "tools/jobs.py::run_python_in_background::subprocess.Popen": 1,
    "tools/schedule_ops.py::_run::subprocess.run": 1,
    "tools/shell_extra.py::pwsh_exec::subprocess.run": 1,
    "tools/shell_extra.py::pwsh_exec_file::subprocess.run": 1,
    "tools/unreached.py::cross_language_text::subprocess.run": 1,
    "tools/unreached.py::tracked_files::subprocess.run": 1,
}


def _inventory():
    return L.inventory(REPO)


def test_no_new_launch_site_appears_without_deciding_its_console():
    """増えた側。新しい起動は `creationflags` を述べるか、ここに理由とともに載せるか。"""
    inv = _inventory()
    grew = {}
    for k, n in sorted(inv.items()):
        was = BASELINE.get(k, 0)
        if n > was:
            grew[k] = (was, n)
    assert not grew, (
        "new child-process launch(es) that inherit whatever console they are given:\n  "
        + "\n  ".join("%s  (was %d, now %d)" % (k, a, b) for k, (a, b) in grew.items())
        + "\n\nA launch whose parent has no console makes Windows give the child a NEW one, "
          "and Windows Terminal shows it. Either pass creationflags (see "
          "tools.childproc.headless_creationflags for the unattended case, and read its "
          "docstring first -- it is not safe for anything that prompts), or add the site "
          "here because it is genuinely interactive or genuinely console-inheriting.")


def test_the_baseline_does_not_outlive_the_sites_it_names():
    """減った側。**片方向のラチェットは、直ったことを検出できない。**

    誰かが起動地点に `creationflags` を足したら、その行はもう現実を説明していない。
    残しておくと、次の読み手は「まだ未決定の123箇所がある」と読む。落として消させる。
    """
    inv = _inventory()
    stale = {}
    for k, n in sorted(BASELINE.items()):
        now = inv.get(k, 0)
        if now < n:
            stale[k] = (n, now)
    assert not stale, (
        "the baseline claims launch sites that no longer exist or no longer inherit:\n  "
        + "\n  ".join("%s  (listed %d, found %d)" % (k, a, b) for k, (a, b) in stale.items())
        + "\n\nSomebody decided these. Remove them from BASELINE so the table describes the "
          "repository rather than the day it was written.")


def test_no_launch_detaches_its_child_from_every_console():
    """「決めている」のに窓を出す唯一の決定。`DETACHED_PROCESS` は子からコンソールを奪い、
    venv の python.exe が起こす本体の python.exe (孫) に**新しい見える窓**を割り当てさせる。
    2026-09-24 に relay/fleet_retention.py の掃除プロセスがデスクトップに窓を出したのを
    scripts/win/console_flash_watch.py が捕まえた。同じ発見の3件目。"""
    uses = L.detached_uses(REPO)
    assert not uses, (
        "DETACHED_PROCESS in tracked code:\n  " + "\n  ".join(uses)
        + "\n\nIt leaves the child with no console, so the venv launcher's grandchild (the real "
          "interpreter) is given a NEW visible one. Use CREATE_NO_WINDOW alone "
          "(tools.childproc.headless_creationflags): the child gets a console with no window "
          "and its children inherit it.")


def test_the_detached_scan_can_see_every_spelling(tmp_path):
    """計器の検査: 3つの書き方すべてを拾い、説明の文章は拾わない。"""
    import subprocess as _sp
    (tmp_path / "m.py").write_text(
        '"""DETACHED_PROCESS is explained here."""\n'
        "import subprocess\n"
        "# DETACHED_PROCESS in a comment\n"
        "a = subprocess.DETACHED_PROCESS\n"
        "b = getattr(subprocess, 'DETACHED_PROCESS', 0)\n"
        "from subprocess import DETACHED_PROCESS\n"
        "c = DETACHED_PROCESS\n", encoding="utf-8")
    _sp.run(["git", "init", "-q", str(tmp_path)], check=True,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    _sp.run(["git", "-C", str(tmp_path), "add", "m.py"], check=True,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    assert L.detached_uses(str(tmp_path)) == ["m.py:4", "m.py:5", "m.py:7"]


def test_the_scanner_still_sees_the_call_shape_it_is_looking_for():
    """**計器そのものの検査。** 走査が壊れて0件を返したら、上の2件は黙って通る。

    `subprocess` の呼び名が変わったり AST の形が変わったりして拾えなくなったとき、
    「増えていない」は「見ていない」と区別がつかない。ここで下限を持つ。
    """
    inv = _inventory()
    assert len(inv) >= 50, (
        "the scanner found only %d launch keys; it used to find over a hundred, so it is "
        "more likely broken than the repository is clean" % len(inv))
    hits = L.scan_file("tools/childproc.py", REPO)
    assert any(h["callee"] == "subprocess.run" for h in hits), \
        "the scanner can no longer see subprocess.run in the module that wraps it"
