""".env が「読まれている」のと「効いている」のは別のこと。

relay/fleet_runner.py は load_dotenv() を relay_fleet の import より**後**で呼んでいた。
モジュール直下の `X = os.environ.get("MCP_...")` は import の瞬間に評価されるので、
そこに書かれた設定は .env にあっても既定値のまま走る。呼び出し時に読む設定
(MCP_SOCKET_FORCE_FAIL など)は同じ .env から同じ走行で効くため、
**効く設定と黙って無視される設定が .env の見た目では区別できなかった**。

2026-08-28 実測: .env に MCP_FLEET_SOCKET_RETRIES=0 を置いた状態で、この経路から
import した DEFAULT_SOCKET_RETRIES は 2 を返した。

だからこのテストは「順序が正しいか」をソースで見る。実際の定数で見ないのは、
今の .env にたまたま該当キーが無ければ定数は既定値と一致してしまい、
**壊れていても緑になる**から -- 母集団が測りたい事象を含まない、いつもの形。
"""
import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _runner_source():
    with io.open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8") as fh:
        return fh.read()


def test_dotenv_is_loaded_before_the_modules_that_read_it_at_import():
    src = _runner_source()
    load_at = src.index("load_dotenv()")
    import_at = src.index("from relay.relay_fleet import")
    assert load_at < import_at, (
        "load_dotenv() が relay_fleet の import より後にある。"
        "モジュール直下の os.environ.get はこの import で評価済みになるので、"
        "そこに置かれた .env の設定は黙って既定値で走る"
    )


def test_the_setting_that_exposed_it_now_reaches_the_constant(tmp_path, monkeypatch):
    """実際に .env から値が届くこと -- 順序だけでなく結果を見る。

    サブプロセスで確かめる。この試験プロセスは既に relay_fleet を import 済みで、
    モジュール定数は再評価されないため、同じプロセス内では**何を書いても通ってしまう**。
    """
    import subprocess
    import sys

    env_file = tmp_path / ".env"
    env_file.write_text("MCP_FLEET_SOCKET_RETRIES=7\n", encoding="utf-8")
    code = (
        "import sys, os;"
        "sys.path.insert(0, %r);"
        "from dotenv import load_dotenv; load_dotenv(%r);"
        "import relay.relay_fleet as F; print(F.DEFAULT_SOCKET_RETRIES)"
        % (REPO, str(env_file))
    )
    env = dict(os.environ)
    env.pop("MCP_FLEET_SOCKET_RETRIES", None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=REPO, env=env, timeout=180)
    assert out.returncode == 0, out.stderr[-800:]
    assert out.stdout.strip().splitlines()[-1] == "7", (
        ".env の値が定数に届いていない: %r" % out.stdout[-200:])
