"""再開放したのに「タブに落ちています」と言い続けないこと。

`RouteIsClosed()` は走行開始以降の `route_closed` を**1件でも**見つけたら閉鎖と答えていた。
再開放が存在しなかった間はそれで正しかった。存在する今は、経路が戻った後も走行の最後まで
琥珀色のままになり、**ドットが既に去った状態を説明する**。

台帳の側も同じ話で、閉鎖だけ書いて開放を書かなければ、それを読む誰も戻ったことを知れない。
だから2つ一組で見張る: 記録が出ること、読む側が最後の1件で判断すること。
"""
import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_reopen_is_written_to_the_ledger():
    """開放を書かなければ、台帳には閉鎖だけが残る。"""
    src = _src("relay", "relay_fleet.py")
    assert '"route_reopened"' in src, "再開放が台帳に記録されていない"
    # 捨てられる前の route に書くこと -- ログの場所を持っているのはそちら。
    i, j = src.index('"route_reopened"'), src.index("reset_socket_route()",
                                                    src.index('"route_reopened"') - 900)
    assert i < j, "reset_socket_route() の後に記録しようとしている(その route はもう無い)"


def test_the_dot_takes_the_newest_route_event():
    """閉鎖1件で即断せず、閉鎖と開放の**新しい方**を採ること。"""
    src = _src("ui", "FleetCockpit.cs")
    body = src[src.index("bool RouteIsClosed()"):]
    body = body[:body.index("\n    DateTime RunStartedLocal()")]

    assert "route_reopened" in body, "開放イベントを読んでいない"
    assert "return true;" not in body, (
        "閉鎖を1件見つけた時点で true を返している -- その後の開放が読まれない")
    assert re.search(r"closed\s*=\s*isClose", body), \
        "最後に見たイベントで上書きしていない"
    assert re.search(r"return\s+closed\s*;", body), "走査後の結論を返していない"


def test_both_events_carry_the_timestamp_key_the_scan_looks_for():
    """走査は `\"at\": \"` を探す。両方のイベントが同じ書き方で出ること。

    ここがずれると走査は黙って0件になり、**fail-open** する — 経路がどうなっていても
    「開いています」と答える。過去に一度この形で壊れている。
    """
    cs = _src("ui", "FleetCockpit.cs")
    assert 'const string AtKey = "\\"at\\": \\""' in cs

    # 記録側: record() は json.dumps で書く(コロンの後に空白が入る)。
    sr = _src("relay", "socket_route.py")
    assert "json.dumps" in sr, "記録の書き方が変わった -- AtKey の空白仮定を見直すこと"
