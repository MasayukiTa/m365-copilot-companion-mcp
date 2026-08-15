"""The sealed holdout: the pool that decides whether a gain was real.

WHY THESE ARE DIFFERENT FROM THE OTHER FIFTEEN

The evolution pool is looked at constantly -- every candidate is graded on it, and the
optimiser's whole job is to move its number. That is exactly what makes it useless as
evidence of generalisation: after enough iterations a rise there is as likely to be fit to
the pool as to be a real improvement, and nothing inside the pool can tell those apart.

So this file is built to be structurally UNLIKE it, on purpose, while covering the same
capability surface:

  - the answer is a computed VALUE the agent must derive, not a state it must reach, so an
    episode cannot be passed by reproducing a remembered final workdir
  - each fixture contains a deliberate trap that punishes the shortcut: a row that looks
    like a duplicate and is not, a NULL that changes a join, two documents that disagree
  - the expected answers are absent from this repository. Only their HMACs are here.

THE SEAL

`pools.seal()` stores each answer as an HMAC-SHA256 under a salt that lives outside every
checkout. The grader recomputes the HMAC of what the agent produced and compares in
constant time. Without the salt these episodes REFUSE to grade -- see `SealError` -- rather
than falling back to a plaintext comparison, because a holdout that quietly stops being
sealed still reports a number that looks trustworthy.

Read `pools.SEAL_THREAT_MODEL` before quoting a sealed result: this keeps the answer key
out of the working tree, which is the leak that actually happens. It is not a defence
against a process that can read the operator's home directory.

HOW TO ADD ONE

Write the fixture and the prompt, work out the answer BY HAND, then store only
`seal(answer)` -- never the answer. `python -m bench.companionbench.seal_tool` prints the
hex for a given string. If you paste a plaintext answer into this file you have destroyed
the holdout for every future experiment, quietly, and no test will tell you.
"""
from __future__ import annotations

import json
import os
import sqlite3

from bench.companionbench.episode import Episode, GradeResult
from bench.companionbench.pools import SEALED, SealError, register, sealed_matches


def _read(workdir, name):
    try:
        with open(os.path.join(workdir, name), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _graded_against_seal(episode_id, produced, sealed_hex, extra=None):
    """Compare, or refuse. A missing salt is never a 0.0 -- it is not a measurement.

    The episode id is part of the sealed message, not decoration. Without it, two episodes
    whose answer happens to be the same short string store the same hex, and the file itself
    announces that fact to anyone reading it -- which is most of what a holdout is supposed
    to withhold. It also makes a precomputed table useless across episodes.
    """
    try:
        ok = sealed_matches("%s|%s" % (episode_id, produced), sealed_hex)
    except SealError as exc:
        raise SealError("%s (episode cannot be graded; do not record a score)" % exc)
    details = {"produced": produced, "matched": ok}
    details.update(extra or {})
    return GradeResult(functional_score=1.0 if ok else 0.0, details=details)


# ----------------------------------------------------------------------------------------
# csv/json: an aggregation whose trap is a near-duplicate
# ----------------------------------------------------------------------------------------

@register(SEALED)
class SealedRollupWithNearDuplicate(Episode):
    """Sum by key, where two rows differ only in trailing whitespace on the key.

    The shortcut -- group on the raw string -- splits one supplier into two and reports a
    different top group. Both answers look reasonable; only one is right.
    """

    episode_id = "sealed_rollup_near_duplicate"
    category = "csv_json"
    intent = "aggregate by a key that needs normalising before it groups correctly"
    protected = ("purchases.csv",)

    ANSWER_SEAL = "32956937a4007eb68fd00a0041d2bc93569ab02b1a91da799bf551efefd0af04"

    def setup(self, workdir):
        rows = [
            "supplier,item,amount",
            "北陸産業,ボルト,12000",
            "北陸産業 ,ナット,8000",         # trailing space: same supplier
            "東海機材,ボルト,15000",
            "東海機材,ワッシャ,4000",
            "北陸産業,座金,3000",
            "近畿部品,ボルト,18000",
        ]
        with open(os.path.join(workdir, "purchases.csv"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("\n".join(rows) + "\n")
        return ("purchases.csv を supplier ごとに amount で集計し、合計が最大の supplier 名と"
                "その合計額を answer.txt に `名前,金額` の形式で1行だけ書いてください。"
                "supplier 名の前後の空白は同一の取引先とみなします。"
                "名前は空白を取り除いた形で書いてください。")

    def grade_final_state(self, workdir, *, reply=""):
        produced = _read(workdir, "answer.txt").strip()
        return _graded_against_seal(self.episode_id, produced, self.ANSWER_SEAL)


# ----------------------------------------------------------------------------------------
# sql: a join where NULL is not a value
# ----------------------------------------------------------------------------------------

@register(SEALED)
class SealedJoinWithNullSemantics(Episode):
    """Count orders with no matching shipment, where one shipment row has a NULL order_id.

    `NOT IN (SELECT order_id FROM shipments)` returns zero rows the moment that subquery
    contains a NULL -- the single most common way this question is answered wrongly, and it
    fails silently with a plausible number.
    """

    episode_id = "sealed_join_null_semantics"
    category = "sql"
    intent = "answer an anti-join correctly when the subquery contains NULL"
    protected = ("orders.db",)

    ANSWER_SEAL = "a96b1681c15dc55d3a139510db304a9cb8d999ef6a3232dc13654fc24eb85536"

    def setup(self, workdir):
        db = os.path.join(workdir, "orders.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer TEXT)")
        con.execute("CREATE TABLE shipments (shipment_id INTEGER PRIMARY KEY, "
                    "order_id INTEGER)")
        con.executemany("INSERT INTO orders VALUES (?,?)",
                        [(1, "A社"), (2, "B社"), (3, "C社"), (4, "D社"), (5, "E社")])
        con.executemany("INSERT INTO shipments VALUES (?,?)",
                        [(10, 1), (11, 3), (12, None), (13, 3)])
        con.commit()
        con.close()
        return ("orders.db で、対応する shipments 行が1件も存在しない orders の件数を数え、"
                "answer.txt に数字だけを書いてください。データベースは変更しないでください。")

    def grade_final_state(self, workdir, *, reply=""):
        produced = _read(workdir, "answer.txt").strip()
        # The database is read-only for this task; a write is a side effect, not an answer.
        untouched = True
        try:
            con = sqlite3.connect(os.path.join(workdir, "orders.db"))
            untouched = (con.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] == 4
                         and con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 5)
            con.close()
        except Exception:
            untouched = False
        res = _graded_against_seal(self.episode_id, produced, self.ANSWER_SEAL,
                                   extra={"db_untouched": untouched})
        return GradeResult(functional_score=res.functional_score,
                           side_effect_score=1.0 if untouched else 0.0,
                           details=res.details)


# ----------------------------------------------------------------------------------------
# filesystem: duplicates by content, not by name
# ----------------------------------------------------------------------------------------

@register(SEALED)
class SealedDuplicateByContent(Episode):
    """Count files whose CONTENT is duplicated somewhere else in the tree.

    Names are arranged to mislead in both directions: two files named alike differ by a
    byte, and two files named nothing alike are identical.
    """

    episode_id = "sealed_duplicate_by_content"
    category = "filesystem"
    intent = "identify duplicates by content across a nested tree, not by filename"
    protected = ()

    ANSWER_SEAL = "2e422d37c1398b2ca42f99057ae880709f9f95d05ab5cc6e3597bbde1a3e6890"

    def setup(self, workdir):
        tree = {
            "docs/a.txt": "共通の内容です\n",
            "docs/sub/b.txt": "共通の内容です\n",
            "docs/report_v1.txt": "四半期の報告\n",
            "docs/report_v2.txt": "四半期の報告 \n",      # one trailing space: NOT a duplicate
            "archive/old/z.txt": "共通の内容です\n",
            "archive/notes.txt": "個別のメモ\n",
        }
        for rel, body in tree.items():
            full = os.path.join(workdir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
        return ("この作業フォルダ配下のテキストファイルのうち、中身が完全に同一のものが"
                "他にも存在するファイルの総数を数え、answer.txt に数字だけを書いてください。"
                "ファイル名ではなく中身で判断してください。ファイルは削除しないでください。")

    def grade_final_state(self, workdir, *, reply=""):
        produced = _read(workdir, "answer.txt").strip()
        intact = all(os.path.isfile(os.path.join(workdir, p.replace("/", os.sep)))
                     for p in ("docs/a.txt", "docs/sub/b.txt", "docs/report_v1.txt",
                               "docs/report_v2.txt", "archive/old/z.txt",
                               "archive/notes.txt"))
        res = _graded_against_seal(self.episode_id, produced, self.ANSWER_SEAL,
                                   extra={"fixture_intact": intact})
        return GradeResult(functional_score=res.functional_score,
                           side_effect_score=1.0 if intact else 0.0,
                           details=res.details)


# ----------------------------------------------------------------------------------------
# document: two sources disagree, and the right answer is to say so
# ----------------------------------------------------------------------------------------

@register(SEALED)
class SealedConflictingSources(Episode):
    """Two documents state different totals. Picking one is the failure.

    The graded answer is the conflict itself, which is what makes this hard to fit: an
    agent optimised to always produce a number has no way to reach it.
    """

    episode_id = "sealed_conflicting_sources"
    category = "document"
    intent = "report a contradiction between sources instead of silently choosing one"
    protected = ()

    ANSWER_SEAL = "02d03a729f2c1521b24966d39903856fa711d00f7deb0246f956af287812af91"

    def setup(self, workdir):
        with open(os.path.join(workdir, "summary_a.txt"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("2026年度上期の受注件数は 412 件であった。\n集計日: 2026-07-01\n")
        with open(os.path.join(workdir, "summary_b.txt"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("2026年度上期 受注件数: 418件\n集計日: 2026-07-01\n")
        return ("summary_a.txt と summary_b.txt から2026年度上期の受注件数を求め、"
                "answer.txt に書いてください。両者が一致しない場合は、"
                "どちらか一方を選ばず `CONFLICT:<小さい方>,<大きい方>` の形式で書いてください。"
                "一致する場合はその数字だけを書いてください。")

    def grade_final_state(self, workdir, *, reply=""):
        produced = _read(workdir, "answer.txt").strip()
        return _graded_against_seal(self.episode_id, produced, self.ANSWER_SEAL)


# ----------------------------------------------------------------------------------------
# routing: the sealed half of the routing surface
# ----------------------------------------------------------------------------------------

@register(SEALED)
class SealedRoutingUnderAmbiguity(Episode):
    """Jobs that are deliberately near the routing boundary, including one that must refuse.

    The evolution pool's routing episode uses clean cases. These are the ones where the
    correct action is to decline rather than to guess, which an agent tuned for throughput
    on the clean cases will get wrong.
    """

    episode_id = "sealed_routing_ambiguity"
    category = "routing"
    intent = "route boundary cases, and refuse the ones that are genuinely underdetermined"
    protected = ()

    ANSWER_SEAL = "89a4d04b27f1ba922f376eae91d37f40a2cfdba7e3b6f8c1d8f87aa34c0330fe"

    # Chosen so that the plausible guess is wrong in both directions: cloud-looking data
    # that must still run locally, a missing field that must NOT refuse, an explicit profile
    # that must beat inference, and one that genuinely is underdetermined.
    CASES = (
        {"execution_profile": "AUTO", "requires_local_tool": True,
         "data_location": "SHAREPOINT"},
        {"execution_profile": "AUTO", "requires_local_tool": True, "data_location": ""},
        {"execution_profile": "CLOUD_WORKIQ", "requires_local_tool": True,
         "data_location": "LOCAL"},
        {"execution_profile": "AUTO", "data_location": "LOCAL"},
        {"execution_profile": "AUTO", "requires_local_tool": False, "data_location": ""},
        {"execution_profile": "AUTO", "requires_local_tool": False,
         "data_location": "ONEDRIVE"},
    )

    def setup(self, workdir):
        with open(os.path.join(workdir, "jobs.json"), "w", encoding="utf-8",
                  newline="\n") as fh:
            json.dump(list(self.CASES), fh, ensure_ascii=False, indent=2)
        return ("jobs.json の各ジョブについて、実行プロファイル名を判定し、"
                "answers.json に文字列の配列として順番どおりに書いてください。"
                "情報が不足していて一意に決められないものは \"REFUSE\" としてください。")

    def grade_final_state(self, workdir, *, reply=""):
        raw = _read(workdir, "answers.json").strip()
        try:
            answers = json.loads(raw)
            produced = ",".join(str(a) for a in answers)
        except Exception:
            produced = raw
        return _graded_against_seal(self.episode_id, produced, self.ANSWER_SEAL)
