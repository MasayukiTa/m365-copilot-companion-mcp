"""プロセスを探す述語が、探している文字列を含む自分自身に当たらないこと。

今日3回、同じ形でつまずいた。うち2回は破壊的だった。

  1. 最小化役の稼働確認が「1」と答えたが実体はゼロ。powershell.exe の
     コマンドラインからスクリプト名を探す条件が、その名前を含む自分の
     クエリに当たっていた。評価用 Edge が監視されないまま前面に居座った
     のはこれが理由。
  2. 走行器を止めるコマンドが、これから走行器を起動するシェルに当たった。
     2回とも道連れで、診断ブロックは0本、30分が消えた。

コマンドライン部分一致は自己言及的な述語で、パターンをコマンドに書いた
瞬間にそのコマンドが一致対象になる。プロセス名で先に絞り、かつ自分と
祖先を除外して、初めて閉じる。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FINDER = ROOT / "scripts" / "win" / "find_procs.ps1"


def _src():
    return FINDER.read_text(encoding="utf-8")


def test_the_finder_exists_so_there_is_one_place_to_get_this_right():
    assert FINDER.is_file()


def test_it_filters_by_process_name_before_matching_a_command_line():
    """名前で絞らなければ、その文字列を口にしただけのプロセスが全部候補になる。"""
    src = _src()
    assert re.search(r"Name='\$ProcessName'", src), "プロセス名で絞っていない"
    assert "-ProcessName" in src or "$ProcessName" in src


def test_it_excludes_this_process_and_its_ancestors():
    """自分だけ除いても足りない。呼び出したシェルもラッパーも同じ文字列を持つ。
    親を殺したのが、説明のつかない exit 255 の正体だった。"""
    src = _src()
    assert "Get-AncestorIds" in src
    assert "ParentProcessId" in src
    assert re.search(r"\$mine\s+-notcontains\s+\$_\.ProcessId", src), "祖先を除外していない"


def test_stopping_is_opt_in():
    """既定が停止だと、数えるつもりの呼び出しが消しにいく。"""
    src = _src()
    assert "[switch]$Stop" in src
    i, j = src.index("[switch]$Stop"), src.index("Stop-Process")
    assert i < j
    assert "if ($Stop)" in src


def test_the_ancestor_walk_is_bounded():
    """親を辿る輪が閉じていた場合に、確認処理自体が固まらないこと。"""
    src = _src()
    assert re.search(r"\$i\s*-lt\s*\d+", src), "上限のないループ"
