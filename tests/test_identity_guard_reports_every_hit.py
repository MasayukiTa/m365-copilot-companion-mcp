"""漏洩検査が件数を過少に言わないこと。

2026-08-28、`scripts/win/lean_capture_isolate.py` は同じホームディレクトリを2行に持っていた。
CI は `IDENTIFYING CONTENT IN TRACKED FILES (1)` と出し、14行目だけを挙げた。
読んだ人間(私)は14行目を直し、それで完了として push しかけた。

**害は件数の側にある**。(1) は「問題は1つ」と読める。つまり報告は、名指しした行さえ消せば
そのファイルは綺麗だと**積極的に主張していた**。次の CI で捕まるので何も外には出ないが、
漏洩検査が漏洩を過少申告するのは、出力を数行節約するために信用を払う取引になる。

「綺麗な作業ツリーで例外が出ない」ことは何の証明にもならない — 測りたい事象を母集団が
含んでいない。だからここでは**2件入りのファイルを実際に作って**数える。
"""
import io
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

#: 検査が探す「Windows のホームディレクトリ」の形。**組み立てる。書かない。**
#:
#: このファイルは「tracked file にその形があると落ちる検査」を試験する。だから形を文字列
#: リテラルで書くと、検査は自分の試験ファイルで落ちる — 実際、最初の push でそうなった。
#: 逃げ道として偽ユーザ名を NON_IDENTIFYING_USERS に足すこともできたが、**発火するたびに
#: 育つ許可リストは、もう検査ではない**。組み立てれば検査は厳しいままでいられる。
BS = chr(92)
HOME = "C:" + BS + "Users" + BS + "somebody0001" + BS + "proj"


def _repo_with(tmp_path, name, body):
    """本物の git リポジトリ。検査は tracked file しか見ないので add まで必要。"""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    io.open(str(tmp_path / name), "w", encoding="utf-8", newline="\n").write(body)
    subprocess.run(["git", "add", name], cwd=str(tmp_path), check=True)
    return str(tmp_path)


def test_two_occurrences_in_one_file_are_both_reported(tmp_path):
    import check_no_identifying_names as G

    body = "\n".join([
        "import sys",
        'sys.path.insert(0, r"%s")' % HOME,
        "x = 1",
        'CONFIG = r"%s%s.env"' % (HOME, BS),
        "",
    ])
    repo = _repo_with(tmp_path, "leaky.py", body)
    hits = G.offences(repo=repo)
    lines = sorted(h[2] for h in hits if h[0] == "leaky.py")
    assert lines == [2, 4], (
        "両方の行が挙がっていない: %r -- 1件しか出ないなら、報告を読んだ人は"
        "名指しされた行だけ直して完了と判断する" % (hits,))


def test_a_clean_file_still_reports_nothing(tmp_path):
    """緩めた結果、綺麗なファイルまで挙げるようになっていないこと。"""
    import check_no_identifying_names as G

    repo = _repo_with(tmp_path, "clean.py", "import os\nREPO = os.path.dirname(__file__)\n")
    assert [h for h in G.offences(repo=repo) if h[0] == "clean.py"] == []


def test_one_file_cannot_become_the_whole_report(tmp_path):
    """1ファイルの列挙には上限があること。生成物1つでログが埋まらないように。"""
    import check_no_identifying_names as G

    n = G.MAX_HITS_PER_FILE + 15
    body = "\n".join(['p%d = r"%s"' % (i, HOME) for i in range(n)]) + "\n"
    repo = _repo_with(tmp_path, "many.py", body)
    hits = [h for h in G.offences(repo=repo) if h[0] == "many.py"]
    numbered = [h for h in hits if h[2] > 0]
    assert len(numbered) == G.MAX_HITS_PER_FILE
    assert any("not listed" in h[1] for h in hits), (
        "打ち切ったことを言っていない -- 黙って切ると『これで全部』に見える")


def test_this_test_file_does_not_itself_trip_the_check():
    """自分自身が検査に引っかからないこと。最初の push で実際に落ちた。"""
    import check_no_identifying_names as G

    rel = os.path.join("tests", "test_identity_guard_reports_every_hit.py").replace("\\", "/")
    assert [h for h in G.offences(repo=REPO) if h[0] == rel] == []


def test_the_repository_itself_is_clean():
    """今の作業ツリーに識別子が無いこと。これが本番の主張。"""
    import check_no_identifying_names as G

    assert G.offences(repo=REPO) == []
