"""「いまどのセッションか」を決める規則を1つにする。

## 併存していた規則

同じ「今の会話」を、経路ごとに別の規則で指していた:

- サーバの `/goal` / `/stream`: `ACTIVE_SID`。起動時の auto-resume が
  **`conv_url` が非空のうち最新**から選び、長い会話のリサイクルでクライアント抜きに動く
- CLI の goal ループ: `list_sessions()[0]` = **`conv_url` が空のものも含めた最新**
- CLI の起動バナー: 同じく `sessions[0]` を表示し、「そのまま打てば続きから」と案内

2つ目と3つ目はサーバの答えを**推測**していた。よく一致するので文面は正しく読めるが、
一致しないとき、操作者は「続かない会話が続く」と告げられ、steer は別の会話に届く。

## 直し方

推測をやめる。`/send` は空の sid を `ACTIVE_SID` に解決するので、**送らない**ことが答えになる。
そして `/sessions` が active な行に印を付けるので、バナーは推測せず名指しできる。

「今日は一致している2つ目の規則」は、明日一致しなくなる2つ目の規則である。
"""
import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_goal_loop_does_not_choose_a_session_itself():
    """CLI の goal ループが sid を選ばないこと。"""
    src = _src("bridge", "session_cli.py")
    body = src[src.index("def run_goal("):]
    body = body[:body.index("\ndef ", 10)]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    assert "sessions[0]" not in code, (
        "goal ループがまだ sessions[0] を sid として選んでいる -- サーバの ACTIVE_SID とは"
        "別の規則なので、食い違ったとき steer が別の会話に届く")


def test_the_server_resolves_an_empty_sid():
    """空の sid をサーバが ACTIVE_SID に解決すること。クライアントが送らない前提の土台。"""
    src = _src("bridge", "copilot_bridge.py")
    i = src.index('if parsed.path == "/send":')
    blk = src[i:i + 900]
    assert "sid = sid or ACTIVE_SID" in blk, (
        "空 sid の解決が無い -- クライアントが送らない設計が成り立たない")


def test_the_sessions_listing_marks_the_active_row():
    src = _src("bridge", "copilot_bridge.py")
    i = src.index('if parsed.path == "/sessions":')
    blk = src[i:src.index('if parsed.path == "/adopt":')]
    assert 's["active"]' in blk, (
        "どの行が active かを返していない -- 読む側は推測するしかなくなる")
    assert "ACTIVE_SID" in blk


def test_the_sessions_listing_admits_when_it_is_cut_short():
    """切ったことを言うこと。黙って切ると、部分的な一覧が完全な一覧に見える。"""
    src = _src("bridge", "copilot_bridge.py")
    i = src.index('if parsed.path == "/sessions":')
    blk = src[i:src.index('if parsed.path == "/adopt":')]
    assert '"truncated"' in blk and '"total"' in blk, (
        "打ち切りを申告していない -- チャットを使うほど古いフリート行が黙って押し出される")
    assert "SESSION_LIST_CAP" in blk, "上限が名前を持っていない(二度書きは drift する)"


def test_the_banner_names_the_active_session_when_it_is_known():
    src = _src("bridge", "session_cli.py")
    body = src[src.index("def _print_banner_and_maybe_resume("):]
    body = body[:body.index("\ndef ", 10)]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    assert 's.get("active")' in code, "active な行を探していない"
    assert "continuing:" in code, "続きから始まる対象を名指ししていない"


def test_the_banner_does_not_claim_continuity_it_cannot_check():
    """active が分からないときに「そのまま打てば続きから」と言わないこと。

    分からないことを、分かっているように言うのがこの欠陥の本体だった。
    """
    src = _src("bridge", "session_cli.py")
    body = src[src.index("def _print_banner_and_maybe_resume("):]
    body = body[:body.index("\ndef ", 10)]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    assert "continue where you left off" not in code, (
        "active を確かめずに『続きから』と断言する文が残っている")
