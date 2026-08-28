"""閉じたポートに何秒も待たないこと。

## 何を直したか

`checkpoint.targets(port)` は `urlopen` でCDPを叩く。**何もbindされていないポート**に対して
これはタイムアウトを待ち切る — 実測 2.03s(9224 = eval Edge、通常は起動していない)。
起動ゲートはポートごとに1回これを呼ぶので、**毎回の起動でこの待ちを払っていた**。
ゴール投入から最初のターンが出るまでの遅延の、単一で最大の項目だった。

## 最初に書いた説明は間違っていた

「閉じたループバックポートは即座に refuse される」と書いた。**実測ではそうならない**:
connect_ex は 2.02s 後に WSAECONNREFUSED を返し、タイムアウトを短くすると
ちょうどその時刻に WSAEWOULDBLOCK を返す。つまり短いプローブは「速いNO」を
得ているのではなく、**早めに諦めてそれをNOと呼んでいる**。別のことなので、そう書いた。

早めに諦めてよい理由は非対称性にある: 生きているCDPポートは約1msで accept する(実測)。
0.5秒はその500倍。そして間違えたときの代償は**新しくない** — 呼び出し元は None を返し、
それは以前 urlopen 自身がタイムアウトしたときに返していたものと同じ。同じ答えに早く着く。
"""
import os
import socket
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _checkpoint():
    import importlib.util

    path = os.path.join(REPO, "scripts", "win", "checkpoint.py")
    spec = importlib.util.spec_from_file_location("checkpoint_probe_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port():
    """A port with nothing bound: bind, read the number, close."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_a_bound_port_reads_as_listening():
    """生きているポートを死んでいると誤判定しないこと。ここが緩むと**逆の**害が出る。"""
    m = _checkpoint()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        assert m._listening(srv.getsockname()[1]) is True
    finally:
        srv.close()


def test_an_unbound_port_reads_as_not_listening_and_does_so_quickly():
    m = _checkpoint()
    port = _free_port()
    t0 = time.time()
    assert m._listening(port) is False
    took = time.time() - t0
    assert took < 1.5, (
        "閉じたポートの判定に %.2fs かかった -- 短縮した意味が無い" % took)


def test_targets_returns_none_for_a_dead_port_without_waiting_out_the_http_timeout():
    """呼び出し元から見た**値**は変わらないこと。速くなるだけ。"""
    m = _checkpoint()
    port = _free_port()
    t0 = time.time()
    got = m.targets(port)
    took = time.time() - t0
    assert got is None, "答えが変わっている -- 呼び出し元は None を『応答なし』と読む"
    assert took < 1.5, "urlopen のタイムアウトを待ち切っている(%.2fs)" % took


def test_an_unanswerable_probe_does_not_declare_the_port_absent():
    """プローブ自体が失敗したときは、ポートが無いとは言わないこと。

    「聞けなかった」を「聞いたら居なかった」にすると、生きているポートを黙って
    切り捨てる。今日だけで何度も踏んだ形なので、ここでも分けておく。
    """
    m = _checkpoint()
    real = socket.socket

    class _Broken(object):
        def __init__(self, *a, **k):
            raise OSError("no sockets today")

    socket.socket = _Broken
    try:
        assert m._listening(9999) is True, (
            "プローブが動かなかったのにポート不在と判定した")
    finally:
        socket.socket = real


# 「プローブが HTTP より先に走ること」をソースの並び順で見るテストは書かない。
# 一度書いて落ちた: `src.index("urlopen")` が、直前のコメント中の "urlopen" に当たった。
# コメント本文に一致して落ちる形は今日6回目で、そのたびに直しているのが答えになっている。
#
# そして順序そのものは、上の
# test_targets_returns_none_for_a_dead_port_without_waiting_out_the_http_timeout
# が**挙動**で押さえている: HTTP を先に呼べば 2 秒かかり、そのテストが落ちる。
# 並びを読むより、時間を測るほうが強い。


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
