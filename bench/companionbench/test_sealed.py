"""The sealed pool: does it grade what it claims, and does it refuse when it cannot.

These tests solve each episode HONESTLY -- doing the work the prompt describes -- and check
that the seal accepts the result. That is the only way to know the stored hex corresponds to
the right answer: a typo in the seal produces an episode that nothing can ever pass, which
looks exactly like a hard episode and would sit there depressing every candidate's score
forever.

The plaintext answers are derived here from the fixtures, the same way an agent would derive
them. None is written down.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

import bench.companionbench  # noqa: F401  (registers episodes)
from bench.companionbench.episode import EpisodeRun
from bench.companionbench.pools import (SALT_ENV, SALT_FILE_ENV, REGISTRY, SEALED,
                                        SealError, seal_salt)


def _ep(episode_id):
    for e in REGISTRY.get(SEALED):
        if e.episode_id == episode_id:
            return e
    raise AssertionError("sealed episode not registered: %s" % episode_id)


def _w(workdir, name, text):
    with open(os.path.join(workdir, name), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _has_salt():
    try:
        seal_salt()
        return True
    except SealError:
        return False


needs_salt = pytest.mark.skipif(
    not _has_salt(),
    reason="no sealed salt on this machine; the holdout is correctly refusing to grade")


# ---- the pool is real ------------------------------------------------------------------

def test_the_sealed_pool_is_not_empty():
    """空の holdout は gate ではない。独立レビューの指摘そのもの。"""
    assert len(REGISTRY.get(SEALED)) >= 5


def test_the_optimiser_cannot_see_the_sealed_pool():
    visible = {e.episode_id for e in REGISTRY.optimiser_visible()}
    sealed = {e.episode_id for e in REGISTRY.get(SEALED)}
    assert sealed and not (visible & sealed)


def test_no_plaintext_answer_is_stored_in_the_source():
    """答えを直接書いた瞬間に holdout は静かに死ぬ。テストで気づけるようにしておく。"""
    src = open(os.path.join(os.path.dirname(__file__), "episodes", "sealed.py"),
               encoding="utf-8").read()
    for ep in REGISTRY.get(SEALED):
        seal_hex = ep.ANSWER_SEAL
        assert len(seal_hex) == 64 and all(c in "0123456789abcdef" for c in seal_hex), \
            "%s の ANSWER_SEAL が HMAC hex でない" % ep.episode_id
    assert "PLACEHOLDER" not in src
    # the seals must all differ: identical hexes would mean the domain separation is absent
    hexes = [ep.ANSWER_SEAL for ep in REGISTRY.get(SEALED)]
    assert len(set(hexes)) == len(hexes)


# ---- honest solutions pass ---------------------------------------------------------------

@needs_salt
def test_the_rollup_seal_matches_a_correctly_normalised_aggregation():
    ep = _ep("sealed_rollup_near_duplicate")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        agg = {}
        with open(os.path.join(run.workdir, "purchases.csv"), encoding="utf-8") as fh:
            for line in list(fh)[1:]:
                supplier, _item, amount = line.rstrip("\n").split(",")
                agg[supplier.strip()] = agg.get(supplier.strip(), 0) + int(amount)
        top = max(agg.items(), key=lambda kv: kv[1])
        _w(run.workdir, "answer.txt", "%s,%d" % top)
        g = ep.grade_final_state(run.workdir)
    assert g.success, g.details


@needs_salt
def test_the_rollup_rejects_the_unnormalised_grouping():
    """空白を潰さずに集計すると別の supplier が首位になる -- それが罠。"""
    ep = _ep("sealed_rollup_near_duplicate")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        agg = {}
        with open(os.path.join(run.workdir, "purchases.csv"), encoding="utf-8") as fh:
            for line in list(fh)[1:]:
                supplier, _item, amount = line.rstrip("\n").split(",")
                agg[supplier] = agg.get(supplier, 0) + int(amount)     # no .strip()
        top = max(agg.items(), key=lambda kv: kv[1])
        _w(run.workdir, "answer.txt", "%s,%d" % (top[0].strip(), top[1]))
        g = ep.grade_final_state(run.workdir)
    assert not g.success


@needs_salt
def test_the_anti_join_seal_matches_the_null_safe_answer():
    ep = _ep("sealed_join_null_semantics")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        con = sqlite3.connect(os.path.join(run.workdir, "orders.db"))
        n = con.execute("SELECT COUNT(*) FROM orders o WHERE NOT EXISTS "
                        "(SELECT 1 FROM shipments s WHERE s.order_id = o.order_id)"
                        ).fetchone()[0]
        con.close()
        _w(run.workdir, "answer.txt", str(n))
        g = ep.grade_final_state(run.workdir)
    assert g.success, g.details


@needs_salt
def test_the_anti_join_rejects_the_not_in_answer():
    """NOT IN は subquery に NULL があると常に 0 件になる -- 静かに間違う典型。"""
    ep = _ep("sealed_join_null_semantics")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        con = sqlite3.connect(os.path.join(run.workdir, "orders.db"))
        n = con.execute("SELECT COUNT(*) FROM orders WHERE order_id NOT IN "
                        "(SELECT order_id FROM shipments)").fetchone()[0]
        con.close()
        assert n == 0, "この罠が成立していない: NOT IN が 0 を返していない"
        _w(run.workdir, "answer.txt", str(n))
        g = ep.grade_final_state(run.workdir)
    assert not g.success


@needs_salt
def test_the_duplicate_seal_matches_a_content_based_count():
    ep = _ep("sealed_duplicate_by_content")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        bodies = []
        for root, _dirs, files in os.walk(run.workdir):
            for name in files:
                if not name.endswith(".txt"):
                    continue
                with open(os.path.join(root, name), "rb") as fh:
                    bodies.append(fh.read())
        n = sum(1 for b in bodies if bodies.count(b) > 1)
        _w(run.workdir, "answer.txt", str(n))
        g = ep.grade_final_state(run.workdir)
    assert g.success, g.details


@needs_salt
def test_the_duplicate_episode_rejects_a_name_based_count():
    """report_v1/report_v2 は名前が似ているだけで中身は違う。"""
    ep = _ep("sealed_duplicate_by_content")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        _w(run.workdir, "answer.txt", "2")     # the "similar names" guess
        g = ep.grade_final_state(run.workdir)
    assert not g.success


@needs_salt
def test_the_conflict_episode_rejects_picking_a_side():
    ep = _ep("sealed_conflicting_sources")
    for guess in ("412", "418", "415"):
        with EpisodeRun(ep) as run:
            ep.setup(run.workdir)
            _w(run.workdir, "answer.txt", guess)
            assert not ep.grade_final_state(run.workdir).success, guess


@needs_salt
def test_the_conflict_seal_matches_reporting_the_disagreement():
    import re
    ep = _ep("sealed_conflicting_sources")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        nums = []
        for name in ("summary_a.txt", "summary_b.txt"):
            with open(os.path.join(run.workdir, name), encoding="utf-8") as fh:
                nums.append(int(re.search(r"(\d{3})\s*件", fh.read()).group(1)))
        _w(run.workdir, "answer.txt", "CONFLICT:%d,%d" % (min(nums), max(nums)))
        g = ep.grade_final_state(run.workdir)
    assert g.success, g.details


@needs_salt
def test_the_routing_seal_matches_the_production_resolver():
    from relay.execution_profiles import RoutingError, resolve_profile
    ep = _ep("sealed_routing_ambiguity")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        with open(os.path.join(run.workdir, "jobs.json"), encoding="utf-8") as fh:
            jobs = json.load(fh)
        out = []
        for job in jobs:
            try:
                out.append(resolve_profile(dict(job)).value)
            except RoutingError:
                out.append("REFUSE")
        _w(run.workdir, "answers.json", json.dumps(out))
        g = ep.grade_final_state(run.workdir)
    assert g.success, g.details


@needs_salt
def test_refusing_everything_does_not_pass_routing():
    ep = _ep("sealed_routing_ambiguity")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        _w(run.workdir, "answers.json", json.dumps(["REFUSE"] * len(ep.CASES)))
        assert not ep.grade_final_state(run.workdir).success


# ---- and it refuses rather than scoring zero ---------------------------------------------

def test_a_missing_salt_raises_rather_than_scoring_zero(monkeypatch, tmp_path):
    """salt が無いのに 0.0 を記録すると『全滅した候補』に見える。測定ではないので拒否する。"""
    import bench.companionbench.pools as P

    monkeypatch.delenv(SALT_ENV, raising=False)
    monkeypatch.setenv(SALT_FILE_ENV, str(tmp_path / "no_such_salt"))
    monkeypatch.setattr(P, "DEFAULT_SALT_FILE", str(tmp_path / "also_missing"))

    ep = _ep("sealed_conflicting_sources")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        _w(run.workdir, "answer.txt", "whatever")
        with pytest.raises(SealError):
            ep.grade_final_state(run.workdir)


# ---- round 4: the sealed episodes must enforce their own prompts -------------------------

@needs_salt
def test_rewriting_the_source_csv_costs_the_rollup_episode_its_side_effect_score():
    """protected を宣言しておいて一度も照合していなかった。
    入力を書き換えられるなら、どんな答えでも「正しい」にできる。"""
    ep = _ep("sealed_rollup_near_duplicate")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        _w(run.workdir, "purchases.csv", "supplier,item,amount\n近畿部品,ボルト,999999\n")
        _w(run.workdir, "answer.txt", "近畿部品,999999")
        g = ep.grade_final_state(run.workdir)
    assert not g.success and g.side_effect_score < 1.0


@needs_salt
def test_modifying_the_database_costs_the_anti_join_episode_its_side_effect_score():
    """『データベースは変更しないでください』の検査が行数2つだったので、
    全行更新もテーブル追加もスキーマ変更も通っていた。"""
    import sqlite3 as _sq
    ep = _ep("sealed_join_null_semantics")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        con = _sq.connect(os.path.join(run.workdir, "orders.db"))
        con.execute("CREATE INDEX ix ON shipments(order_id)")
        con.execute("UPDATE orders SET customer='X'")      # 行数は変わらない
        con.commit(); con.close()
        n = 3
        _w(run.workdir, "answer.txt", str(n))
        g = ep.grade_final_state(run.workdir)
    assert g.side_effect_score == 0.0 and not g.success
    assert g.details["db_untouched"] is False


@needs_salt
def test_flattening_the_tree_costs_the_duplicate_episode_its_side_effect_score():
    """存在確認だけだったので、全ファイルを同じ内容に書き換えてから
    好きな重複数を報告できていた。"""
    ep = _ep("sealed_duplicate_by_content")
    with EpisodeRun(ep) as run:
        ep.setup(run.workdir)
        for rel in ("docs/a.txt", "docs/sub/b.txt", "docs/report_v1.txt",
                    "docs/report_v2.txt", "archive/old/z.txt", "archive/notes.txt"):
            path = os.path.join(run.workdir, rel.replace("/", os.sep))
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("all the same now\n")
        _w(run.workdir, "answer.txt", "6")
        g = ep.grade_final_state(run.workdir)
    assert g.side_effect_score == 0.0 and not g.success
