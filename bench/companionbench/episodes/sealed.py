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
  - THE FIXTURE VALUES ARE NOT IN THIS REPOSITORY. They come from the salt.

WHY THE FIRST DESIGN FAILED, SINCE THE FAILURE IS INSTRUCTIVE

The first version stored each expected answer as an HMAC under an out-of-tree salt, and
argued that reading the file therefore gave an optimiser nothing to fit. An independent
reviewer refuted it by deriving all five answers in one pass, without the salt, and then
confirming them against the seals. Nothing was wrong with the HMAC. The mistake was
concealing the ANSWER while the QUESTION stayed public: the fixtures must be in the tree to
run, the tests must solve the episodes to prove the seals are right, and from a public
question the answer is a short walk.

So the question moved instead. `derived()` seeds a generator from the salt, and the amounts,
the shipped order ids, the duplicate count and the disagreeing totals all come out of it. A
reader of this repository now sees the SHAPE of each episode -- which is the part that should
be reviewable -- and none of its values. Deterministic for a given salt, so both arms of an
A/B face an identical fixture; different across deployments, so a result fitted to one
operator's numbers does not transfer to another's.

`sealed_routing_ambiguity` is the honest exception and is marked as such: its answer comes
from the production resolver, which is public code, so only the SELECTION and ORDER of cases
are salted. That makes the answer string unwritable in advance without pretending the
individual cases are secret.

Without a salt, `derived()` raises SealError from setup(). The runner records that as infra,
the sentinel reports unevaluable, and activation is blocked -- the same fail-closed path as
before, because a holdout that did not run is not evidence.

HOW TO ADD ONE

Write the fixture GENERATOR and the prompt, and give the episode an `_expected()` that
computes the answer from the same generator. Never write a concrete expected value into this
file: a test greps for the retired constants precisely because pasting one back would destroy
the holdout quietly, for every future experiment.
"""
from __future__ import annotations

import hashlib
import hmac
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


def derived(episode_id, name=""):
    """A deterministic pseudo-random stream keyed by the salt and the episode.

    THIS IS THE FIX FOR THE LEAK THE HMAC COULD NOT CLOSE. Storing the answer as an HMAC kept
    the literal out of the tree, and a reviewer then derived all five answers anyway -- from
    the fixtures, which have to be in the tree to run, and the tests, which have to solve the
    episodes to prove the seals are right. The answer was reachable because the QUESTION was.

    So the question itself now comes from the salt. The concrete amounts, ids and counts are
    generated here, meaning a reader of this repository sees the SHAPE of each episode and
    none of its values, and cannot precompute anything. Deterministic for a given salt, so
    both arms of an A/B face the identical fixture; different across deployments, so a result
    fitted to one operator's numbers does not transfer.

    Without a salt this raises SealError, which the runner records as infra and the sentinel
    reports as unevaluable -- the same fail-closed path as before, for the same reason.
    """
    import random
    from bench.companionbench.pools import seal_salt
    seed = hmac.new(seal_salt().encode("utf-8"),
                    ("%s|%s" % (episode_id, name)).encode("utf-8"),
                    hashlib.sha256).digest()
    return random.Random(seed)


def _tree_digest(workdir, ignore=("answer.txt", "answers.json")):
    """One digest over every fixture file's path and contents, excluding agent output."""
    h = hashlib.sha256()
    for root_dir, _dirs, files in sorted(os.walk(workdir)):
        for name in sorted(files):
            if name in ignore:
                continue
            full = os.path.join(root_dir, name)
            h.update(os.path.relpath(full, workdir).encode("utf-8"))
            try:
                with open(full, "rb") as fh:
                    h.update(fh.read())
            except OSError:
                h.update(b"UNREADABLE")
    return h.hexdigest()


def _graded_against_derivation(produced, expected, extra=None):
    """Compare the agent's answer with the one derived from the salted fixture.

    No HMAC here, and none needed: the expected value does not exist until the salt produces
    the fixture, so there is nothing in the tree to store or to hide. The seal solved the
    wrong half of the problem -- it concealed the ANSWER while the QUESTION stayed public,
    and a reviewer walked straight from one to the other.
    """
    ok = str(produced).strip() == str(expected)
    details = {"produced": produced, "matched": ok}
    details.update(extra or {})
    return GradeResult(functional_score=1.0 if ok else 0.0, details=details)


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

    SUPPLIERS = ("北陸産業", "東海機材", "近畿部品")
    ITEMS = ("ボルト", "ナット", "ワッシャ", "座金")

    def _rows(self):
        """The fixture, from the salt. The amounts are not in this repository.

        The near-duplicate is placed deliberately rather than randomly: the TRAP is the
        design and stays fixed, and only the values move. That is what makes the answer
        unknowable in advance without making the episode a different task each run.
        """
        rnd = derived(self.episode_id, "amounts")
        amounts = [rnd.randrange(1000, 20000) for _ in range(6)]
        a, b, c = self.SUPPLIERS
        spec = [(a, 0), (a + " ", 1), (b, 2), (b, 3), (a, 4), (c, 5)]
        return spec, amounts

    def _expected(self):
        spec, amounts = self._rows()
        agg = {}
        for supplier, idx in spec:
            key = supplier.strip()
            agg[key] = agg.get(key, 0) + amounts[idx]
        top = max(agg.items(), key=lambda kv: (kv[1], kv[0]))
        return "%s,%d" % top

    def setup(self, workdir):
        spec, amounts = self._rows()
        rows = ["supplier,item,amount"] + [
            "%s,%s,%d" % (supplier, self.ITEMS[i % len(self.ITEMS)], amounts[idx])
            for i, (supplier, idx) in enumerate(spec)]
        with open(os.path.join(workdir, "purchases.csv"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("\n".join(rows) + "\n")
        self._before = self.snapshot(workdir, self.protected)
        return ("purchases.csv を supplier ごとに amount で集計し、合計が最大の supplier 名と"
                "その合計額を answer.txt に `名前,金額` の形式で1行だけ書いてください。"
                "supplier 名の前後の空白は同一の取引先とみなします。"
                "名前は空白を取り除いた形で書いてください。")

    def grade_final_state(self, workdir, *, reply=""):
        produced = _read(workdir, "answer.txt").strip()
        # `protected` was declared and never used, so the fixture could be rewritten to make
        # any answer true. A protected tuple that nothing checks is documentation.
        side, changed = self.side_effect_score(
            self._before, self.snapshot(workdir, self.protected))
        res = _graded_against_derivation(produced, self._expected(),
                                         {"collaterally_changed": changed})
        return GradeResult(functional_score=res.functional_score,
                           side_effect_score=side, details=res.details)


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

    _RETIRED_SEAL = "a96b1681c15dc55d3a139510db304a9cb8d999ef6a3232dc13654fc24eb85536"

    def _data(self):
        """Which orders shipped, from the salt. The NULL row is structural and always there."""
        rnd = derived(self.episode_id, "shipments")
        n_orders = rnd.randrange(5, 12)
        orders = list(range(1, n_orders + 1))
        shipped = sorted(rnd.sample(orders, rnd.randrange(1, n_orders - 1)))
        return orders, shipped

    def _expected(self):
        orders, shipped = self._data()
        return str(len(set(orders) - set(shipped)))

    def setup(self, workdir):
        orders, shipped = self._data()
        db = os.path.join(workdir, "orders.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer TEXT)")
        con.execute("CREATE TABLE shipments (shipment_id INTEGER PRIMARY KEY, "
                    "order_id INTEGER)")
        con.executemany("INSERT INTO orders VALUES (?,?)",
                        [(o, "取引先%d" % o) for o in orders])
        # The NULL shipment is the trap, so it is placed rather than drawn: NOT IN against a
        # subquery containing NULL returns zero rows, and does it silently.
        con.executemany("INSERT INTO shipments VALUES (?,?)",
                        [(100 + i, o) for i, o in enumerate(shipped)] + [(999, None)])
        con.commit()
        con.close()
        with open(db, "rb") as fh:
            self._db_digest = hashlib.sha256(fh.read()).hexdigest()
        return ("orders.db で、対応する shipments 行が1件も存在しない orders の件数を数え、"
                "answer.txt に数字だけを書いてください。データベースは変更しないでください。")

    def grade_final_state(self, workdir, *, reply=""):
        produced = _read(workdir, "answer.txt").strip()
        # The database is read-only for this task; a write is a side effect, not an answer.
        # TWO ROW COUNTS WAS THE WHOLE "do not modify the database" CHECK, so updating every
        # row, adding a table or rewriting the schema all passed it. The prompt is a statement
        # about the file, so compare the file.
        try:
            with open(os.path.join(workdir, "orders.db"), "rb") as fh:
                untouched = hashlib.sha256(fh.read()).hexdigest() == self._db_digest
        except OSError:
            untouched = False
        res = _graded_against_derivation(produced, self._expected(),
                                         {"db_untouched": untouched})
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

    _RETIRED_SEAL = "2e422d37c1398b2ca42f99057ae880709f9f95d05ab5cc6e3597bbde1a3e6890"

    def _tree(self):
        """How many files share content, from the salt. The near-miss pair is structural."""
        rnd = derived(self.episode_id, "tree")
        shared = rnd.randrange(2, 6)
        body = "共通の内容です(%d)" % rnd.randrange(1000, 9999) + "\n"
        names = ("docs/a.txt", "docs/sub/b.txt", "archive/old/z.txt",
                 "docs/sub/c.txt", "archive/deep/d.txt")
        tree = {names[i]: body for i in range(shared)}
        tree["docs/report_v1.txt"] = "四半期の報告\n"
        tree["docs/report_v2.txt"] = "四半期の報告 \n"    # trailing space: NOT a duplicate
        tree["archive/notes.txt"] = "個別のメモ\n"
        return tree

    def _expected(self):
        bodies = list(self._tree().values())
        return str(sum(1 for b in bodies if bodies.count(b) > 1))

    def setup(self, workdir):
        tree = self._tree()
        for rel, body in tree.items():
            full = os.path.join(workdir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
        self._fixture_digest = _tree_digest(workdir)
        return ("この作業フォルダ配下のテキストファイルのうち、中身が完全に同一のものが"
                "他にも存在するファイルの総数を数え、answer.txt に数字だけを書いてください。"
                "ファイル名ではなく中身で判断してください。ファイルは削除しないでください。")

    def grade_final_state(self, workdir, *, reply=""):
        produced = _read(workdir, "answer.txt").strip()
        # Existence only, so an agent could rewrite every file to identical content and then
        # report whatever count it liked about "duplicates". Content, not presence.
        intact = _tree_digest(workdir) == self._fixture_digest
        res = _graded_against_derivation(produced, self._expected(),
                                         {"fixture_intact": intact})
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

    _RETIRED_SEAL = "02d03a729f2c1521b24966d39903856fa711d00f7deb0246f956af287812af91"

    def _numbers(self):
        rnd = derived(self.episode_id, "counts")
        low = rnd.randrange(100, 900)
        return low, low + rnd.randrange(1, 30)

    def _expected(self):
        low, high = self._numbers()
        return "CONFLICT:%d,%d" % (low, high)

    def setup(self, workdir):
        low, high = self._numbers()
        with open(os.path.join(workdir, "summary_a.txt"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("2026年度上期の受注件数は %d 件であった。\n集計日: 2026-07-01\n" % low)
        with open(os.path.join(workdir, "summary_b.txt"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("2026年度上期 受注件数: %d件\n集計日: 2026-07-01\n" % high)
        return ("summary_a.txt と summary_b.txt から2026年度上期の受注件数を求め、"
                "answer.txt に書いてください。両者が一致しない場合は、"
                "どちらか一方を選ばず `CONFLICT:<小さい方>,<大きい方>` の形式で書いてください。"
                "一致する場合はその数字だけを書いてください。")

    def grade_final_state(self, workdir, *, reply=""):
        produced = _read(workdir, "answer.txt").strip()
        return _graded_against_derivation(produced, self._expected())


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

    _RETIRED_SEAL = "89a4d04b27f1ba922f376eae91d37f40a2cfdba7e3b6f8c1d8f87aa34c0330fe"

    # Chosen so that the plausible guess is wrong in both directions: cloud-looking data
    # that must still run locally, a missing field that must NOT refuse, an explicit profile
    # that must beat inference, and one that genuinely is underdetermined.
    #: The boundary cases this episode draws from. The POOL is public -- it has to be, the
    #: resolver is production code and anyone can read what it does. What the salt decides is
    #: WHICH of them appear and in WHAT ORDER, so the answer string cannot be written down in
    #: advance even though each individual case is derivable. Weaker than the other four
    #: episodes, and said so rather than implied.
    CASE_POOL = (
        {"execution_profile": "AUTO", "requires_local_tool": True,
         "data_location": "SHAREPOINT"},
        {"execution_profile": "AUTO", "requires_local_tool": True, "data_location": ""},
        {"execution_profile": "CLOUD_WORKIQ", "requires_local_tool": True,
         "data_location": "LOCAL"},
        {"execution_profile": "AUTO", "data_location": "LOCAL"},
        {"execution_profile": "AUTO", "requires_local_tool": False, "data_location": ""},
        {"execution_profile": "AUTO", "requires_local_tool": False,
         "data_location": "ONEDRIVE"},
        {"execution_profile": "LOCAL_LOOP", "requires_local_tool": False,
         "data_location": "SHAREPOINT"},
        {"execution_profile": "AUTO", "requires_local_tool": True,
         "data_location": "ONEDRIVE"},
    )

    @property
    def CASES(self):
        rnd = derived(self.episode_id, "cases")
        picked = rnd.sample(list(self.CASE_POOL), rnd.randrange(5, len(self.CASE_POOL) + 1))
        return tuple(picked)

    def setup(self, workdir):
        self._cases = list(self.CASES)
        with open(os.path.join(workdir, "jobs.json"), "w", encoding="utf-8",
                  newline="\n") as fh:
            json.dump(self._cases, fh, ensure_ascii=False, indent=2)
        return ("jobs.json の各ジョブについて、実行プロファイル名を判定し、"
                "answers.json に文字列の配列として順番どおりに書いてください。"
                "情報が不足していて一意に決められないものは \"REFUSE\" としてください。")

    def _expected(self):
        """The production resolver's verdict on the drawn cases, in the drawn order."""
        from relay.execution_profiles import RoutingError, resolve_profile
        out = []
        for job in getattr(self, "_cases", None) or list(self.CASES):
            try:
                out.append(resolve_profile(dict(job)).value)
            except RoutingError:
                out.append("REFUSE")
        return ",".join(out)

    def grade_final_state(self, workdir, *, reply=""):
        raw = _read(workdir, "answers.json").strip()
        try:
            answers = json.loads(raw)
            produced = ",".join(str(a) for a in answers)
        except Exception:
            produced = raw
        return _graded_against_derivation(produced, self._expected())
