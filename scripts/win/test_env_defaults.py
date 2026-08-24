"""Ensure-EnvDefaults の振る舞いを、実際に .env を書かせて確かめる。

文字列走査ではなく実挙動で見る。この関数の誤りは常に「他人の端末でだけ」現れる種類のもの
なので、ここで実際にファイルを往復させないと意味が無い。
"""
import json
import pathlib
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PS = ROOT / "scripts" / "win" / "env_defaults.ps1"


def run(root):
    """一時ディレクトリの .env に対して関数を1回走らせる。"""
    cmd = ". '%s'; Ensure-EnvDefaults -Root '%s'" % (PS, root)
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return p


@pytest.fixture
def box():
    with tempfile.TemporaryDirectory() as d:
        yield pathlib.Path(d)


def env_of(root):
    out = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_absent_keys_are_added_and_recorded_as_ours(box):
    """空の .env に既定が入り、「自分が書いた」記録が残ること。
    記録が無ければ、次回それを更新してよいのか判断できない。"""
    (box / ".env").write_text("MCP_API_KEY=secret\n", encoding="utf-8")
    run(box)
    env = env_of(box)
    assert env["MCP_API_KEY"] == "secret", "利用者の値を壊している"
    assert "TASK_JOB_APPROVAL_MODE" in env, "既定が入っていない"
    rec = json.loads((box / ".env.defaults.json").read_text(encoding="utf-8"))
    assert rec["TASK_JOB_APPROVAL_MODE"] == env["TASK_JOB_APPROVAL_MODE"]


def test_a_value_the_user_changed_is_never_touched(box):
    """利用者が変えた値には二度と触らない。ここを誤ると、意図した設定が黙って消える。"""
    (box / ".env").write_text("MCP_LOCAL_EDGE_MB_LIMIT=9999\n", encoding="utf-8")
    run(box)
    run(box)
    assert env_of(box)["MCP_LOCAL_EDGE_MB_LIMIT"] == "9999"
    rec = json.loads((box / ".env.defaults.json").read_text(encoding="utf-8"))
    assert rec.get("MCP_LOCAL_EDGE_MB_LIMIT") != "9999", "利用者の値を自分のものとして記録した"


def test_running_twice_changes_nothing(box):
    """毎起動で走る。2回目が何かを動かすなら、毎朝設定が揺れることになる。"""
    (box / ".env").write_text("MCP_API_KEY=k\n", encoding="utf-8")
    run(box)
    first = (box / ".env").read_text(encoding="utf-8")
    run(box)
    assert (box / ".env").read_text(encoding="utf-8") == first


def test_no_env_file_means_nothing_is_created(box):
    """.env は quickstart が作る。まだ無いなら、この関数は何もしないのが正しい --
    鍵の入っていない .env を先に作ると、quickstart 側の生成と衝突する。"""
    run(box)
    assert not (box / ".env").exists()


def test_the_websocket_key_is_not_written(box):
    """MCP_FLEET_SOCKET は未設定が ON。ここで "1" を書くと、全端末が今日の答えに固定され、
    コード側の既定を変える余地が消える。空文字は OFF を意味するという非対称もある。"""
    (box / ".env").write_text("MCP_API_KEY=k\n", encoding="utf-8")
    run(box)
    assert "MCP_FLEET_SOCKET" not in env_of(box)


def shipped_default(key):
    """このリリースが出荷している既定値。テストが番号を丸暗記しないため実物から読む。"""
    import re
    src = PS.read_text(encoding="utf-8")
    m = re.search(r'^\s*%s\s*=\s*"([^"]*)"' % re.escape(key), src, re.M)
    assert m, "%s の既定が見つからない" % key
    return m.group(1)


def write_env(root, key, value):
    (root / ".env").write_text(key + "=" + value + "\n", encoding="utf-8")


def test_a_default_this_tool_wrote_is_upgraded_when_the_shipped_default_changes(box):
    """この変更が存在する理由そのもの。

    古いリリースが既定として 0 を書くと、キーが存在するので以降のリリースは全て素通りし、
    その端末は永久に古い値のまま残った -- 「他の人の端末では start_all で env の一部
    書き換えがなされていない」として報告された通りの症状。

    ここでは「前回この道具が別の値を書いた」状態を記録側に作り、現在の出荷既定へ
    更新されることを確かめる。利用者が触っていない値だから更新してよい。"""
    key = "MCP_LOCAL_ROTATE_AFTER_TURNS"
    want = shipped_default(key)
    stale = "99"
    assert stale != want
    write_env(box, key, stale)
    (box / ".env.defaults.json").write_text(json.dumps({key: stale}), encoding="utf-8")

    run(box)

    assert env_of(box)[key] == want, "自分が書いた古い既定を更新できていない"
    rec = json.loads((box / ".env.defaults.json").read_text(encoding="utf-8"))
    assert rec[key] == want, "更新した値を記録し直していない"


def test_a_user_value_that_happens_to_match_our_default_is_claimed(box):
    """利用者が偶然こちらの既定と同じ値を手で書いた場合、それはこちらのものとして記録する。
    記録しないでおくと、次に既定が変わった時にその端末だけ取り残される側に戻る。"""
    key = "MCP_LOCAL_REVIEW_MAX_CONCURRENT"
    write_env(box, key, shipped_default(key))
    run(box)
    rec = json.loads((box / ".env.defaults.json").read_text(encoding="utf-8"))
    assert rec[key] == shipped_default(key)
