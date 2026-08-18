"""The fleet adapter's refusals and its attestation, without a live fleet.

What is testable here is exactly what the module claims: that a comparison which could not be
attributed to the candidate is REFUSED, and that the child really loads the manifest it was
sent. Driving a browser is not tested and the module says so.

The attestation tests spawn a real child process -- that is the whole point of them. If the
manifest did not survive into the child, they fail, which is the property the five review
rounds were unable to establish any other way.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from bench.companionbench.fleet_agent import (RESULT_PREFIX, FleetAgent,
                                              FleetContractError)
from relay.selfimprove import manifest as M
from relay.selfimprove import runtime_config as RC


def _agent(**kw):
    kw.setdefault("agent_url", "http://127.0.0.1:0/unused")
    return FleetAgent(**kw)


def _seed():
    d = tempfile.mkdtemp(prefix="seed_")
    os.makedirs(os.path.join(d, "memory"), exist_ok=True)
    with open(os.path.join(d, "memory", "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("- [テーマ](theme.md)\n")
    return d


def _manifest_file(manifest):
    path = os.path.join(tempfile.mkdtemp(prefix="mf_"), "m.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh)
    return path


# ---- the refusals -------------------------------------------------------------------------

def test_a_field_the_fleet_does_not_read_is_refused():
    base = M.base_manifest()
    # every declared parameter IS read by the fleet, so this uses a component the target
    # cannot reach by pretending the covered set is narrower
    a = _agent(memory_seed=_seed())
    a.covered_fields = frozenset({"parameters.max_retries"})
    cand = M.apply_genome(base, {"parameters": {"memory_max_items": 9}})
    with pytest.raises(FleetContractError) as exc:
        a.check_genome(base, cand)
    assert "same program twice" in str(exc.value)


def test_an_inert_refuter_budget_is_refused():
    """refuter が無効なら max_refute_passes は読まれない。
    読まれないフィールドの A/B は、同じプログラムを2回走らせて p 値を出すこと。"""
    base = M.base_manifest()
    cand = M.apply_genome(base, {"parameters": {"max_refute_passes": 5}})
    with pytest.raises(FleetContractError) as exc:
        _agent(refuter=False, memory_seed=_seed()).check_genome(base, cand)
    assert "inert" in str(exc.value)

    # with the refuter on it is a real comparison
    _agent(refuter=True, memory_seed=_seed()).check_genome(base, cand)


def test_a_memory_experiment_without_a_seed_is_refused():
    """空の記憶に対する記憶実験は何も動かさない。共有の記憶に対する実験は、
    片方の腕がもう片方の入力を書く。"""
    base = M.base_manifest()
    cand = M.apply_genome(base, {"parameters": {"memory_max_items": 9}})
    with pytest.raises(FleetContractError) as exc:
        _agent().check_genome(base, cand)
    assert "memory_seed" in str(exc.value)


def test_each_arm_gets_its_own_memory_directory():
    """フリートの記憶は走行ごとに読み書きされるので、共有すると対戦が系列になる。"""
    a = _agent(memory_seed=_seed())
    first, _ = a._arm_state_dir()
    second, _ = a._arm_state_dir()
    assert first != second
    assert os.path.isfile(os.path.join(first, ".fleet", "memory", "INDEX.md"))
    assert os.path.isfile(os.path.join(second, ".fleet", "memory", "INDEX.md"))


def test_the_child_is_not_given_the_parameters_under_test():
    """run_relay_fleet は明示引数を manifest より優先する -- 意図的に。
    だからここで渡すと、試験対象のフィールドを自分で黙らせることになる。"""
    from bench.companionbench.fleet_agent import _child_source
    src = _child_source()
    assert "max_transient" not in src.split("run_relay_fleet")[1].split(")")[0]
    assert "max_refute=" not in src


# ---- the attestation: a real child, carrying a real manifest -------------------------------

def test_the_child_loads_the_manifest_it_was_sent(monkeypatch):
    """5ラウンドかけても他の方法では確立できなかった性質: manifest が実際に
    実行側へ届いていること。子プロセスに聞き、返答を検証する。"""
    cand = M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 11}})
    monkeypatch.setenv(RC.OVERRIDE_ENV, _manifest_file(cand))
    got = _agent(memory_seed=_seed()).attest(cand)
    assert got["harness_id"] == M.harness_id(cand), "子プロセスに manifest が届いていない"
    assert got["execution_target"] == "relay_fleet/v1"
    assert got["effective"]["memory_max_items"] == 11


def test_a_different_manifest_produces_a_different_attestation(monkeypatch):
    """同じ値を返し続けるだけの attest は検査になっていない。"""
    a = M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 2}})
    b = M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 12}})
    agent = _agent(memory_seed=_seed())

    monkeypatch.setenv(RC.OVERRIDE_ENV, _manifest_file(a))
    got_a = agent.attest(a)
    monkeypatch.setenv(RC.OVERRIDE_ENV, _manifest_file(b))
    got_b = agent.attest(b)

    assert got_a["harness_id"] != got_b["harness_id"]
    assert got_a["effective"]["memory_max_items"] == 2
    assert got_b["effective"]["memory_max_items"] == 12


def test_the_adapter_satisfies_the_runner_contract():
    from bench.companionbench import agents as A
    assert FleetAgent.execution_target == A.FLEET
    assert "parameters.max_retries" in FleetAgent.covered_fields
    assert hasattr(FleetAgent, "attest")


# ---- round 7: the adapter could not have run at all ---------------------------------------

def test_the_child_is_not_handed_a_browser_context():
    """Playwright の context は json.dumps を通らない。渡そうとしていた時点で、
    このアダプタは『未検証』ではなく『動作不能』だった。"""
    from bench.companionbench.fleet_agent import _child_source
    src = _child_source()
    assert "connect_over_cdp" in src, "子が自分でブラウザに接続していない"
    assert 'payload.get("context")' not in src


def test_the_child_reads_the_key_the_fleet_actually_writes():
    """`response` は当て推量で、実際は last_response。成功した走行まで空返答になっていた。"""
    from bench.companionbench.fleet_agent import _child_source
    assert "last_response" in _child_source()


def test_a_child_error_is_raised_rather_than_returned_as_an_empty_reply():
    """ブラウザが起動しない事象が『候補が失敗したタスク』として採点されていた。"""
    a = _agent(memory_seed=_seed())
    a._run_child = lambda mode, payload, workdir=None: {
        "attest": {"harness_id": "x"}, "reply": "", "error": "BrowserType.launch failed"}
    with pytest.raises(FleetContractError) as exc:
        a("prompt", tempfile.mkdtemp())
    assert "fleet child failed" in str(exc.value)


def test_the_seeded_memory_directory_is_actually_used(monkeypatch):
    """seed を作って使っていなかった -- 子は workdir で走り、project_memory は
    カレント基準で .fleet を解決するので、両腕が同じ store を共有していた。"""
    from relay import project_memory as PM

    captured = {}

    def fake_run(args, **kw):
        captured["cwd"] = kw.get("cwd")
        captured["state"] = (kw.get("env") or {}).get(PM.STATE_DIR_ENV)
        # OBSERVED WHILE THE CHILD IS RUNNING, because the directory is removed as soon as it
        # returns -- one was leaked per child and 520 had accumulated on one machine. The
        # property under test is unchanged; only the moment it can be seen has moved, and
        # inside the call is the more honest place anyway: that is when the child sees it.
        captured["seed_present"] = os.path.isfile(
            os.path.join(captured["state"] or "", "memory", "INDEX.md"))

        class _P:
            returncode = 0
            stdout = RESULT_PREFIX + json.dumps({"attest": {"harness_id": "x"}})
            stderr = ""
        return _P()

    import bench.companionbench.fleet_agent as FA
    monkeypatch.setattr(FA.subprocess, "run", fake_run)
    a = _agent(memory_seed=_seed())
    a._run_child("attest", None, workdir=tempfile.mkdtemp(prefix="wd_"))
    assert captured["state"], "FLEET_STATE_DIR が渡されていない"
    assert captured["seed_present"], "seed が子に見えていない"
    assert captured["cwd"] != "" and captured["state"].startswith(captured["cwd"])
    assert not os.path.isdir(captured["cwd"]), "アーム状態ディレクトリが残っている"


def test_project_memory_honours_an_explicit_state_root(monkeypatch):
    """既定値が真の文字列だったので、env は一度も参照されていなかった。"""
    from relay import project_memory as PM

    root = tempfile.mkdtemp(prefix="stateroot_")
    monkeypatch.setenv(PM.STATE_DIR_ENV, root)
    PM.record_task("テーマ", "作業", "DONE", note="x", authority="MACHINE_VERIFIER")
    assert os.path.isdir(os.path.join(root, "memory"))


def test_a_turn_that_never_started_is_refused_rather_than_scored():
    """実走行で判明: フリート Edge がサインイン画面だと composer が無く、
    outcome STUCK / turns 0 で空返答が返る。それを『候補が失敗したタスク』と
    採点したら、使えない環境が『劣ったハーネス』に見える。"""
    a = _agent(memory_seed=_seed())
    a._run_child = lambda mode, payload, workdir=None: {
        "attest": {"harness_id": ""}, "reply": "", "error": "",
        "worker": {"outcome": "STUCK", "turns": 0,
                   "reason": "conversation tab/composer is closed (dead target)"},
    }
    with pytest.raises(FleetContractError) as exc:
        a("prompt", tempfile.mkdtemp())
    assert "completed no turns" in str(exc.value)
    assert "dead target" in str(exc.value)


def test_a_real_answer_is_returned_normally():
    """締めた結果、成功経路まで拒否していないこと。"""
    a = _agent(memory_seed=_seed())
    a._run_child = lambda mode, payload, workdir=None: {
        "attest": {"harness_id": ""}, "reply": "84", "error": "",
        "worker": {"outcome": "DONE", "turns": 3, "reason": ""},
    }
    assert a("prompt", tempfile.mkdtemp()) == "84"


def test_agent_url_must_be_a_url_the_tab_can_open():
    """agent_url はワーカーのタブが開くチャットURL。API エンドポイントを渡すと
    composer の無いページが開き、90秒後に dead target になる -- 実際にそうなった。"""
    with pytest.raises(FleetContractError):
        FleetAgent(agent_url="127.0.0.1:8765")
    FleetAgent(agent_url="https://example.invalid/chat")     # 受理される形


def test_the_fleet_prompt_names_the_workdir():
    """エピソードのプロンプトはファイル名しか言わない -- 「mod_b.py の TIMEOUT を変更」。
    それが置かれた一時ディレクトリは agent が知らない場所で、BridgeAgent は前置していた。
    このアダプタは workdir を受け取り _run_child に渡し、_run_child はそれを使っていなかった。
    子は自分の state dir で走るので、ファイル系エピソードは全て別の作業場所を測っていた。"""
    sent = {}

    class _Probe(FleetAgent):
        def _run_child(self, mode, payload, workdir=None):
            sent.update(payload)
            return {"reply": "done, mod_b.py updated", "worker": {"turns": 1},
                    "attest": {"harness_id": ""}}

    agent = _Probe(agent_url="https://m365.cloud.microsoft/chat/")
    agent("mod_b.py の TIMEOUT を 30 から 90 に変更してください。", "C:/tmp/cb_episode_42")

    goal = sent["goal"]
    assert isinstance(goal, dict), "goal が dict でないと run_relay_fleet は cwd を読まない"
    assert "C:/tmp/cb_episode_42" in goal["text"], "プロンプトが作業場所を伝えていない"
    assert goal["cwd"] == "C:/tmp/cb_episode_42", "fleet に作業ディレクトリが渡っていない"


def test_the_goal_shape_is_the_one_the_fleet_reads():
    """relay_fleet は dict の goal のときだけ cwd を読む。文字列で渡すと黙って無視される。"""
    from relay.relay_fleet import goal_fields

    text, _checks, cwd = goal_fields({"text": "do it", "cwd": "C:/wd"})
    assert text == "do it" and cwd == "C:/wd"
    _text, _c, cwd_none = goal_fields("do it")
    assert cwd_none is None, "文字列 goal でも cwd が付くなら、この試験の前提が変わっている"


# ---- concurrency comes from the fleet's own RAM policy, not from a constant ---------------

def _plain_agent():
    return FleetAgent(agent_url="https://example.invalid/chat/")


def test_a_chosen_maxtabs_is_honoured_exactly_even_on_a_small_machine(monkeypatch):
    """autoscale を切るのは『RAM適応は要らない』という運用者の明示的判断。

    512GB機で maxtabs=64 と書いた人に対し、こちらが黙って RAM で切り詰めるのは
    制御を使うのではなく奪うこと。人が選んだ値はそのまま通す。"""
    import bench.companionbench.fleet_agent as FA
    import relay.fleet_runner as fr
    import relay.relay_fleet as rf
    monkeypatch.setattr(fr, "settings_maxtabs", lambda default=3: 64)
    monkeypatch.setattr(fr, "settings_autoscale", lambda: (False, 0))
    monkeypatch.setattr(FA, "_maxtabs_was_chosen", lambda: True)
    monkeypatch.setattr(rf, "auto_concurrency", lambda n: 1)   # a cramped box
    assert _plain_agent().max_concurrent_episodes == 64, "選ばれた設定値を切り詰めた"


def test_autoscale_sizes_against_free_ram_up_to_the_configured_ceiling(monkeypatch):
    """autoscale が有効なら空きRAMで決まり、上限は運用者の設定した天井。"""
    import relay.fleet_runner as fr
    import relay.relay_fleet as rf
    monkeypatch.setattr(fr, "settings_maxtabs", lambda default=3: 8)
    monkeypatch.setattr(fr, "settings_autoscale", lambda: (True, 32))
    monkeypatch.setattr(rf, "auto_concurrency", lambda n: 5)
    assert _plain_agent().max_concurrent_episodes == 5, "RAMの答えを使っていない"
    monkeypatch.setattr(rf, "auto_concurrency", lambda n: 999)
    assert _plain_agent().max_concurrent_episodes == 32, "天井を超えた"


def test_an_explicit_value_is_a_hard_cap_like_max_concurrent_on_the_cli(monkeypatch):
    import relay.fleet_runner as fr
    monkeypatch.setattr(fr, "settings_maxtabs", lambda default=3: 64)
    monkeypatch.setattr(fr, "settings_autoscale", lambda: (True, 64))
    agent = FleetAgent(agent_url="https://example.invalid/chat/",
                       max_concurrent_episodes=2)
    assert agent.max_concurrent_episodes == 2


def test_an_unreadable_settings_file_does_not_yield_the_default_three(monkeypatch, tmp_path):
    """『読めなければ直列』と書いていたが、コードはそう動いていなかった。

    `_settings_int` は読み取り・パースの失敗を内部で握りつぶして既定値を返すので、
    settings.txt が無い/壊れていても `settings_maxtabs()` は 3 を返し、例外は飛ばない。
    それを『例外を投げさせる』テストで通していたので、実挙動を一度も検証していなかった。
    3 は、この property を書かせた事故（58〜116秒→14〜16分）を起こした当の数字。"""
    import relay.fleet_runner as fr
    import relay.relay_fleet as rf
    monkeypatch.setattr(fr, "_settings_path", lambda: str(tmp_path / "nope.txt"))
    assert fr.settings_maxtabs() == 3, "前提が変わったらこのテストの意味も変わる"
    monkeypatch.setattr(fr, "settings_autoscale", lambda: (False, 0))
    monkeypatch.setattr(rf, "auto_concurrency", lambda n: 1)   # 2GB free box
    assert _plain_agent().max_concurrent_episodes == 1, "余裕の無い機械で3を返した"


def test_an_unchosen_default_defers_to_what_the_machine_can_afford(monkeypatch):
    """誰も選んでいない 3 は、このモジュールの当て推量であって運用者の判断ではない。

    そして 2GB しか空きの無い機械での 3 は、まさにこの property を書かせた事故
    （58〜116秒のエピソードが14〜16分になった）を起こした当の数字。"""
    import bench.companionbench.fleet_agent as FA
    import relay.fleet_runner as fr
    import relay.relay_fleet as rf
    monkeypatch.setattr(fr, "settings_maxtabs", lambda default=3: 3)
    monkeypatch.setattr(fr, "settings_autoscale", lambda: (False, 0))
    monkeypatch.setattr(FA, "_maxtabs_was_chosen", lambda: False)
    monkeypatch.setattr(rf, "auto_concurrency", lambda n: 1)
    assert _plain_agent().max_concurrent_episodes == 1
    monkeypatch.setattr(rf, "auto_concurrency", lambda n: 64)
    assert _plain_agent().max_concurrent_episodes == 3, "既定値の上へ増やした"


def test_whether_maxtabs_was_chosen_is_read_from_the_file_not_from_the_default(tmp_path,
                                                                              monkeypatch):
    """`settings_maxtabs()` は書かれた 3 と、無いときの 3 を区別できない。"""
    import bench.companionbench.fleet_agent as FA
    import relay.fleet_runner as fr
    p = tmp_path / "settings.txt"
    monkeypatch.setattr(fr, "_settings_path", lambda: str(p))
    assert FA._maxtabs_was_chosen() is False, "存在しないファイルを『選ばれた』と読んだ"
    p.write_text("effort=auto\n", encoding="utf-8")
    assert FA._maxtabs_was_chosen() is False, "他のキーだけで『選ばれた』と読んだ"
    p.write_text("effort=auto\nmaxtabs=12\n", encoding="utf-8")
    assert FA._maxtabs_was_chosen() is True


def test_a_genuine_exception_still_falls_back_to_serial(monkeypatch):
    import relay.fleet_runner as fr

    def _boom(*a, **k):
        raise OSError("import or attribute failure")

    monkeypatch.setattr(fr, "settings_autoscale", _boom)
    assert _plain_agent().max_concurrent_episodes == 1


def test_the_attestation_expectation_follows_the_ARM_not_the_last_attest(tmp_path, monkeypatch):
    """基準アーム全件が infra になり、閉ループが一周できなかった原因。

    プリフライトは基準→候補の順に attest するので、`_expected_harness_id` は候補のまま
    残る。その後の交互実行で基準アームに入っても期待値は候補のままなので、基準側の
    エピソードが全て『child ran base but candidate was active』で落ちる。
    `paired_evaluate` は `no episode ran on both arms` で中断した。

    親が『いま export しているもの』を見れば、答えのコピーが2つ存在しなくなるので
    ずれようがない。"""
    import json as _json

    from relay.selfimprove import manifest as M
    from relay.selfimprove import runtime_config as RC

    base = M.base_manifest()
    cand = M.apply_genome(base, {"parameters": {"max_retries": 6}})
    path = tmp_path / "arm.json"
    path.write_text(_json.dumps(base), encoding="utf-8")
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(path))

    agent = FleetAgent(agent_url="https://example.invalid/chat/")
    # the stale value the preflight would have left behind
    agent._expected_harness_id = M.harness_id(cand)
    assert agent._active_harness_id() == M.harness_id(base), (
        "the parent must read the manifest it actually installed")


def test_no_installed_manifest_means_no_expectation_to_check(monkeypatch):
    """何も install されていなければ照合対象が無い -- 空を返し、直前の attest に委ねる。"""
    from relay.selfimprove import runtime_config as RC
    monkeypatch.delenv(RC.OVERRIDE_ENV, raising=False)
    agent = FleetAgent(agent_url="https://example.invalid/chat/")
    assert agent._active_harness_id() == ""


@pytest.mark.parametrize("genome", [None, {"parameters": {"max_retries": 6}}])
def test_parent_and_child_compute_the_same_id_for_the_same_file(tmp_path, monkeypatch, genome):
    """置き換えた期待値が、静かな別の不一致になっていないこと。

    親は生の JSON から id を計算し、子は `active_manifest(refresh=True)` から計算する。
    後者が正規化や既定値の補完をしていれば、同じファイルに対して両者が違う答えを出し、
    ひとつの不一致を別の不一致に取り替えただけになる。"""
    man = M.base_manifest() if genome is None else M.apply_genome(M.base_manifest(), genome)
    path = tmp_path / "m.json"
    path.write_text(json.dumps(man), encoding="utf-8")
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(path))
    parent = FleetAgent(agent_url="https://example.invalid/chat/")._active_harness_id()
    child = M.harness_id(RC.active_manifest(refresh=True))
    assert parent == child, "親と子が同じファイルに違う id を付けている"
