"""自動操作用の Edge が、利用者のタスクバーに居座らないことを固定する。

同じ取りこぼしを3回やっている。どれも「フリート側(:9222 copilot-companion-edge)に
入れて、ブリッジ側(:9223 copilot-bridge-edge)に広げていない」という同じ形:

  1. rehide() がプロファイル決め打ちで、ブリッジを出しても戻せなかった
  2. 仮想デスクトップへ退ける移動スクリプトも決め打ちだった
  3. 最小化しただけではタスクバーのボタンは残る（利用者が見ているのはそれ）

実測(2026-08-07): フリート側は visible=False で窓なし、ブリッジ側は
visible=True/iconic=True の最小化窓。後者だけタスクバーに出ていた。

窓を消す(SW_HIDE)のは不可。Edge がタブの描画を捨て、CDP が切れる。
WS_EX_TOOLWINDOW を立てるとタスクバーと Alt+Tab から外れ、窓は生きたまま残る。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECOVER = ROOT / "relay" / "edge_recover.py"
BRIDGE = ROOT / "bridge" / "copilot_bridge.py"
MOVER = ROOT / "scripts" / "win" / "move_companion_to_desktop.ps1"
KEEPER = ROOT / "scripts" / "win" / "edge_keeper.ps1"


def _src(p):
    return p.read_text(encoding="utf-8-sig")


def test_rehide_takes_a_port_or_profile():
    """どの Edge を戻すのか、呼び出し側が指定できること。

    決め打ちだと、ブリッジがいくら rehide を呼んでもフリートの窓を隠すだけで、
    自分の窓は出たままになる。
    """
    src = _src(RECOVER)
    sig = re.search(r"def rehide\(([^)]*)\)", src).group(1)
    assert "port" in sig or "profile" in sig, sig


def test_rehide_snippet_is_not_hardcoded_to_one_profile():
    src = _src(RECOVER)
    snippet = src[src.index("_REHIDE_PS"):src.index("def rehide")]
    assert "__PROFILE__" in snippet, "置換される目印が無い＝決め打ちに戻っている"
    assert "'copilot-companion-edge'" not in snippet


def test_the_mover_takes_a_profile_too():
    src = _src(MOVER)
    assert "$ProfileMarker" in src
    assert "-match 'copilot-companion-edge'" not in src


def test_rehide_removes_the_taskbar_button_not_just_minimizes():
    """最小化だけで終えないこと。

    最小化した窓はタスクバーのボタンを持ち続ける。利用者が見て困るのはそれ。
    """
    src = _src(RECOVER)
    snippet = src[src.index("_REHIDE_PS"):src.index("def rehide")]
    assert "0x80" in snippet, "WS_EX_TOOLWINDOW を立てていない"
    assert "SetWindowLong" in snippet


def test_sw_hide_is_never_left_set():
    """SW_HIDE で終わらないこと。

    隠したままにすると Edge がタブの描画を捨て、駆動中に CDP が切れる
    (TargetClosedError)。属性を変える一瞬だけ使い、必ず最小化へ戻す。
    """
    src = _src(RECOVER)
    snippet = src[src.index("_REHIDE_PS"):src.index("def rehide")]
    calls = re.findall(r"ShowWindow\(\$h,\s*(\d+)\)", snippet)
    assert calls, "ShowWindow の呼び出しが無い"
    assert calls[-1] != "0", "最後が SW_HIDE のままだと描画が捨てられる: %s" % calls

    # keeper にも同じ基準を適用する。最小化だけでは taskbar ボタンが残るため、keeper も
    # WS_EX_TOOLWINDOW を立てるようになった（属性変更には一瞬の SW_HIDE が要る）。
    # 禁じるのは「使うこと」ではなく「隠したまま終えること」。
    keeper_calls = re.findall(r"ShowWindow\(\$h,\s*(\d+)\)", _src(KEEPER))
    assert keeper_calls, "keeper に ShowWindow の呼び出しが無い"
    assert keeper_calls[-1] != "0", "keeper が SW_HIDE で終えている: %s" % keeper_calls
    for i, c in enumerate(keeper_calls):
        if c == "0":
            assert "6" in keeper_calls[i + 1:], "keeper が隠したまま戻していない: %s" % keeper_calls


def test_the_bridge_actually_asks_for_its_own_edge():
    """仕組みがあっても、呼ばれなければ何も起きない。"""
    # コメントや説明文の中の rehide() を拾わないよう、行頭が呼び出しの行だけ見る。
    calls = []
    for line in _src(BRIDGE).splitlines():
        body = line.strip()
        if body.startswith("#") or body.startswith('"'):
            continue
        m = re.match(r"rehide\(([^)]*)\)", body)
        if m:
            calls.append(m.group(1))
    assert calls, "ブリッジが rehide を呼んでいない"
    for c in calls:
        assert "port=" in c, "ブリッジが自分のポートを渡していない: rehide(%s)" % c


def test_the_bridge_rehides_at_startup():
    """前回の実行から残った窓も片づくこと。

    surface されなかった回でも、窓だけ生き残っていることがある。
    """
    src = _src(BRIDGE)
    assert "startup rehide raised" in src


# ---- 4回目: 評価用 Edge(:9224) が監視の既定から漏れていた ----------------------------------

def test_every_managed_profile_is_watched_by_the_keeper():
    """プロファイルが増えるたびに、それを列挙している箇所を掃き漏らしてきた。

    4回目は :9224 の評価用 Edge。keeper の既定マーカーに名前が無いので、
    測定系列が開いた窓が運用者の前面に出たまま、誰も片付けなかった。
    症状はこのループが存在する理由そのもの。

    一覧を1箇所にしただけでは再発する。keeper が実際にその全部を見ていることを
    ここで固定して初めて、追加が監視に届く。"""
    import sys
    sys.path.insert(0, str(ROOT))
    from relay.edge_recover import MANAGED_EDGE_PROFILES, keeper_profile_marker

    keeper = (ROOT / "scripts" / "win" / "edge_keeper.ps1").read_text(encoding="utf-8")
    default = re.search(r"\$ProfileMarker\s*=\s*'([^']+)'", keeper).group(1)
    for profile in set(MANAGED_EDGE_PROFILES.values()):
        assert profile in default, "keeper が %s を見ていない" % profile
    # 逆向きも: keeper が知っていて登録に無い名前があれば、正本が2つになる
    for name in default.split("|"):
        assert name in set(MANAGED_EDGE_PROFILES.values()), "登録に無い名前: %s" % name
    assert keeper_profile_marker().split("|"), keeper_profile_marker()


def test_the_port_map_knows_the_evaluation_browser():
    """:9224 を既定へ落とすと、rehide が別の Edge を最小化しにいく。"""
    import sys
    sys.path.insert(0, str(ROOT))
    from relay.edge_recover import _profile_for_port
    assert _profile_for_port(9224) == "copilot-eval-edge"
    assert _profile_for_port(9223) == "copilot-bridge-edge"


# ---- 5回目: 印を付ける側が、正しい手順を持つ側を無効化していた --------------------------------

def _files_that_mark_windows():
    """WS_EX_TOOLWINDOW(0x80) を立てているファイルを、名前で覚えずに探す。

    ここが要点。これまで4回とも「新しく Edge の窓に触る場所が増えたのに、
    列挙している側を掃いていない」だった。覚えていたファイルを検査する限り、
    5回目は次に増えた場所で起きる。"""
    out = []
    for path in list(ROOT.glob("scripts/**/*.ps1")) + list(ROOT.glob("relay/**/*.py")) + \
            list(ROOT.glob("scripts/**/*.py")):
        if "test" in path.name:
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "SetWindowLong" in src and "0x80" in src:
            out.append(path)
    return out


def test_anything_that_marks_a_window_uses_the_hide_bracket():
    """シェルは『窓が表示された瞬間』にタスクバーボタンを出すか決める。
    可視のままスタイルを変えても Explorer は評価し直さないので、最初の表示で
    作られたボタンは残り続ける。正しい手順は 隠す → スタイル変更 → 表示。

    自作の起動器はこれを持たず（最小化→変更→最小化）、しかも 0x80 を立てたことで
    edge_keeper と rehide の `if (($ex -band 0x80) -eq 0)` を素通りさせ、
    正しい手順を持つ2つを両方とも無効化していた。動いていたのに何もしなかった。"""
    for path in _files_that_mark_windows():
        src = path.read_text(encoding="utf-8", errors="ignore")
        assert re.search(r"ShowWindow\([^)]*,\s*0\s*\)", src) or "SW_HIDE" in src, (
            "%s は 0x80 を立てるが、隠す手順を持たない" % path.name)


def test_the_eval_launcher_does_not_mark_windows_at_all():
    """窓を作らせなければ、隠す競争に負けようがない。
    正規の起動器は既定で --headless=new -- 窓なし・タスクバーなし。"""
    src = (ROOT / "scripts" / "start_eval_edge.ps1").read_text(encoding="utf-8")
    assert "SetWindowLong" not in src, "また自前で印を付けている"
    assert "start_companion_edge.ps1" in src, "正規の起動器に委ねていない"
    # 文字列としての -Foreground は禁じない -- 拒否メッセージが
    # 「サインインしたいなら -Foreground で」と案内するのは正しい。
    # 見るべきは起動器を「どう呼んでいるか」の一行だけ。
    call = [ln for ln in src.splitlines() if "start_companion_edge.ps1" in ln]
    assert call, "起動器を呼ぶ行が無い"
    for ln in call:
        assert "-Foreground" not in ln, "起動器を前面モードで呼んでいる: %s" % ln.strip()


def test_the_eval_launcher_proves_nothing_is_showing_before_it_succeeds():
    """3回続けて「直った」と思い、3回とも運用者に見つけられた。
    次は無い、という指示を受けたので、祈りではなく機構にする。

    このプロファイルの窓が許されるのは「画面外」かつ「タスクバー外」のときだけ。
    唯一の例外はサインイン面で、そこは人が見る必要がある場面。それ以外は
    起動そのものを失敗させる -- 誰かの前に窓が居座ったまま走る測定に、
    データとしての価値は無い。"""
    src = (ROOT / "scripts" / "start_eval_edge.ps1").read_text(encoding="utf-8")
    shared = ROOT / "scripts" / "win" / "eval_windows.ps1"

    # 判定は1箇所にだけ在ること。起動器と実行中の監視が別々の定義を持てば、
    # やがて片方が「隠れている」と言い続ける -- 誰かの前に窓が在るのに。
    assert shared.is_file(), "共有の判定スクリプトが無い"
    pred = shared.read_text(encoding="utf-8")
    assert "function Get-VisibleEvalWindows" in pred
    assert "function Get-VisibleEvalWindows" not in src, "起動器が判定を複製している"
    assert "eval_windows.ps1" in src, "起動器が共有の判定を読み込んでいない"

    # 画面内か、タスクバーに出るか、どちらでも該当させること
    assert "onScreen" in pred and "inTaskbar" in pred

    # 監視は読むだけ -- 起動も停止もしないこと。
    # 走行中の可視性を「確認」するはずの命令が起動器を呼ぶ形で書かれており、
    # 走れば監視対象の測定そのものを殺していた。
    for banned in ("Stop-Process", "Start-Process", "taskkill", "start_companion_edge"):
        assert banned not in pred, "監視が %s を持っている" % banned

    # 起動器: 成功の出口より前に検査があり、該当したら失敗すること
    assert "REFUSING" in src, "見えていても成功で返している"
    i = src.index("Get-VisibleEvalWindows -Marker")
    j = src.index("no visible window")
    assert i < j, "成功を返してから検査している"
    # サインインは人が見る場面なので、この検査が握りつぶさないこと
    assert "-Foreground" in src


def test_the_keeper_does_not_trust_the_toolwindow_bit_on_a_window_it_has_not_seen():
    """ビットは「タスクバーに居ない」ことの証明にならない。

    シェルはタスクバー所属を『最初に表示された瞬間』に決め、その後スタイルを読み直さない。
    つまり WS_EX_TOOLWINDOW が立っていても、それが立つ前に作られたボタンは残っている --
    他の何かが立てた窓、あるいは2つの呼び出しの間で死んだ前任の keeper が残した窓が、
    ちょうどそう見える。ビットだけで飛ばすと、運用者が文句を言っているボタンをそのまま
    残したまま、ログ上は『対応済み』になる。

    だから keeper がその handle を初めて見た時は、隠して→印を付けて→出し直す手順を
    無条件で通す。所属を再評価させるのは表示し直す行為であって、ビットではない。"""
    src = (ROOT / "scripts" / "win" / "edge_keeper.ps1").read_text(encoding="utf-8")
    assert "HandledWindows" in src, "見た handle を覚えていない"
    # 初見なら、ビットが立っていても手順を通すこと
    i = src.index("$key = [string]$h")
    guard = src[i:i + 460]
    assert "-not $script:HandledWindows.ContainsKey($key)" in guard
    assert "-or" in guard, "初見でもビットが立っていれば飛ばしている"
    # 手順そのものは残っていること
    assert "ShowWindow($h, 0)" in guard and "ShowWindow($h, 6)" in guard
