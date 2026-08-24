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
