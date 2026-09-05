"""Phase 0 acceptance: an experiment must be attributable to the change that produced it.

The self-improvement loop already had the hard parts -- fresh/burned slices, a significance
gate, frozen-set checks before and after, four separate infra guards. What it did not have
was identity. `validate` computed the resolved SETS for both arms and then reported only
their sizes; the archive was handed a literal `slice_ids=[]` with a TODO beside it; no
experiment or harness id existed anywhere in the repository. So a number could be repeated
but never reproduced, and no result could be tied to the tasks it came from.

Each test below is one of the seven acceptance criteria in the brief. They are written
against the real functions rather than a description of them, because the failure mode
being guarded is precisely a docstring that promises per-instance data while the code
returns counts.
"""
import json
import os
import tempfile
import pytest

from relay import provenance as PROV
from relay.selfimprove import experiment as EX
from relay.selfimprove import frozen as F
from relay.selfimprove import l2
from relay.selfimprove.archive import Archive

SLICE = ["inst-%02d" % i for i in range(10)]


def _report(*, on_resolved, off_resolved, on_failed=(), off_failed=(),
            on_infra=(), off_infra=(), keep=True):
    """A report shaped like the one loop.validate now returns."""
    return {
        "toggle": "T", "n": len(SLICE), "dataset": "Verified", "seed": 7,
        "slice_ids": list(SLICE),
        "on_resolved": len(on_resolved), "off_resolved": len(off_resolved),
        "on": {"resolved_ids": sorted(on_resolved), "failed_ids": sorted(on_failed),
               "infra_ids": sorted(on_infra)},
        "off": {"resolved_ids": sorted(off_resolved), "failed_ids": sorted(off_failed),
                "infra_ids": sorted(off_infra)},
        "gate": {"keep": keep, "verdict": "keep" if keep else "revert", "reason": "stub"},
    }


def _run(report, **kw):
    """run_iteration with the real machinery but a stubbed validate and a temp archive.

    frozen_intact is pinned to "intact" the same way relay/selfimprove/test_l2.py does it:
    the frozen baseline is a property of a real checkout, and these tests are about what the
    iteration RECORDS, not about the judge's integrity (which test_frozen.py owns).
    """
    kw.setdefault("archive_path", os.path.join(tempfile.mkdtemp(prefix="ph0_"), "archive.jsonl"))
    orig = F.frozen_intact
    F.frozen_intact = lambda *a, **k: (True, [])
    try:
        return l2.run_iteration(toggle="T", n=len(SLICE),
                                validate_fn=lambda **_: report, **kw)
    finally:
        F.frozen_intact = orig


# ---- criterion 1: ON and OFF results preserve per-instance identity ------------------

def test_the_report_carries_which_instances_not_just_how_many():
    rep = _report(on_resolved=["inst-01", "inst-02"], off_resolved=["inst-01"])
    assert rep["on"]["resolved_ids"] == ["inst-01", "inst-02"]
    assert rep["off"]["resolved_ids"] == ["inst-01"]
    # the pairing that a McNemar test needs: same task, different arm
    gained = set(rep["on"]["resolved_ids"]) - set(rep["off"]["resolved_ids"])
    assert gained == {"inst-02"}


def test_the_grade_reader_returns_all_three_sets():
    """loop._grade_arm must hand back resolved/failed/infra, not just resolved+count."""
    import inspect

    from relay.selfimprove import loop
    src = inspect.getsource(loop._grade_arm)
    assert "return resolved, graded, failed, infra" in src


# ---- criterion 2: infra failures are distinguishable from real failures ---------------

def test_an_ungradable_instance_is_infra_not_a_failure():
    rep = _report(on_resolved=["inst-01"], on_failed=["inst-02"], on_infra=["inst-03"],
                  off_resolved=["inst-01"])
    assert rep["on"]["failed_ids"] == ["inst-02"]
    assert rep["on"]["infra_ids"] == ["inst-03"]
    # an instance the grader could not judge is in neither of the other two sets
    assert not set(rep["on"]["infra_ids"]) & set(rep["on"]["failed_ids"])
    assert not set(rep["on"]["infra_ids"]) & set(rep["on"]["resolved_ids"])


def test_infra_instances_are_excluded_from_the_graded_count():
    import inspect

    from relay.selfimprove import loop
    src = inspect.getsource(loop._grade_arm)
    assert "graded = len(resolved) + len(failed)" in src, "infra を graded に数えている"


# ---- criterion 3: sentinel receives per-instance sets ---------------------------------

def test_the_sentinel_is_fed_from_the_report_without_being_asked():
    """以前は呼び出し側が手で渡さない限り sentinel が評価されなかった。"""
    import inspect
    src = inspect.getsource(l2.run_iteration)
    assert 'report.get("on") or {}).get("resolved_ids")' in src


# ---- criterion 4: sentinel failure or missing data prevents auto-apply -----------------

def test_auto_apply_is_blocked_when_a_configured_sentinel_cannot_be_evaluated():
    d = tempfile.mkdtemp(prefix="ph0_sent_")
    sent = os.path.join(d, "sentinel.json")
    # configured, but with no members/baseline -> unevaluable
    json.dump({"instance_ids": [], "baseline_resolved": []}, open(sent, "w", encoding="utf-8"))
    rep = _report(on_resolved=SLICE[:6], off_resolved=SLICE[:3], keep=True)
    out = _run(rep, sentinel_path=sent, auto_commit=True)
    assert out["final_keep"] is False, "評価できない sentinel で auto-apply が通っている"
    assert "sentinel" in (out.get("reason") or "").lower()
    assert out["sentinel"]["status"] == "unevaluable"


def test_the_same_case_still_queues_for_review_without_auto_apply():
    """完了した測定を捨てない。人が「未評価」と見て判断できる状態にする。"""
    d = tempfile.mkdtemp(prefix="ph0_sent2_")
    sent = os.path.join(d, "sentinel.json")
    json.dump({"instance_ids": [], "baseline_resolved": []}, open(sent, "w", encoding="utf-8"))
    rep = _report(on_resolved=SLICE[:6], off_resolved=SLICE[:3], keep=True)
    out = _run(rep, sentinel_path=sent, auto_commit=False)
    assert out["final_keep"] is True
    assert any("UNEVALUABLE" in n for n in out.get("notes", []))


# ---- criterion 5: every experiment is reproducible from its recorded metadata ----------

def test_a_fingerprint_names_the_harness_that_actually_ran():
    fp = EX.harness_fingerprint(genome={"knobs": {"T": "1"}}, execution_profile="LOCAL_LOOP",
                                env={"SWE_MISS85_DISCIPLINE": "1"})
    f = fp["fields"]
    assert len(fp["harness_id"]) == 64                       # sha256 hex
    assert f["git_commit"]                                    # never silently absent
    assert f["env_toggles"]["SWE_MISS85_DISCIPLINE"] == "1"
    assert f["execution_profile"] == "LOCAL_LOOP"


def test_the_fingerprint_changes_when_the_harness_changes_and_not_otherwise():
    a = EX.harness_fingerprint(genome={"knobs": {"T": "1"}}, env={})
    b = EX.harness_fingerprint(genome={"knobs": {"T": "1"}}, env={})
    c = EX.harness_fingerprint(genome={"knobs": {"T": "0"}}, env={})
    assert a["harness_id"] == b["harness_id"], "同じ構成で id が揺れている"
    assert a["harness_id"] != c["harness_id"], "構成が違うのに同じ id"


def test_an_unrecorded_toggle_would_be_an_unrecorded_confound():
    """fingerprint に載る toggle の一覧が空になっていないこと。"""
    assert EX.FINGERPRINT_ENV_KEYS, "挙動を変える toggle を一つも記録していない"


def test_experiment_ids_are_unique_and_time_ordered():
    a = EX.new_experiment_id(ts=1000)
    b = EX.new_experiment_id(ts=2000)
    assert a != b
    assert a < b, "時系列に並ばない id は grep しづらい"


def test_two_ids_from_one_clock_reading_still_differ(monkeypatch):
    """WINDOWS の time.time() は約 15.6ms 刻みで進む。

    tail は sha256(time.time() | pid) だったので、同じ tick 内の2回は**同一の id** になった。
    台帳は append-only で仮説を書き換えないから、2回目は
    "experiment ... already has a proposal" で落ちる。13分のスイートで3回に1回ほど失敗し、
    flaky と呼ばれる形をしていた。docstring は最初から
    「同じ秒に始まった2つを区別できればよい」と要求していて、その要求が満たされていなかった。

    ts を渡さない経路を突く。渡す経路は上のテストが押さえている。
    """
    monkeypatch.setattr(EX.time, "time", lambda: 1788600000.0)
    ids = {EX.new_experiment_id() for _ in range(50)}
    assert len(ids) == 50, "同一 tick 内で id が衝突した: %d/50" % len(ids)


def test_the_candidate_id_depends_on_the_parent_not_only_the_genome():
    """試しているのは節点ではなく辺。親が違えば別の候補。"""
    g = {"knobs": {"T": "1"}}
    assert EX.candidate_id(g, parent_harness_id="p1") != EX.candidate_id(g, parent_harness_id="p2")
    assert EX.candidate_id(g, parent_harness_id="p1") == EX.candidate_id(g, parent_harness_id="p1")


# ---- criterion 6: archive records identify the actual evaluated tasks -------------------

def test_the_archive_records_the_real_slice_not_an_empty_list():
    d = tempfile.mkdtemp(prefix="ph0_arch_")
    path = os.path.join(d, "archive.jsonl")
    rep = _report(on_resolved=SLICE[:6], off_resolved=SLICE[:3])
    _run(rep, archive_path=path)
    entries = Archive(path).all()
    assert len(entries) == 1
    assert entries[0]["slice_ids"] == SLICE, "空のスライスを書いている"


def test_the_archive_entry_carries_the_experiment_identity():
    d = tempfile.mkdtemp(prefix="ph0_arch2_")
    path = os.path.join(d, "archive.jsonl")
    _run(_report(on_resolved=SLICE[:5], off_resolved=SLICE[:2]), archive_path=path)
    desc = Archive(path).all()[0]["descriptors"]
    ident = desc["experiment"]
    for key in ("experiment_id", "candidate_id", "parent_harness_id", "baseline_harness_id",
                "dataset", "slice_ids", "toggle"):
        assert key in ident, "identity に %s が無い" % key
    assert ident["slice_ids"] == SLICE
    # 二つの指紋が別物として記録されること。以前は候補込みで取った指紋ひとつを
    # baseline_harness_id にも parent にも使い回し、parent_harness_id は空だった --
    # 同じハッシュを指す3フィールドのうち2つが嘘という状態。
    assert len(desc["baseline_fingerprint"]["harness_id"]) == 64
    assert len(desc["candidate_fingerprint"]["harness_id"]) == 64
    assert (desc["baseline_fingerprint"]["harness_id"]
            != desc["candidate_fingerprint"]["harness_id"]), "候補と基準の指紋が同一"
    assert ident["parent_harness_id"] == desc["baseline_fingerprint"]["harness_id"]
    assert ident["baseline_harness_id"] == ident["parent_harness_id"]
    assert ident["parent_harness_id"], "親が空のまま -- 何からの派生か記録されていない"


def test_no_placeholder_empty_slice_remains_in_the_source():
    """実データがあるのに空を書く、という名指しで禁じられた形が復活しないこと。"""
    import inspect
    src = inspect.getsource(l2.run_iteration)
    assert "slice_ids=[]," not in src


# ---- an instance the grader never mentioned at all ------------------------------------

def test_a_target_the_grader_never_judged_is_infra_not_a_silent_disappearance():
    """独立レビューの指摘: 結果ファイルに行が無い instance はどの集合にも入らず、
    会計から消えていた。分母が黙って縮むと『半分しか走らなかった arm』が
    『走った arm』と同じ顔をする。"""
    import json as _json
    import tempfile as _tf

    from relay.selfimprove import loop

    d = _tf.mkdtemp(prefix="ga_")
    targets = os.path.join(d, "t.txt")
    with open(targets, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("inst-01\ninst-02\ninst-03\ninst-04\n")
    results = os.path.join(d, "grade_results.jsonl")
    with open(results, "w", encoding="utf-8", newline="\n") as fh:
        for iid, verdict in (("inst-01", "RESOLVED"), ("inst-02", "not"),
                             ("inst-03", "EVALERR")):
            fh.write(_json.dumps({"runid": "R", "instance_id": iid,
                                  "verdict": verdict}) + "\n")
        # inst-04 は一行も無い -- グレーダが触れなかった

    orig_run, orig_results = loop.subprocess.run, loop.GRADE_RESULTS
    loop.subprocess.run = lambda *a, **k: None
    loop.GRADE_RESULTS = results
    try:
        resolved, graded, failed, infra = loop._grade_arm(d, targets, "ds", "R")
    finally:
        loop.subprocess.run, loop.GRADE_RESULTS = orig_run, orig_results

    assert resolved == {"inst-01"}
    assert failed == {"inst-02"}
    assert infra == {"inst-03", "inst-04"}, "触れられなかった inst-04 が infra でない"
    assert graded == 2, "判定されていないものを graded に数えてはいけない"


def test_no_result_file_makes_every_target_infra_not_zero_failures():
    import tempfile as _tf

    from relay.selfimprove import loop

    d = _tf.mkdtemp(prefix="ga2_")
    targets = os.path.join(d, "t.txt")
    with open(targets, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("inst-01\ninst-02\n")

    orig_run, orig_results = loop.subprocess.run, loop.GRADE_RESULTS
    loop.subprocess.run = lambda *a, **k: None
    loop.GRADE_RESULTS = os.path.join(d, "nope.jsonl")
    try:
        resolved, graded, failed, infra = loop._grade_arm(d, targets, "ds", "R")
    finally:
        loop.subprocess.run, loop.GRADE_RESULTS = orig_run, orig_results

    assert graded == 0 and not resolved and not failed
    assert infra == {"inst-01", "inst-02"}


def test_a_deleted_sentinel_file_is_unevaluable_not_unconfigured():
    """sentinel.json を消すだけで tripwire が黙って外れていた。
    パスを渡した呼び出し側は『検査してほしい』と言っている。"""
    import tempfile as _tf

    with _tf.TemporaryDirectory() as d:
        res = _run(_report(on_resolved=list(SLICE), off_resolved=list(SLICE)[:1]),
                   sentinel_path=os.path.join(d, "sentinel_deleted.json"),
                   auto_commit=True,
                   archive_path=os.path.join(d, "a.jsonl"))

    assert res["final_keep"] is False, "canary が消えたまま auto_commit を許した"
    assert any("missing" in (nt or "") for nt in res["notes"]), res["notes"]


# ---- an archived row must be able to answer what it was ---------------------------------

def test_the_archive_row_can_reconstruct_the_comparison():
    """『どの id が通ったか』しか答えられない行は、再検証には使えない。
    数えられるが読み直せない記録は、archive の目的の大半を果たしていない。"""
    import tempfile as _tf

    from relay.selfimprove.archive import Archive
    from relay.selfimprove.controller import EvolutionController
    from relay.selfimprove.ledger import HypothesisLedger

    d = _tf.mkdtemp(prefix="arcfull_")
    arc = Archive(os.path.join(d, "a.jsonl"))
    ctl = EvolutionController(
        ledger=HypothesisLedger(os.path.join(d, "h.jsonl")), archive=arc)

    orig = F.frozen_intact
    F.frozen_intact = lambda *a, **k: (True, [])
    try:
        ctl.run_candidate(
        evidence=[{"kind": "own_measurements", "authority": PROV.AGENT_INFERENCE}],
            genome={"parameters": {"memory_max_items": 9}},
            hypothesis="more recall helps", target_failure_class="missing_evidence",
            evaluate=lambda *a, **k: {
                "gate": {"keep": False, "verdict": "inconclusive", "reason": "noise"},
                "paired_ids": ["e1", "e2"],
                "on": {"resolved_ids": ["e1"], "failed_ids": ["e2"], "infra_ids": []},
                "off": {"resolved_ids": ["e1"], "failed_ids": ["e2"], "infra_ids": []},
                "candidate_results": [{"episode_id": "e1", "success": True,
                                       "functional_score": 1.0, "latency_s": 0.2}],
                "baseline_results": [{"episode_id": "e1", "success": True,
                                      "functional_score": 1.0, "latency_s": 0.3}],
                "security": {"regressed": False, "failing": [], "comparable": 1,
                             "passed_count": 1},
                "sentinel": {"regressed": False, "comparable": 3},
                "regression": {"regressed": False},
                "pools": {"evolution": ["e1"], "regression": ["e2"], "sealed": ["s1"]},
                "grader_version": "abc123", "seed": 7, "agent": "SimulatedAgent",
                "baseline_harness_id": "b" * 64,
            })
    finally:
        F.frozen_intact = orig

    desc = arc.all()[-1]["descriptors"]
    for key in ("experiment_id", "candidate_id", "candidate_harness_id",
                "parent_harness_id", "baseline_harness_id", "components", "parameters",
                "decision_state", "decision_reason", "paired_ids", "on", "off",
                "episode_results", "security", "sentinel", "regression", "infra",
                "pools", "grader_version", "seed", "agent", "harness_fingerprint"):
        assert key in desc, "archive 行に %s が無い" % key
    assert desc["episode_results"]["candidate"][0]["functional_score"] == 1.0
    assert desc["parent_harness_id"] and desc["parent_harness_id"] != desc["candidate_harness_id"]
    assert desc["grader_version"] == "abc123"


def test_the_runner_supplies_what_the_archive_records():
    """archive 側だけ広げても、runner が渡さなければ空欄が並ぶだけ。"""
    import tempfile as _tf

    import bench.companionbench.agents as A
    from bench.companionbench import runner as R
    from relay.selfimprove import manifest as MM

    base = MM.base_manifest()
    cand = MM.apply_genome(base, {"parameters": {"memory_max_items": 9}})
    out = R.paired_evaluate(base, cand, A.in_process(lambda *_a: ""),
                            tmpdir=_tf.mkdtemp(prefix="pefull_"))
    for key in ("candidate_results", "baseline_results", "pools", "agent",
                "grader_version", "latency_s"):
        assert key in out, "runner が %s を返していない" % key
    assert out["pools"]["sealed"], "封印プールが記録されていない"
    assert out["grader_version"] != "unavailable"


def test_the_row_records_which_dataset_and_which_baseline_and_which_agent():
    """round 7 の残り3点。seed 相当が無い、baseline の詳細指紋が無い、
    エージェント設定がクラス名だけ -- どれも『この結果は何と比較可能か』に答えられない。"""
    import tempfile as _tf

    import bench.companionbench.agents as A
    from bench.companionbench import runner as R
    from relay.selfimprove import manifest as MM

    base = MM.base_manifest()
    cand = MM.apply_genome(base, {"parameters": {"memory_max_items": 9}})
    out = R.paired_evaluate(base, cand, A.in_process(lambda *_a: ""),
                            tmpdir=_tf.mkdtemp(prefix="ds_"))

    assert out["dataset_fingerprint"], "どのデータセットで測ったか記録されていない"
    assert isinstance(out["agent"], dict) and out["agent"]["execution_target"]
    assert out["baseline_genome"]["parameters"]["memory_max_items"] == 5


def test_the_dataset_fingerprint_moves_when_the_sealed_instance_moves(monkeypatch):
    """salt を替えれば封印プールの問いが変わる。行が同じ指紋のままなら、
    比較不能な結果同士が比較可能に見える。"""
    from bench.companionbench import runner as R
    from bench.companionbench.pools import SALT_ENV

    monkeypatch.setenv(SALT_ENV, "salt-one")
    first = R.dataset_fingerprint()
    monkeypatch.setenv(SALT_ENV, "salt-two")
    second = R.dataset_fingerprint()
    assert first and second and first != second


def test_the_dataset_fingerprint_does_not_leak_the_sealed_answers():
    """指紋は期待値の上で計算するが、記録されるのはハッシュだけ。"""
    import inspect

    from bench.companionbench import runner as R
    assert "sha256" in inspect.getsource(R.dataset_fingerprint)
    assert len(R.dataset_fingerprint()) == 16


def test_the_fleet_adapter_describes_its_configuration_without_leaking_it():
    """フリートURL・refuter・記憶seed・タイムアウトは結果を変える。
    クラス名だけでは、別の設定で走った2つの行が同じに見える。"""
    import tempfile as _tf

    from bench.companionbench.fleet_agent import FleetAgent

    seed = _tf.mkdtemp(prefix="sd_")
    os.makedirs(os.path.join(seed, "memory"), exist_ok=True)
    with open(os.path.join(seed, "memory", "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("- [x](x.md)\n")

    d = FleetAgent(agent_url="http://secret-host:9999/token-abc",
                   cdp_url="http://127.0.0.1:9222", refuter=True,
                   memory_seed=seed).describe()
    assert d["refuter"] is True and d["memory_seed_digest"]
    assert d["has_cdp_url"] is True
    blob = json.dumps(d)
    assert "secret-host" not in blob and "token-abc" not in blob, "URL が記録に漏れている"


# ---- テストが本番の予測記録を捏造しないこと -------------------------------------------------------

def test_the_hypothesis_ledger_can_be_redirected(monkeypatch, tmp_path):
    """`nightly()` はコントローラを内部で作るので、テストは台帳の行き先を指定できなかった。
    実測: test_policy_wiring を1回走らせるだけで本番台帳が 2171 -> 2291 行に増えた。

    害は小さくない。台帳には結論1018件が溜まり全部 infra_abort で、私はそれを
    「自律ループが2日間失敗し続けた記録」と読んだ。**テスト実行の記録だった。**
    ledger.py の冒頭が書いているとおり、後から書いた仮説は物語であって証拠ではない。
    合成された千件に埋もれた本物の記録も、同じ理由で証拠として使えなくなる。
    """
    from relay.selfimprove.ledger import ENV_PATH, HypothesisLedger, default_path
    target = str(tmp_path / "h.jsonl")
    monkeypatch.setenv(ENV_PATH, target)
    assert default_path() == target
    assert HypothesisLedger().path == target


def test_the_path_is_resolved_at_construction_not_bound_at_import(monkeypatch, tmp_path):
    """`path=DEFAULT_PATH` を既定引数にすると import 時に束縛され、後から差し替えられない。
    frozen.py と authority_ledger.py が同じ罠で記録を残している。3度目。"""
    import inspect

    from relay.selfimprove.ledger import ENV_PATH, HypothesisLedger
    sig = inspect.signature(HypothesisLedger.__init__)
    assert sig.parameters["path"].default is None, (
        "既定引数に本番パスが焼き込まれている: %s" % sig.parameters["path"].default)
    monkeypatch.setenv(ENV_PATH, str(tmp_path / "late.jsonl"))
    assert HypothesisLedger().path.endswith("late.jsonl")


def test_the_suite_itself_is_pointed_away_from_production():
    """conftest がプロセス単位で逃がしていること。個々のテストが覚えている必要を無くす。"""
    import os
    from relay.selfimprove.ledger import DEFAULT_PATH, ENV_PATH
    got = os.environ.get(ENV_PATH)
    assert got, "conftest が %s を設定していない" % ENV_PATH
    assert os.path.abspath(got) != os.path.abspath(DEFAULT_PATH)


def test_the_ledger_lock_treats_a_delete_pending_file_as_contention(tmp_path, monkeypatch):
    """Windows reports a lock being released as PermissionError, not FileExistsError.

    This idiom was chosen because it "has to work identically on Windows", and the single way
    Windows differs is the case it did not handle: a lock file whose last handle has just
    closed with an unlink outstanding sits in delete-pending, and O_CREAT|O_EXCL on it returns
    ERROR_ACCESS_DENIED. The exception then leaves the lock instead of being retried. Measured
    in the identical copy of this loop in relay/task_router.py: 24 concurrent writers, 2
    failures in 8 runs, most threads raising at once.
    """
    import os as _os
    from relay.selfimprove import ledger as L

    lock_path = str(tmp_path / "x.lock")
    real_open = _os.open
    calls = {"n": 0}

    def flaky(p, flags, *a, **k):
        if str(p) == lock_path:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "Permission denied")
        return real_open(p, flags, *a, **k)

    monkeypatch.setattr(_os, "open", flaky)
    with L._exclusive(lock_path):
        pass
    assert calls["n"] >= 3, "the refusals were not actually exercised"
    assert not os.path.exists(lock_path), "the lock must be released on the way out"


def test_the_baseline_lock_treats_a_delete_pending_file_as_contention(tmp_path, monkeypatch):
    """The fourth and last copy of the O_CREAT|O_EXCL loop, and the one that matters most.

    Windows returns ERROR_ACCESS_DENIED -- PermissionError, not FileExistsError -- for a lock
    file whose last handle has just closed with an unlink outstanding. Catching FileExistsError
    alone let a raw PermissionError out of _baseline_lock, where a caller cannot tell it apart
    from BaselineRefused, which is what this function raises when it genuinely cannot get the
    lock. Measured in the identical loop in relay/task_router.py: 24 concurrent writers, 2
    failures in 8 runs.

    Editing frozen.py re-signs the constitution, so this fix carried the operator's explicit
    instruction; the authority ledger holds it verbatim.
    """
    import os as _os
    from relay.selfimprove import frozen as _F

    lock_path = str(tmp_path / ".baseline.lock")
    monkeypatch.setattr(_F, "_BASELINE_LOCK", lock_path)
    real_open = _os.open
    calls = {"n": 0}

    def flaky(p, flags, *a, **k):
        if str(p) == lock_path:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "Permission denied")
        return real_open(p, flags, *a, **k)

    monkeypatch.setattr(_os, "open", flaky)
    with _F._baseline_lock(timeout_s=5.0):
        pass
    assert calls["n"] >= 3, "the refusals were not actually exercised"
    assert not os.path.exists(lock_path), "the lock must be released on the way out"


def test_a_lock_nobody_releases_is_still_refused_not_hung(tmp_path, monkeypatch):
    """The timeout must survive the widened except. A PermissionError that never clears is a
    held lock, and the answer to that is BaselineRefused -- not a raw OS error, and not a spin
    that never ends."""
    import os as _os
    from relay.selfimprove import frozen as _F

    lock_path = str(tmp_path / ".baseline.lock")
    monkeypatch.setattr(_F, "_BASELINE_LOCK", lock_path)
    monkeypatch.setattr(_os, "open", lambda *a, **k: (_ for _ in ()).throw(
        PermissionError(13, "Permission denied")))
    with pytest.raises(_F.BaselineRefused):
        with _F._baseline_lock(timeout_s=0.2):
            pass
