# -*- coding: utf-8 -*-
"""The fair-reproduction test: a generated Skill is only earned when the answer was not pasted
into its trigger and a short, ordinary prompt still reaches it.

These are fixtures, not the installed library -- `skills/` is gitignored, so a test that asked
the real store would pass on a developer's machine and match nothing on CI (see
test_skill_match_japanese for the same lesson). Each Skill here is written into a tmp store and
approved through the normal two-step gate, so the matcher sees exactly what it would in
production.
"""
from pathlib import Path

from relay.skills import SkillStore
from tools.skill_fairtest import (
    answer_leak,
    fairtest,
    fires_on_short_prompt,
)


# The withheld September artefact, reduced to its identifying material: a product code, a
# customer name, a measured figure. A fair Skill must be reachable WITHOUT any of these
# appearing in its matching face.
IDEAL_OUTPUT = (
    "2026年9月 OGF 粒子径報告。製品 ea300、顧客 copperfilm、"
    "平均粒径 micronthreeeight、歌出荷量 twelvefourtons。スライドは承認済構成。"
)

SHORT_PROMPTS = [
    "粒子径の報告資料を作って",
    "OGF の月次報告をお願い",
]

# The generic vocabulary of THIS task: the subject words a person types in a short prompt
# (粒子径, OGF, 月次報告). A fair face is allowed to share these; they are not the answer. The
# report's identifying material -- product code, customer, figures -- is deliberately absent.
TOPIC_VOCAB = "粒子径 粒径 OGF 月次 報告 報告資料 測定 集計 図 資料 作成"


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return SkillStore(
        tmp_path / "project", tmp_path / "state.sqlite3", tmp_path / "gates"
    )


def _approve(store, name, description, when_to_use):
    """Write a one-Skill bundle, import it and take it through the two-step approval gate so the
    matcher treats it as trusted and model-invocable -- the only state fairtest looks at."""
    src = store.project_root.parent / "download" / name
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: \"{description}\"\n"
        f"when_to_use: \"{when_to_use}\"\n"
        "---\n\n"
        "# 手順\n抽出→集計→図→資料の順で組み立てる。\n",
        encoding="utf-8",
    )
    store.import_external(src)
    challenge = store.request_approval(name)
    store.confirm_approval(name, challenge["token"])


# A Skill whose face describes WHEN it applies, in ordinary topic words, without quoting the
# withheld report. This is the fair Skill: short prompts reach it, no answer is pasted in.
FAIR_NAME = "particle-report"
FAIR_DESC = "粒子径の測定結果から月次の報告資料を組み立てる"
FAIR_WHEN = "粒子径 粒径 OGF の月次報告。測定値を集計し図を作り資料に仕上げる"


def test_fair_skill_passes(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _approve(store, FAIR_NAME, FAIR_DESC, FAIR_WHEN)
    report = fairtest(store, FAIR_NAME, IDEAL_OUTPUT, SHORT_PROMPTS, TOPIC_VOCAB)
    assert report["fair"] is True
    assert report["cheating_tokens"] == []
    assert report["short_prompt"]["any_fires"] is True
    # The one thing a clean report must never be read as.
    assert report["output_judged"] is False


def test_answer_pasted_into_the_face_is_caught(tmp_path, monkeypatch):
    """The report's identifying material (product code, customer, the figure) written into
    when_to_use. It would match, but only because the answer is in the trigger -- cheating."""
    store = _store(tmp_path, monkeypatch)
    leaked_when = FAIR_WHEN + " ea300 copperfilm micronthreeeight twelvefourtons"
    _approve(store, "particle-report-cheat", FAIR_DESC, leaked_when)
    leaked = answer_leak(
        store._model_invocable_trusted()["particle-report-cheat"], IDEAL_OUTPUT, TOPIC_VOCAB
    )
    assert leaked, "the pasted-in product code/customer/figure must be detected"
    report = fairtest(store, "particle-report-cheat", IDEAL_OUTPUT, SHORT_PROMPTS, TOPIC_VOCAB)
    assert report["fair"] is False
    assert report["cheating_tokens"]


def test_fair_face_has_no_leak(tmp_path, monkeypatch):
    """The honest face shares ordinary topic words with the report (報告, 月次, 粒子径) and that
    is how matching is meant to work; those are NOT leaks. Only the report's identifying
    material counts, and the fair Skill carries none of it."""
    store = _store(tmp_path, monkeypatch)
    _approve(store, FAIR_NAME, FAIR_DESC, FAIR_WHEN)
    leaked = answer_leak(store._model_invocable_trusted()[FAIR_NAME], IDEAL_OUTPUT, TOPIC_VOCAB)
    assert leaked == []


def test_long_prompt_only_skill_fails_the_short_prompt(tmp_path, monkeypatch):
    """A Skill whose trigger is a paragraph of paths and contract terms -- the '無駄に長い
    プロンプト' the operator forbade. It does not quote the answer, so it is not cheating
    by leak; but no short ordinary prompt reaches it, so it is not fair either."""
    store = _store(tmp_path, monkeypatch)
    # A face built from an unrelated, highly specific vocabulary: a short everyday prompt about
    # the particle report shares nothing distinctive with it, so the confident matcher refuses.
    long_when = (
        "ingest the quarterly kubernetes autoscaler latency histogram and reconcile the "
        "grafana dashboards against the prometheus retention window before emitting the sla "
        "attainment csv export"
    )
    _approve(store, "telemetry-pipeline", "ingest telemetry and reconcile dashboards", long_when)
    short = fires_on_short_prompt(store, "telemetry-pipeline", SHORT_PROMPTS)
    assert short["any_fires"] is False
    report = fairtest(store, "telemetry-pipeline", IDEAL_OUTPUT, SHORT_PROMPTS)
    assert report["fair"] is False


def test_unknown_skill_is_not_fair(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    report = fairtest(store, "no-such-skill", IDEAL_OUTPUT, SHORT_PROMPTS)
    assert report["fair"] is False
    assert report["output_judged"] is False
