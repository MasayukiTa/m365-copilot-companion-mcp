"""健康ドット(サインイン/エージェント)の判定規則を、ソースに対して固定する。

## このファイルが一度言っていたこと、そしてなぜ書き換わったか

以前ここは「タブ一覧を見て M365 チャットが開いているか」で判定する規則
(`hasUsableM365Chat` / `LooksLikeUsableM365Chat` / `ExtractTabUrls` / `LooksLikeLoginWall`)
を固定していた。2026-08-28 にその規則ごと削除された。**fail-open するから**である:
ソケット経路への移行でタブが開かれなくなると `ExtractTabUrls` は空を返し、
空リストに対する述語は false になり、**何も無いという理由でドットが緑になった**。

削除した側のテストは `tests/test_capture_status.py` にあり、あの識別子群が
「存在しないこと」を主張している。だからこのファイルが同じ識別子の「存在」を
主張し続けた結果、2つのテストファイルが正面から矛盾し、CI が落ちた。
ここは現行規則を書く場所であって、消えた規則を悼む場所ではない。

## 現行規則(ui/FleetCockpit.cs の UpdateCaptureDots)

サインインは**捕捉記録**(.fleet/capture_status.json)から、
エージェントは**捕捉が名指しした agent と経路の状態**から決まる。タブは見ない。
"""
from pathlib import Path

from tools import cockpit_source  # reads every part of the split cockpit source

RAW = cockpit_source.read(encoding="utf-8")


def _executable(cs):
    """コメントを落としたソース。

    削除した識別子が「戻っていないこと」を生ソースで見ると、**削除理由を説明している
    コメント**に一致して落ちる。実際に落ちた。同じ形は今日で5回目なので、Python 側の
    tests/_srcprobe.py と同じ考え方をここでも使う: 主張は実行されるコードに対してだけ行う。
    """
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


def test_stale_status_reports_age_and_live_runner_without_guessing_cause():
    header = (Path(__file__).parent / "FleetCockpit.Header.cs").read_text(encoding="utf-8")
    body = header[header.index("void StaleVerdict("):header.index("// Process state unknown")]
    assert 'if (st == 1)' in body
    assert 'status.json の更新が ' in body
    assert ' 秒止まっています（ランナーは稼働中）' in body
    assert 'status.json has not updated for ' in body
    assert 's (runner alive)' in body
    assert 'sweep busy' not in body


def _dots_body():
    body = SOURCE[SOURCE.index("void UpdateCaptureDots("):]
    return body[:body.index('SetDot(4, HealthState.Red, T("hs_agent_bad"), now);')]


# ---- 消えた規則が戻ってきていないこと --------------------------------------------------------

def test_the_tab_sniffing_rule_has_not_come_back():
    """空のタブ一覧に対して false を返し、その false で緑にする規則を復活させないこと。"""
    for gone in ("LooksLikeUsableM365Chat", "hasUsableM365Chat", "ExtractTabUrls",
                 "static bool LooksLikeLoginWall"):
        assert gone not in SOURCE, (
            "%s が戻っている -- タブが開かれない経路では、これは『何も無いから緑』になる" % gone)


# ---- サインインのドット -----------------------------------------------------------------------

def test_signin_distinguishes_a_signin_failure_from_any_other_failure():
    """`kind == "signin"` だけが赤。他の失敗は黄。

    捕捉が失敗した理由を区別せずに赤くすると、サインインの問題でない障害まで
    『サインインし直せ』と言うことになる。
    """
    body = _dots_body()
    assert 'string.Equals(kind, "signin", StringComparison.OrdinalIgnoreCase)' in body
    assert 'SetDot(3, HealthState.Red, T("hs_signin_bad"), now)' in body
    assert 'SetDot(3, HealthState.Yellow, T("hs_signin_failed_other"), now)' in body


def test_an_expired_token_is_not_red_unless_a_run_wants_one():
    """期限切れは自動的に赤ではない。走行中(live)なら黄、走行していないなら灰。

    走行終了後に何時間も赤いドットは、読まれなくなるドットになる。
    """
    body = _dots_body()
    # RED while a run is live (an expired token means the run cannot send); grey otherwise.
    assert 'else if (live)' in body
    assert 'SetDot(3, HealthState.Red, T("hs_signin_stale"), now)' in body
    # The key gained a suffix when hs_signin_gray stopped covering three different
    # situations with one sentence ("No capture on record" was false in two of them).
    # The rule under test is unchanged: expired token, no run, grey rather than red.
    assert 'SetDot(3, HealthState.Gray, T("hs_signin_gray_expired"), now)' in body


# ---- エージェントのドット ---------------------------------------------------------------------

def test_agent_is_gray_when_no_run_is_live():
    body = _dots_body()
    i = body.index("if (!live)")
    assert 'SetDot(4, HealthState.Gray, T("hs_agent_gray"), now)' in body[i:i + 200]


def test_a_closed_route_never_says_workers_are_on_tabs():
    """Copilot never runs in a tab (relay/no_tab.py): a shut route HOLDS goals, it moves
    no worker to a tab. Red only when goals are held (count + the relay's reason from
    status.json token_wait); amber when the route is shut with nothing held."""
    body = _dots_body()
    assert "else if (RouteState() == ROUTE_CLOSED)" in body
    assert "hs_agent_tabs" not in SOURCE
    assert "HeldGoals(" in body
    assert 'T("hs_agent_route_closed_held")' in body
    assert 'T("hs_agent_route_closed_idle")' in body
    blk = body[body.index("else if (RouteState() == ROUTE_CLOSED)"):]
    blk = blk[:blk.index("else if (FleetAgentIsBound())")]
    assert "HealthState.Red" in blk and "HealthState.Yellow" in blk
    assert "HealthState.Green" not in blk
    assert "全ワーカーがタブ" not in SOURCE


def test_token_wait_is_held_goals_not_a_tab_state():
    body = _dots_body()
    assert 'T("hs_agent_token_wait")' in body
    assert "TOKEN_WAIT_RED_S" in body


def test_the_canned_answer_sniff_only_annotates_the_amber():
    """定型の無回答は**色を決めない**。1ターンの質を設備の状態に格上げしない。

    経路閉鎖の分岐では、色は待機中ゴール数(HeldGoals)だけが決める。定型無回答の判定は
    「待機ゼロの黄色」に付ける注記に限られる。
    """
    body = _dots_body()
    blk = body[body.index("else if (RouteState() == ROUTE_CLOSED)"):]
    blk = blk[:blk.index("else if (HeldGoals(out tokReason")]
    assert 'note += T("hs_agent_canned")' in blk, "注記ではなく色を決めている"
    assert blk.count("LooksLikeCannedNonAnswer") == 1
    pre = blk[:blk.index("LooksLikeCannedNonAnswer")]
    assert pre.count("else") >= 1 and "held > 0" in pre, "定型判定が待機ゴールの分岐の外に出ている"
    tail = blk[blk.index("LooksLikeCannedNonAnswer"):]
    assert "HealthState.Red" not in tail and "HealthState.Green" not in tail
    assert tail.count("SetDot(4,") == 1




def test_agent_is_green_only_on_positive_evidence():
    """緑になるのは、フリート自身の紐付けが確認できたときだけ。

    「悪い証拠が無い」は緑の理由にならない。それが消えた規則の失敗そのもの。

    2026-09-16、この検査は緑の分岐が2つあることを要求していた。2つ目は
    capture_status.json の gpt_id への fallback で、FleetAgentIsBound 自身のコメントが
    「あれは ANY surface の最後の捕捉であり、他人についての出来事は自分についての証拠では
    ない」と書いている、まさにそのフィールドである。主判定が避けるために作られたものを
    次の分岐が緑の根拠にしていた。fallback は残す（非空の gpt_id は無ではない）が、
    黄である。この検査は今それを要求する。
    """
    body = _dots_body()
    assert "else if (FleetAgentIsBound())" in body
    assert "else if (!string.IsNullOrEmpty(gptId))" in body
    assert body.count('SetDot(4, HealthState.Green, T("hs_agent_ok"), now)') == 1,         "別サーフェスの証拠が再び緑になっている"
    assert 'SetDot(4, HealthState.Yellow, T("hs_agent_other_surface"), now)' in body


def test_agent_is_red_when_nothing_names_an_agent():
    assert 'SetDot(4, HealthState.Red, T("hs_agent_bad"), now);' in SOURCE
