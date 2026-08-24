"""クライアント側サンプラのテスト。境界の外を測る道具が、自分を測ってはいけない。"""
import types

from scripts.win import watch_client_ws as W


class _P:
    def __init__(self, pid, ppid, cmd, mb, name="python.exe"):
        self.info = {"pid": pid, "ppid": ppid, "name": name,
                     "cmdline": cmd.split(),
                     "memory_info": type("M", (), {"rss": int(mb * 1024 * 1024)})()}


def _psutil(procs):
    return types.SimpleNamespace(process_iter=lambda attrs=None: procs)


def test_it_sums_the_runner_and_everything_under_it():
    """socket の費用はブラウザの外にも落ちる。node の子まで数えないと、
    測りたかった当のものを取り逃す。"""
    ps = _psutil([_P(10, 1, "python run_route_campaign.py", 100.0),
                  _P(11, 10, "node driver.js", 50.0, "node.exe"),
                  _P(12, 11, "node worker.js", 25.0, "node.exe"),
                  _P(99, 1, "unrelated.py", 999.0)])
    total, n, roots = W.sample("run_route_campaign", set(), ps)
    assert total == 175.0 and n == 3 and roots == [10]


def test_it_does_not_match_itself_or_its_parents():
    """コマンドラインの部分一致は自己言及的な述語 -- 探している文字列を自分が持っている。
    同じ形で今夜すでに、停止命令が自分の親シェルを2度殺している。"""
    ps = _psutil([_P(500, 400, "python watch_client_ws.py --pattern run_route_campaign", 80.0),
                  _P(400, 300, "bash -c ... run_route_campaign ...", 20.0),
                  _P(10, 1, "python run_route_campaign.py", 100.0)])
    total, n, roots = W.sample("run_route_campaign", {500, 400, 300}, ps)
    assert roots == [10], "自分か親を対象に含めている"
    assert total == 100.0 and n == 1


def test_no_runner_is_a_gap_not_a_zero():
    """走行と走行の間に走行器は居ない。そこを 0 と書くと、
    『クライアント側は何も使っていない』という嘘の谷ができる。"""
    total, n, roots = W.sample("run_route_campaign", set(), _psutil([_P(1, 0, "idle", 5.0)]))
    assert total is None and n == 0 and roots == []


def test_the_lineage_walk_stops_at_the_top():
    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def ppid(self):
            return 0 if self.pid <= 1 else self.pid - 1

    ps = types.SimpleNamespace(Process=_Proc)
    ids = W._own_lineage(ps)
    assert len(ids) <= 12 and 0 not in ids
