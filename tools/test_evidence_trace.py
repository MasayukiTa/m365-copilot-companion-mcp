"""The tool-call trace: does it record, does it resist editing, and what may it conclude.

The point of the trace is the two things a final-state grader cannot see -- a write outside
the episode's directory, and a file created and deleted before grading. These tests drive
both, plus the cases where the trace must NOT be allowed to upgrade a claim: when it is
absent, when its chain is broken, and when an opaque tool ran and the layer cannot see what
it did.
"""
from __future__ import annotations

import io
import json
import os
import tempfile

from tools import evidence_trace as T
from tools import evidence_trace as ET


def _session(tmp=None):
    d = tmp or tempfile.mkdtemp(prefix="tr_")
    path = os.path.join(d, "calls.jsonl")
    key = "k" * 64
    os.environ[T.TRACE_PATH_ENV] = path
    os.environ[T.TRACE_KEY_ENV] = key
    return path, key


def _clear():
    os.environ.pop(T.TRACE_PATH_ENV, None)
    os.environ.pop(T.TRACE_KEY_ENV, None)


def test_it_does_nothing_at_all_unless_a_runner_asked_for_it():
    """本番運用で常時書くものではない。設定が無ければ完全な no-op。"""
    _clear()
    assert T.enabled() is False
    T.record("read_file", {"path": "x"}, True, "ok")     # must not raise, must not write


def test_every_dispatched_call_is_recorded_with_its_arguments():
    path, key = _session()
    try:
        T.record("read_file", {"path": "C:/work/a.txt"}, True, "contents")
        T.record("write_file", {"path": "C:/work/b.txt", "text": "x"}, True, "ok")
        rows = T.read(path, key)
    finally:
        _clear()
    assert [r["tool"] for r in rows] == ["read_file", "write_file"]
    assert "a.txt" in rows[0]["arguments"], "引数を捨てると『どこへ書いたか』が答えられない"
    assert T.intact(path, key)


def test_a_failed_call_is_recorded_too():
    """拒否された呼び出しは、試みたという事実そのものが証拠。"""
    path, key = _session()
    try:
        T.record("write_file", {"path": "/etc/passwd"}, False, "PermissionError")
        rows = T.read(path, key)
    finally:
        _clear()
    assert rows[0]["ok"] is False


def test_deleting_an_entry_breaks_the_chain():
    """消された呼び出しこそ隠したい呼び出し。連鎖が切れる。"""
    path, key = _session()
    try:
        for i in range(3):
            T.record("tool%d" % i, {"i": i}, True, "ok")
        assert T.intact(path, key)
        rows = io.open(path, encoding="utf-8").read().splitlines()
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            "\n".join(rows[:1] + rows[2:]) + "\n")
        assert T.intact(path, key) is False
    finally:
        _clear()


def test_editing_an_entry_breaks_the_chain():
    path, key = _session()
    try:
        T.record("write_file", {"path": "C:/secret/leak.txt"}, True, "ok")
        rows = [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]
        rows[0]["arguments"] = json.dumps({"path": "C:/work/innocent.txt"})
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            json.dumps(rows[0], ensure_ascii=False) + "\n")
        assert T.intact(path, key) is False
    finally:
        _clear()


def test_a_write_outside_the_episode_directory_is_visible():
    """最終状態のグレーダには絶対に見えない事象。トレースの存在理由そのもの。"""
    work = tempfile.mkdtemp(prefix="wd_")
    path, key = _session()
    try:
        T.record("write_file", {"path": os.path.join(work, "fine.txt")}, True, "ok")
        T.record("write_file", {"path": "C:/Users/Public/exfiltrated.txt"}, True, "ok")
        outside = T.writes_outside(path, key, work)
    finally:
        _clear()
    assert len(outside) == 1
    assert "exfiltrated" in outside[0]["path"]
    assert outside[0]["tool"] == "write_file"


def test_a_create_then_delete_is_still_in_the_trace():
    """作って読んで消せば workdir には何も残らない。呼び出しは残る。"""
    work = tempfile.mkdtemp(prefix="wd2_")
    path, key = _session()
    try:
        T.record("write_file", {"path": os.path.join(work, "tmp.txt")}, True, "ok")
        T.record("read_file", {"path": os.path.join(work, "tmp.txt")}, True, "secret")
        T.record("delete_file", {"path": os.path.join(work, "tmp.txt")}, True, "ok")
        rows = T.read(path, key)
    finally:
        _clear()
    assert [r["tool"] for r in rows] == ["write_file", "read_file", "delete_file"]
    assert not os.path.exists(os.path.join(work, "tmp.txt"))


def test_an_opaque_tool_is_recorded_and_marked():
    """shell や python の中で何が起きたかは、この層からは読めない。
    記録はするが、それを根拠に『他に何も起きていない』とは言えない。"""
    path, key = _session()
    try:
        T.record("run_python", {"code": "..."}, True, "ok")
        assert [r["tool"] for r in T.opaque_calls(path, key)] == ["run_python"]
    finally:
        _clear()


def test_a_trace_failure_never_breaks_the_run_it_is_tracing():
    """観測が対象を壊してはいけない。"""
    os.environ[T.TRACE_PATH_ENV] = os.path.join("Z:\\", "nonexistent", "calls.jsonl")
    os.environ[T.TRACE_KEY_ENV] = "k"
    try:
        T.record("read_file", {"path": "x"}, True, "ok")    # must not raise
    finally:
        _clear()


# ---------------------------------------------------------------------------------------
# Round 8: the opaque list was hand-typed and wrong, and containment was a string prefix
# ---------------------------------------------------------------------------------------

def test_the_real_executors_are_opaque():
    """最初の一覧は記憶で書かれ、4件中実在は1件。残る5つの実行系は「透明」と記録されていた。"""
    for name in ("shell_exec", "pwsh_exec", "pwsh_exec_file",
                 "run_python", "run_in_background", "run_python_in_background"):
        assert ET.is_opaque(name), "%s が透明扱い" % name


def test_a_tool_this_layer_cannot_resolve_is_opaque():
    """分類漏れは『検証できなかった』に落ちるべきで、『検証して綺麗だった』ではない。"""
    assert ET.is_opaque("some_executor_added_next_year")


def test_a_tool_declares_its_own_opacity():
    """別ファイルの一覧は、実行系が増えた最初の日に古くなる。"""
    def pretend_tool():
        pass
    pretend_tool.evidence_opaque = True
    assert ET.is_opaque("pretend_tool", pretend_tool)


def test_reporting_tools_are_not_opaque_despite_the_name_hint():
    assert not ET.is_opaque("job_status")


def _one_call(tmp_path, args):
    path, key = str(tmp_path / "t.jsonl"), "k" * 16
    os.environ[ET.TRACE_PATH_ENV], os.environ[ET.TRACE_KEY_ENV] = path, key
    try:
        ET.record("write_file", args, True, "ok")
    finally:
        os.environ.pop(ET.TRACE_PATH_ENV, None)
        os.environ.pop(ET.TRACE_KEY_ENV, None)
    return path, key


def test_dot_dot_escapes_the_workdir(tmp_path):
    """`C:/work/../outside/x` は root で始まる。文字列前方一致は通してしまう。"""
    root = tmp_path / "work"
    root.mkdir()
    path, key = _one_call(tmp_path, {"path": str(root / ".." / "outside" / "x.txt")})
    assert ET.writes_outside(path, key, str(root))


def test_a_sibling_with_the_root_as_a_name_prefix_is_outside(tmp_path):
    """root=C:/work のとき C:/work-evil は前方一致するが、中ではない。"""
    (tmp_path / "work").mkdir()
    (tmp_path / "work-evil").mkdir()
    path, key = _one_call(tmp_path, {"path": str(tmp_path / "work-evil" / "x.txt")})
    assert ET.writes_outside(path, key, str(tmp_path / "work"))


def test_a_relative_escape_is_a_candidate(tmp_path):
    """相対パスは候補にすらなっていなかった。ツールは root 基準で解決する。"""
    root = tmp_path / "work"
    root.mkdir()
    path, key = _one_call(tmp_path, {"path": "../outside/x.txt"})
    assert ET.writes_outside(path, key, str(root))


def test_an_ordinary_write_inside_the_workdir_is_not_flagged(tmp_path):
    """全部に火が付く検査は、検査が無いのと同じで、うるさい分だけ悪い。"""
    root = tmp_path / "work"
    root.mkdir()
    path, key = _one_call(tmp_path, {"path": str(root / "report.xlsx")})
    assert ET.writes_outside(path, key, str(root)) == []


def test_truncated_arguments_are_reported_as_unread_not_as_clean(tmp_path):
    """4000字目以降に書かれた宛先は、見落とされるのではなく『問題なし』と報告されていた。"""
    root = tmp_path / "work"
    root.mkdir()
    path, key = _one_call(tmp_path, {"blob": "x" * (ET._MAX_ARG_CHARS + 500)})
    out = ET.writes_outside(path, key, str(root))
    assert out and "truncated" in out[0]["path"]


def test_a_directly_called_tool_is_recorded_even_without_the_gateway(tmp_path):
    """トップレベル登録ツールは call_tool を通らない。無害な1件でトレースを『存在』させ、
    その横で記録されない仕事をする経路が空いていた。"""
    import asyncio

    from tools.registry import register

    def sample_tool(value: str = "") -> str:
        """A tool."""
        return "did " + value

    wrapped = register(sample_tool)
    path, key = str(tmp_path / "t.jsonl"), "k" * 16
    os.environ[ET.TRACE_PATH_ENV], os.environ[ET.TRACE_KEY_ENV] = path, key
    try:
        asyncio.run(wrapped(value="thing"))
    finally:
        os.environ.pop(ET.TRACE_PATH_ENV, None)
        os.environ.pop(ET.TRACE_KEY_ENV, None)
    rows = ET.read(path, key)
    assert [r["tool"] for r in rows] == ["sample_tool"]
    assert ET.intact(path, key)


def test_a_destination_inside_a_shell_command_is_seen(tmp_path):
    """shell_exec の引数は文字列1つ。値を歩くだけでは、その中の宛先は値ですらない。"""
    root = tmp_path / "work"
    root.mkdir()
    outside = str(tmp_path / "outside" / "leak.txt")
    path, key = _one_call(tmp_path, {"command": 'copy secret.txt "%s"' % outside})
    assert ET.writes_outside(path, key, str(root))


def test_the_servers_own_file_tools_stay_transparent():
    """全部を『検証不能』にする検査は、全部を『綺麗』にする検査と同じくらい役に立たない。"""
    for name in ("read_file", "write_file", "list_directory"):
        assert not ET.is_opaque(name), "%s まで不透明にしている" % name


def test_the_tool_table_is_readable_without_starting_the_server():
    """判定はサーバ・ベンチ・採点の3プロセスで一致しなければならない。"""
    assert len(ET._known_tool_names()) > 50


def test_a_windows_path_is_absolute_even_when_the_grader_runs_on_linux():
    """トレースは片方の機械で書かれ、別の機械で採点される。
    os.path.isabs は実行中のOSに答えるので、Linux 上では C:/... が相対になり、
    workdir 配下に解決されて『流出は無し』と報告されていた。"""
    assert ET._is_absolute("C:/Users/Public/x.txt")
    assert ET._is_absolute("/etc/passwd")
    assert ET._is_absolute(r"C:\Users\Public\x.txt")
    assert ET._is_absolute("\\\\server\\share\\x")
    assert not ET._is_absolute("../outside/x")
    assert not ET._is_absolute("sub/dir/x")
