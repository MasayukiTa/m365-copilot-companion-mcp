"""スラッシュ設定は、どの経路から入力しても**設定**として扱われること。

## 何が起きていたか

コンポーザに `/fanout on` のような設定行を入れて確定する経路は2つある:

- **送信ボタン** — `HandleSlashSetting()` を先に呼ぶ。ここは直っていた
- **Ctrl+Enter** — 呼んでいなかった

送信ボタン側のコメントは、この不具合を実機で見つけた経緯まで書いている:
「`/fanout on` を実際の窓に打ったら settings.txt に fanout キーが無く、走行中のワーカーに
その文字列が渡っていた」。**その修正はボタンに入って、そこで止まっていた。**

そして Ctrl+Enter は、キーボードで操作する人と `scripts/win/submit_via_ui.ps1` が通る経路である。
`submit_via_ui.ps1` の `-Command` は走行中かを見ずに無条件で投入するので(:190、`-Goal` の
走行中ガードは:192〜196で、`-Command` はその外)、**走行中に設定を送ると設定にならず、
ワーカーへの指示文として届いていた**。

同じ故障に2つの入口があるとき、それは1つの故障である。目の前の呼び出し元だけ直すと、
残りで生き続ける。
"""
import re
from pathlib import Path

RAW = (Path(__file__).with_name("FleetCockpit.cs")).read_text(encoding="utf-8")


def _executable(cs):
    """コメントを落としたソース。断言はコメントではなく実行されるコードに対して行う。"""
    out, i, n = [], 0, len(cs)
    while i < n:
        if cs.startswith("//", i):
            j = cs.find(chr(10), i)
            i = n if j < 0 else j
        elif cs.startswith("/*", i):
            j = cs.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(cs[i]); i += 1
    return "".join(out)


SOURCE = _executable(RAW)


def _confirm_paths():
    """コンポーザの入力を確定させる分岐。両方とも `TrySendSteer` / `StartFleet` へ分かれる。"""
    return [m.start() for m in re.finditer(r"if \(_composerRunActive\) TrySendSteer\(\);", SOURCE)]


def test_there_are_exactly_two_confirm_paths():
    """経路が増えたらこのテストを見直すこと。増えた経路は同じ穴を持ちうる。"""
    assert len(_confirm_paths()) == 2, (
        "確定経路が2つでなくなった(%d) -- 新しい経路も HandleSlashSetting を通す必要がある"
        % len(_confirm_paths()))


def test_every_confirm_path_handles_a_slash_setting_first():
    """**両方**の経路が、steer/start より前に HandleSlashSetting を呼ぶこと。"""
    for start in _confirm_paths():
        before = SOURCE[max(0, start - 600):start]
        assert "HandleSlashSetting()" in before, (
            "確定経路のひとつが HandleSlashSetting を呼んでいない -- その経路から入れた"
            "設定行は、設定にならずワーカーへの指示文として届く")


def test_the_slash_handler_short_circuits():
    """設定として扱ったら、そこで止まること。続けて steer すると二重に効く。"""
    for start in _confirm_paths():
        before = SOURCE[max(0, start - 600):start]
        i = before.rindex("HandleSlashSetting()")
        assert "return" in before[i:i + 40], (
            "HandleSlashSetting の後に return していない -- 設定が steer としても送られる")
