"""完了したのに履歴に無いワーカーが出る件。

## 症状と実測(2026-08-28)

`.fleet/transcripts/` のファイル名(`r<started16進>_a<attempt>_<worker>`)で実際に走った
ワーカーを数え、`.fleet/history.json` のキー(`started#name`)と突き合わせた結果:

    直近8走行のうち **7走行**で1体以上が履歴に無い。
    欠落は**最後に終わったワーカー**であることが多い(8走行中5走行)。
    1ゴールだけの走行は **0/1** — 唯一のワーカーが履歴に入らなかった。

## 原因

`status.json` の `running` は「全ワーカーが terminal になった瞬間」に false になる。
コックピットは `if (runningNow) ArchiveTerminal(root);` としていたので、
**最後の1体が terminal になったスイープは、この行が走らないスイープ**。
そのワーカーが terminal である状態で archive が走る機会は一度も来ない。

1ゴール走行が 0/1 なのはこの説明の一番きれいな形で、他の説明では出せない数字。

## なぜ毎ティック archive してはいけないのか(元の設計の言い分)

終了した走行のスナップショットは status.json に残り続けるので、毎ティック archive すると
「履歴を空にする」で消したカードが毎ティック復活する。だから走行ごとに**一度だけ**走らせる。
"""
import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cs():
    with io.open(os.path.join(REPO, "ui", "FleetCockpit.cs"), encoding="utf-8") as fh:
        return fh.read()


def test_a_finished_run_gets_one_more_archive_pass():
    src = _cs()
    assert "else ArchiveRunTailOnce(root);" in src, (
        "走行終了後の archive パスが無い -- 最後に終わったワーカーは履歴に入らない")
    body = src[src.index("void ArchiveRunTailOnce("):]
    body = body[:body.index("void ArchiveTerminal(")]
    assert "_tailArchivedRunStarted == started" in body, "走行ごとに一度だけ、になっていない"
    assert "_tailArchivedRunStarted = started;" in body, "実行済みの印を付けていない"
    assert "ArchiveTerminal(root)" in body, "スナップショットから archive していない"


def test_the_tail_pass_reads_the_snapshot_not_the_visible_cards():
    """画面のフィルタに左右されないこと。

    `MaybeAutoArchive` は `_toolbarShown`(表示中のカード)を回すので、完了タブを開いたまま
    走行が終わると DONE 以外は履歴に入らない。末尾パスは snapshot を直接見る。
    """
    src = _cs()
    body = src[src.index("void ArchiveRunTailOnce("):]
    body = body[:body.index("void ArchiveTerminal(")]
    assert "_toolbarShown" not in body, (
        "末尾パスが表示中カードに依存している -- タブのフィルタ次第で履歴が欠ける")


def _cs_terminal_statuses(func_src):
    return set(re.findall(r'status == "([a-z_]+)"', func_src))


def test_both_cockpit_terminal_tests_match_python():
    """C# の terminal 判定2か所が relay_fleet.TERMINAL と一致すること。

    ここがずれると、欠けたステータスのワーカーが履歴に入らないだけでなく、
    `MaybeAutoArchive` が「まだ終わっていない」と判断して**走行全体**の自動アーカイブが止まる。
    """
    from relay.relay_fleet import TERMINAL
    expected = set(TERMINAL)

    src = _cs()
    it = src[src.index("static bool IsTerminalWorker("):]
    it = it[:it.index("\n    }")]
    assert _cs_terminal_statuses(it) == expected, (
        "IsTerminalWorker が Python の TERMINAL と違う: C#=%r python=%r"
        % (sorted(_cs_terminal_statuses(it)), sorted(expected)))

    at = src[src.index("void ArchiveTerminal("):]
    at = at[:at.index("if (added) SaveHistory();")]
    got = _cs_terminal_statuses(at)
    assert got == expected, (
        "ArchiveTerminal が Python の TERMINAL と違う: C#=%r python=%r"
        % (sorted(got), sorted(expected)))


def test_the_live_guard_is_still_there():
    """終了後に毎ティック archive するようになっていないこと。

    元の言い分(消したカードが復活する)は今も正しい。緩めたのは『一度だけ足す』ところだけ。
    """
    src = _cs()
    assert "if (runningNow) ArchiveTerminal(root);" in src, (
        "走行中の毎ティック archive が消えている")
