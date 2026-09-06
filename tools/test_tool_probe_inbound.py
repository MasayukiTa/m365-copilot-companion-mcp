"""プローブの tool 呼び出しがこのサーバに着弾したかを、経路に依存せず記録する。

このプローブが生まれた障害は「コネクタの同意が失効し、呼び出しが Copilot の
web UI の中で死ぬ」もので、こちらのプロセスには何も届かないので、どの計数器も
動かず、どのドットも緑のままだった。逆に成功はこちら側で完全に見える -- プローブは
このサーバしか名前を知らないディレクトリを1つ列挙させる。だから着弾が信号で、
プローブ窓の中で着弾が無いことが警報になる。ページでも socket でも同じに読める。
"""
import ast
import inspect
import json

import pytest

from tools import tool_probe as TP


@pytest.fixture
def inbound(tmp_path, monkeypatch):
    p = tmp_path / "probe_inbound.json"
    monkeypatch.setattr(TP, "_INBOUND_PATH", p)
    return p


def test_an_unrelated_tool_call_writes_nothing(inbound):
    """ゲートウェイの全呼び出しが通る場所なので、関係ない呼び出しで書いてはいけない。"""
    assert TP.note_inbound("read_file", {"path": "C:/tmp/whatever.txt"}) is False
    assert not inbound.exists()


def test_the_probes_own_call_is_stamped(inbound):
    assert TP.note_inbound("list_directory", {"path": str(TP._CHALLENGE_DIR)}) is True
    rec = json.loads(inbound.read_text(encoding="utf-8"))
    assert rec["tool"] == "list_directory" and rec["ts"] > 0


def test_never_seen_reads_as_zero_not_none(inbound):
    """None を返すと、窓の開始時刻との比較で『いま着弾した』に化ける経路ができる。"""
    assert TP.last_inbound_ts() == 0.0


def test_it_cannot_fail_a_tool_call(inbound, monkeypatch, tmp_path):
    """ホットパス上にある。記録の失敗で tool 呼び出しを落としてはいけない。"""
    # A DRIVE LETTER IS NOT UNWRITABLE EVERYWHERE. "Z:/nonexistent/x.json" is a nonexistent
    # drive on Windows and a perfectly ordinary relative directory named "Z:" on Linux, so the
    # write succeeded there and this returned True. The failure was invisible for as long as
    # the manifest audit stopped the job before pytest ran.
    #
    # A file used as a directory fails on both: ENOTDIR on Linux, and the same refusal from
    # Windows.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(TP, "_INBOUND_PATH", TP.Path(str(blocker / "x.json")))
    assert TP.note_inbound("list_directory", {"path": str(TP._CHALLENGE_DIR)}) is False
    assert TP.last_inbound_ts() == 0.0


def test_the_gateway_stamps_before_it_dispatches():
    """呼んでいなければ何も記録されない。ディスパッチの前であること。"""
    import io
    src = io.open("main.py", encoding="utf-8").read()
    i = src.index("_probe.note_inbound(name, _args)")
    j = src.index("_out = fn(**_args)")
    assert i < j, "ディスパッチの後に置かれている"


def test_the_probe_compares_the_window_and_records_the_bit():
    """窓を開く前の刻印と比べなければ、前回の残りを今回の着弾と読む。"""
    import io
    src = io.open("bridge/copilot_bridge.py", encoding="utf-8").read()
    assert "_inbound_before = tool_probe.last_inbound_ts()" in src
    assert "tool_probe.last_inbound_ts() > _inbound_before" in src
    assert "inbound=_inbound" in src


def test_the_summary_carries_it_separately_from_alive():
    """text が返ることと、呼び出しが届いたことは別。片方だけでは
    コネクタ経路の障害と応答の障害を切り分けられない。"""
    fn = ast.parse(inspect.getsource(TP.record_probe).lstrip()).body[0]
    names = [a.arg for a in fn.args.args]
    assert "inbound" in names and "alive" in names
    src = inspect.getsource(TP.get_summary)
    assert '"tool_inbound"' in src and '"tool_alive"' in src


# ---------------------------------------------------------------------------------------
# THE ARRIVAL IS NOW THE VERDICT, not just a field reported beside it.
#
# It used to be that `ok` came from scanning the reply for a token the agent had to discover
# on disk, and the arrival was recorded alongside as extra colour. That probe was refused by
# the live agent on security grounds -- correctly; see tool_probe._CHALLENGE_INSTRUCTION_TAIL
# -- so the token changed direction. It now goes OUT in the path and comes back IN through the
# tool channel, and these tests pin the two halves of the check that replaced the scan.
# ---------------------------------------------------------------------------------------

def test_the_arrival_carries_the_token_it_was_called_with(inbound):
    assert TP.note_inbound(
        "list_directory", {"path": "C:/x/.fleet/probe_challenge/probe_0123456789ab"}) is True
    assert json.loads(inbound.read_text(encoding="utf-8"))["token"] == "0123456789ab"


def test_naming_only_the_parent_directory_proves_nothing(inbound):
    """The limit note_inbound's docstring used to record as accepted, now closed. A call that
    merely mentions the challenge directory still stamps -- it is cheap and harmless to record
    -- but it carries no token, so it can no longer satisfy a probe."""
    assert TP.note_inbound("list_directory", {"path": "C:/x/.fleet/probe_challenge"}) is True
    assert json.loads(inbound.read_text(encoding="utf-8"))["token"] == ""
    assert TP.probe_arrived("0123456789ab", 0.0) is False


def test_the_previous_probes_arrival_does_not_satisfy_this_one(inbound):
    """The failure the window alone could not catch: two probes inside one window, or a clock
    that did not move. The token settles it without reference to time at all."""
    TP.note_inbound("list_directory",
                    {"path": "C:/x/.fleet/probe_challenge/probe_aaaaaaaaaaaa"}, ts=1000.0)
    assert TP.probe_arrived("aaaaaaaaaaaa", 0.0) is True     # its own probe
    assert TP.probe_arrived("bbbbbbbbbbbb", 0.0) is False    # the next one


def test_an_arrival_from_before_the_window_is_rejected(inbound):
    TP.note_inbound("list_directory",
                    {"path": "C:/x/.fleet/probe_challenge/probe_aaaaaaaaaaaa"}, ts=1000.0)
    assert TP.probe_arrived("aaaaaaaaaaaa", 999.0) is True
    assert TP.probe_arrived("aaaaaaaaaaaa", 1001.0) is False


def test_no_arrival_at_all_is_not_a_pass(inbound):
    assert not inbound.exists()
    assert TP.probe_arrived("aaaaaaaaaaaa", 0.0) is False


def test_the_fallback_challenge_can_never_pass(inbound):
    """new_probe_challenge returns FALLBACK_CHALLENGE_TOKEN when it cannot prepare the
    directory. Whatever is stamped, that probe must resolve to a failure, never a pass."""
    TP.note_inbound("list_directory",
                    {"path": "C:/x/.fleet/probe_challenge/probe_aaaaaaaaaaaa"}, ts=1000.0)
    assert TP.probe_arrived(TP.FALLBACK_CHALLENGE_TOKEN, 0.0) is False
    assert TP.probe_arrived("", 0.0) is False


def test_a_corrupt_stamp_reads_as_no_arrival(inbound, monkeypatch):
    inbound.write_text("{not json", encoding="utf-8")
    assert TP.last_inbound() == {}
    assert TP.probe_arrived("aaaaaaaaaaaa", 0.0) is False


def test_verify_probe_arrival_keeps_the_kind_vocabulary_and_precedence():
    """Same five branches, same order, same names as verify_probe_reply -- readers, the health
    strip and probe_kind_is_alive all key on these strings."""
    assert TP.verify_probe_arrival("何でも", agent_loaded=False, arrived=True) == (
        False, "agent_unreachable")
    assert TP.verify_probe_arrival(
        "接続マネージャーを開く", agent_loaded=True, arrived=True) == (False, "consent_card")
    assert TP.verify_probe_arrival(
        "実行不可", agent_loaded=True, arrived=True) == (False, "canned_fallback")
    assert TP.verify_probe_arrival("呼び出せました", agent_loaded=True, arrived=True) == (
        True, "answer")
    assert TP.verify_probe_arrival("呼び出せました", agent_loaded=True, arrived=False) == (
        False, "error")


def test_a_confident_reply_without_an_arrival_is_still_a_failure():
    """THE point of moving the proof to the server. An agent that says it called the tool, in
    the words a green probe would once have accepted, does not make it true."""
    ok, kind = TP.verify_probe_arrival(
        "list_directory を呼び出し、正常に一覧できました。", agent_loaded=True, arrived=False)
    assert (ok, kind) == (False, "error")


def test_verify_probe_arrival_reads_nothing_from_disk():
    """It stays a pure function, like classify_probe_reply: the I/O lives in probe_arrived so
    the classification can be tested with canned values."""
    src = inspect.getsource(TP.verify_probe_arrival)
    tree = ast.parse(src.lstrip())
    calls = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "open" not in calls
    assert "last_inbound" not in calls


def _attr_calls(module_path):
    """Every `x.y(...)` callee name actually CALLED in a module, via the AST.

    Deliberately not a substring search over the text: the change these tests guard left
    explanatory comments naming the retired function, so `"verify_probe_reply" not in src`
    would fail on prose while a real call sat one line below. Parse, don't grep.
    """
    import io
    tree = ast.parse(io.open(module_path, encoding="utf-8").read())
    return {n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


@pytest.mark.parametrize("module_path", ["bridge/copilot_bridge.py", "relay/edge_reconnect.py"])
def test_the_probe_verdict_comes_from_the_arrival_not_the_reply(module_path):
    """Both places that run a challenge probe must decide `ok` from the arrival. Leaving one
    on verify_probe_reply would leave one of them asking the agent to transcribe a secret --
    and that is the ask the live agent refused."""
    calls = _attr_calls(module_path)
    assert "verify_probe_arrival" in calls
    assert "probe_arrived" in calls
    assert "verify_probe_reply" not in calls
