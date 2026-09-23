# -*- coding: utf-8 -*-
"""What an ordinary office user will throw at the Skill store without meaning to.

Japanese and emoji text, folders with spaces, SKILL.md saved by Notepad (BOM, CRLF, or
Shift_JIS), a very large procedure, a half-filled frontmatter, a library of 500 Skills, and two
things reading the library at the same moment. Each case either works, or is refused with a
reason a person can act on -- never silently dropped. All state lives under tmp_path.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from relay.skills import MAX_FILE_BYTES, MAX_FILES, SkillError, SkillStore, load_bundle

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skills_business"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_SKILLS_INCLUDE_PERSONAL", raising=False)
    monkeypatch.setenv("MCP_SKILLS_STATE_DB", str(tmp_path / "state" / "skills.sqlite3"))
    monkeypatch.setenv("MCP_SKILLS_GATE_DIR", str(tmp_path / "gates"))
    return tmp_path


def _store(env, proj=None):
    proj = proj or env / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    return SkillStore(proj, db_path=env / "state" / "skills.sqlite3", gate_dir=env / "gates")


def _approve(store, name):
    review = store.request_approval(name)
    store.confirm_approval(name, review["token"])


def _write_skill(root, folder, raw: bytes):
    d = root / "skills" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_bytes(raw)
    return d


GOOD = ("---\n"
        "name: stationery-order\n"
        "description: \"文房具の発注手順。コピー用紙・トナー・ボールペンの在庫確認と注文\"\n"
        "when_to_use: \"コピー用紙 トナー 文房具 の補充、発注\"\n"
        "---\n\n"
        "# 文房具の発注\n\n1. 在庫棚を確認する\n2. $ARGUMENTS を発注システムで注文する\n")


# ----------------------------------------------------------------- paths, names and characters

def test_a_project_under_a_japanese_folder_with_spaces_works_end_to_end(env):
    proj = env / "共有 フォルダ (営業部)" / "業務 手順"
    store = _store(env, proj)
    src = env / "受け取り 用" / "expense-reimbursement"
    shutil.copytree(FIXTURES / "expense-reimbursement", src)
    shutil.copy(src / "references" / "tricky-cases.md",
                src / "references" / "勘定科目の迷いやすい例.md")
    store.import_external(src)
    _approve(store, "expense-reimbursement")
    assert store.match("接待で使ったお金の経費申請ってどうやるの")["name"] == "expense-reimbursement"
    assert "対象: 9月分" in store.render("expense-reimbursement", "9月分")
    assert "日当" in store.read_resource("expense-reimbursement",
                                         "references/勘定科目の迷いやすい例.md")


def test_emoji_in_description_and_body_round_trips(env):
    store = _store(env)
    store.create_local("kpi-dashboard", "📊 月次KPIダッシュボードの更新 ✅",
                       "## 手順 🚀\n1. 数字を更新する 📈\n2. 共有する 🙌")
    rendered = store.render("kpi-dashboard")
    assert "📈" in rendered and "🙌" in rendered
    assert store.get("kpi-dashboard").description == "📊 月次KPIダッシュボードの更新 ✅"


def test_a_japanese_skill_name_is_refused_with_the_rule_and_listed_as_invalid(env):
    store = _store(env)
    raw = GOOD.replace("name: stationery-order", "name: 文房具の発注").encode("utf-8")
    _write_skill(store.project_root, "文房具の発注", raw)
    assert store.discover() == []
    reason = store.invalid_bundles()["文房具の発注"]
    assert "lowercase letters, digits, and hyphens" in reason


# ------------------------------------------------------------ what Notepad and Excel produce

@pytest.mark.parametrize("variant", ["lf", "crlf", "bom", "bom+crlf", "cr"])
def test_line_endings_and_bom_do_not_matter(env, variant):
    text = GOOD
    if "crlf" in variant:
        text = text.replace("\n", "\r\n")
    elif variant == "cr":
        text = text.replace("\n", "\r")
    raw = text.encode("utf-8")
    if "bom" in variant:
        raw = b"\xef\xbb\xbf" + raw
    store = _store(env)
    _write_skill(store.project_root, "stationery-order", raw)
    skill = store.get("stationery-order")
    assert skill.description.startswith("文房具の発注手順")
    assert "\r" not in skill.body
    _approve(store, "stationery-order")
    assert "トナー を発注システムで注文する" in store.render("stationery-order", "トナー")
    assert store.match("コピー用紙とトナーを発注したい")["name"] == "stationery-order"


def test_a_shift_jis_skill_md_is_refused_and_says_utf8(env):
    store = _store(env)
    _write_skill(store.project_root, "stationery-order", GOOD.encode("cp932"))
    assert store.discover() == []
    assert "UTF-8" in store.invalid_bundles()["stationery-order"]


def test_a_very_large_procedure_loads_and_the_review_preview_is_bounded(env):
    store = _store(env)
    line = "- 手順の詳細説明。この行は大きな手順書を模したものです。\n"
    body = line * ((MAX_FILE_BYTES - 4096) // len(line.encode("utf-8")))
    raw = GOOD.encode("utf-8") + body.encode("utf-8")
    assert MAX_FILE_BYTES - 8192 < len(raw) <= MAX_FILE_BYTES
    _write_skill(store.project_root, "stationery-order", raw)
    t0 = time.perf_counter()
    review = store.request_approval("stationery-order")
    elapsed = time.perf_counter() - t0
    assert review["instruction_preview_truncated"] is True
    assert len(review["instruction_preview"]) == 8000
    store.confirm_approval("stationery-order", review["token"])
    rendered = store.render("stationery-order")
    assert len(rendered) > 600_000   # the whole body is handed over, not the preview
    print("\n[perf] request_approval on a %d-byte SKILL.md: %.3f s" % (len(raw), elapsed))
    assert elapsed < 10


def test_a_skill_md_over_the_size_limit_is_refused_with_the_limit(env):
    store = _store(env)
    raw = GOOD.encode("utf-8") + b"x" * (MAX_FILE_BYTES + 1)
    _write_skill(store.project_root, "stationery-order", raw)
    assert store.discover() == []
    assert "exceeds %d bytes" % MAX_FILE_BYTES in store.invalid_bundles()["stationery-order"]


def test_too_many_files_is_refused(env):
    store = _store(env)
    d = _write_skill(store.project_root, "stationery-order", GOOD.encode("utf-8"))
    (d / "assets").mkdir()
    for i in range(MAX_FILES):
        (d / "assets" / ("form_%03d.txt" % i)).write_text("x", encoding="utf-8")
    assert store.discover() == []
    assert "exceeds %d files" % MAX_FILES in store.invalid_bundles()["stationery-order"]


def test_too_many_total_bytes_is_refused(env):
    store = _store(env)
    d = _write_skill(store.project_root, "stationery-order", GOOD.encode("utf-8"))
    (d / "assets").mkdir()
    for i in range(3):
        (d / "assets" / ("scan_%d.pdf" % i)).write_bytes(b"%PDF" + b"0" * (MAX_FILE_BYTES - 10))
    assert store.discover() == []
    assert "Skill bundle exceeds" in store.invalid_bundles()["stationery-order"]


@pytest.mark.parametrize("raw,needle", [
    ("---\nname: stationery-order\ndescription: \"x\"\n---\n\n", "body is empty"),
    ("---\nname: stationery-order\ndescription: \"x\"\n---\n   \n\t\n", "body is empty"),
    ("---\nname: stationery-order\n---\n\n# 手順\n", "description is required"),
    ("---\nname: stationery-order\ndescription: \"\"\n---\n\n# 手順\n", "description is required"),
    ("---\nname: stationery-order\ndescription: 注意: 締め日は25日\n---\n\n# 手順\n", "YAML"),
    ("---\n- just\n- a list\n---\n\n# 手順\n", "must be a mapping"),
    ("---\nname: stationery-order\ndescription: \"x\"\n\n# 手順\n", "closing delimiter"),
    ("# 手順だけ書いた\n\n1. 注文する\n", "must start with YAML frontmatter"),
    ("---\nname: stationery-order\ndescription: \"%s\"\n---\n\n# 手順\n" % ("長" * 1025),
     "exceeds 1024"),
])
def test_half_written_skill_md_is_refused_with_a_reason(env, raw, needle):
    store = _store(env)
    _write_skill(store.project_root, "stationery-order", raw.encode("utf-8"))
    assert store.discover() == []
    assert needle in store.invalid_bundles()["stationery-order"]
    with pytest.raises(SkillError, match="could not be loaded"):
        store.get("stationery-order")


def test_a_missing_name_falls_back_to_the_folder_name(env):
    store = _store(env)
    raw = GOOD.replace("name: stationery-order\n", "").encode("utf-8")
    _write_skill(store.project_root, "stationery-order", raw)
    assert [s.name for s in store.discover()] == ["stationery-order"]


def test_extra_frontmatter_fields_are_kept_not_rejected(env):
    store = _store(env)
    raw = GOOD.replace("---\n\n#", "owner: 総務課\nversion: 3\ntags: [発注, 総務]\n---\n\n#", 1)
    _write_skill(store.project_root, "stationery-order", raw.encode("utf-8"))
    skill = store.get("stationery-order")
    assert skill.metadata["owner"] == "総務課" and skill.metadata["tags"] == ["発注", "総務"]


# ----------------------------------------------------------------------------- 500 Skills

DEPTS = ["総務部", "人事部", "経理部", "営業部", "製造部", "品質保証部", "物流部",
         "情報システム部", "法務部", "購買部", "技術部", "広報部"]
THINGS = ["年次計画", "備品台帳", "教育記録", "安全点検", "設備保全", "顧客台帳", "価格表",
          "作業標準書", "社内アンケート", "防災訓練", "名刺発注", "車両管理", "郵便物",
          "鍵管理", "廃棄物処理", "省エネ報告", "衛生委員会", "社用携帯", "在宅勤務", "採用面接"]
HOW = ["の作り方", "の更新手順", "の確認方法", "の提出手順", "の保管ルール"]

#: Requests the 18-Skill library matched correctly (tests/test_skills_business_matching.py),
#: re-asked of the same 18 Skills buried among 482 others.
KNOWN_GOOD = [
    ("接待で使ったお金の経費申請ってどうやるの", "expense-reimbursement"),
    ("打ち合わせのメモから決定事項とToDoを抜き出して", "meeting-minutes-summary"),
    ("今月の売上をエクセルで集計したい", "monthly-sales-excel"),
    ("苦情を受けたときの初動対応を知りたいです", "customer-complaint-first-response"),
    ("新幹線とホテルを取る前に何が必要？出張の承認とか", "business-trip-request"),
    ("請求書の金額が発注書と合っているかチェックしたい", "invoice-check"),
    ("明日の14時から会議室を予約したい", "meeting-room-booking"),
    ("月末の棚卸をやるので手順を知りたい", "inventory-stocktake"),
    ("業務委託契約書のレビューをお願いしたいです", "contract-review-points"),
    ("昨日のサーバ停止の障害報告書を書きたい", "incident-report"),
]


@pytest.fixture(scope="module")
def big_library(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("biz500")
    mp.delenv("MCP_SKILLS_INCLUDE_PERSONAL", raising=False)
    mp.setenv("MCP_SKILLS_STATE_DB", str(tmp / "state.sqlite3"))
    mp.setenv("MCP_SKILLS_GATE_DIR", str(tmp / "gates"))
    try:
        proj = tmp / "proj"
        (proj / "skills").mkdir(parents=True)
        store = SkillStore(proj, db_path=tmp / "state.sqlite3", gate_dir=tmp / "gates")
        business = sorted(p.name for p in FIXTURES.iterdir() if (p / "SKILL.md").is_file())
        for name in business:
            shutil.copytree(FIXTURES / name, proj / "skills" / name)
            _approve(store, name)
        combos = [(d, t, h) for t in THINGS for d in DEPTS for h in HOW]
        t0 = time.perf_counter()
        for i in range(500 - len(business)):
            d, t, h = combos[(i * 7) % len(combos)]
            store.create_local("proc-%03d" % i, "%s: %s%s" % (d, t, h),
                               "# %s%s\n\n1. %sの担当者に確認する\n" % (t, h, d))
        create_s = time.perf_counter() - t0
        yield {"store": store, "tmp": tmp, "create_s": create_s,
               "n_created": 500 - len(business)}
    finally:
        mp.undo()


def test_500_skills_discover_list_and_match_latency(big_library):
    """Every tool call builds a NEW SkillStore and runs discover() at least once, so this is
    measured the way a tool call pays it: a fresh store per operation.

    BEFORE THE BUNDLE CACHE, MEASURED 2026-09-24 on the development machine (Windows, NTFS,
    antivirus on), fresh store each time: discover() of 500 Skills 19.6-20.2 s; list_metadata
    18.0-18.5 s; match() 17.5-19.5 s hit or miss; render() 18.7-19.7 s. cProfile of one
    match(): 11.6 s in load_bundle (7.9 s of it Path.resolve -> _getfinalpathname, 4.1 s
    stat), 7.7 s in _state_for (two queries on a fresh connection per Skill), 1.0 s YAML.

    AFTER (same machine, same library): the first discover() of a cold process 9.5 s -- it
    fills the cache; then discover 0.52 s, list_metadata 0.45 s, match 0.50-0.66 s, render
    0.40-0.62 s. What remains is one directory listing per bundle (os.scandir, ~0.5 ms each
    here), which is the price of noticing an edit without being told about it. Bundles written
    in the last 2 s are re-read every time until they age (see relay/skills._RACY_MARGIN_NS).

    The warm ceilings below are ~10x the measured values: a regression to re-reading every
    bundle (tens of seconds) fails them; runner noise does not.
    """
    store = big_library["store"]

    def fresh():
        return SkillStore(store.project_root, db_path=store.db_path, gate_dir=store.gate_dir)

    t0 = time.perf_counter()
    skills = fresh().discover()
    first_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    again = fresh().discover()
    discover_s = time.perf_counter() - t0
    assert again == skills
    t0 = time.perf_counter()
    rows = fresh().list_metadata(model_safe=True)
    list_s = time.perf_counter() - t0
    assert len(skills) == len(rows) == 500
    t0 = time.perf_counter()
    hit = fresh().match(KNOWN_GOOD[5][0])
    hit_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    none = fresh().match("今日の天気は？")
    miss_s = time.perf_counter() - t0
    assert none is None
    t0 = time.perf_counter()
    fresh().render("weekly-report-template", "山田太郎 2026-09-21")
    render_s = time.perf_counter() - t0
    print("\n[perf] 500 Skills: create_local x%d %.1f s (%.1f ms each); first discover %.2f s; "
          "discover %.2f s; list_metadata %.2f s; match (hit=%s) %.2f s; match (no hit) %.2f s; "
          "render %.2f s"
          % (big_library["n_created"], big_library["create_s"],
             1000 * big_library["create_s"] / big_library["n_created"], first_s, discover_s,
             list_s, (hit or {}).get("name"), hit_s, miss_s, render_s))
    assert first_s < 75
    assert discover_s < 5
    assert list_s < 5
    assert max(hit_s, miss_s) < 5
    assert render_s < 5


def test_500_skills_matching_quality_at_scale(big_library, monkeypatch):
    """The same known-good requests, with the 18 Skills buried among 482 others that share
    office vocabulary (部, 手順, 報告, 確認...). discover() is served from one snapshot so this
    measures the MATCHER at scale, separately from the latency above; match() itself is the
    unmodified code."""
    store = big_library["store"]
    snapshot = store.discover()
    monkeypatch.setattr(store, "discover", lambda: snapshot)
    results = [(q, e, (store.match(q) or {}).get("name")) for q, e in KNOWN_GOOD]
    hits = sum(1 for _q, e, got in results if got == e)
    wrong = [(q, e, got) for q, e, got in results if got is not None and got != e]
    fps = [q for q in ("今日の天気は？", "おすすめのランチの店を教えて", "PCのパスワードを忘れた",
                       "PowerPointのスライドの色を変えたい", "Wi-Fiにつながらない")
           if store.match(q) is not None]
    assert fps == []
    print("\n[quality@500] known-good still matched %d/%d; wrong %s; misses %s; FP %s"
          % (hits, len(KNOWN_GOOD), wrong,
             [q for q, e, got in results if got is None], fps))
    assert wrong == []
    assert hits >= FLOOR_KNOWN_GOOD_AT_500


#: MEASURED 2026-09-24: 10/10 still matched, no wrong Skill, no false positive.
FLOOR_KNOWN_GOOD_AT_500 = 9


def test_500_skills_approval_of_one_newcomer(big_library):
    store = big_library["store"]
    src = big_library["tmp"] / "incoming" / "visitor-desk"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: visitor-desk\ndescription: \"受付デスクの交代手順\"\n---\n\n# 交代\n1. 引き継ぐ\n",
        encoding="utf-8")
    t0 = time.perf_counter()
    store.import_external(src)
    review = store.request_approval("visitor-desk")
    store.confirm_approval("visitor-desk", review["token"])
    elapsed = time.perf_counter() - t0
    # MEASURED 2026-09-24 (development machine, fresh store per step): 42.8-46.1 s before the
    # bundle cache, 2.3-2.5 s after. The ceiling catches a return to re-reading everything.
    print("\n[perf] import+request+confirm in a 500-Skill library: %.2f s" % elapsed)
    assert store.get("visitor-desk").trust == "trusted"
    assert elapsed < 20


# ----------------------------------------------------------------------------- concurrency

def test_two_threads_discovering_while_an_approval_is_synced(env):
    proj = env / "proj"
    (proj / "skills").mkdir(parents=True)
    for name in ("invoice-check", "incident-report", "inventory-stocktake"):
        shutil.copytree(FIXTURES / name, proj / "skills" / name)
    store = _store(env)
    _approve(store, "incident-report")
    review = store.request_approval("invoice-check")
    payload = json.loads(Path(review["gate_path"]).read_text(encoding="utf-8"))
    payload.update({"answered": True, "answer": "approved", "answered_at": time.time()})
    Path(review["gate_path"]).write_text(json.dumps(payload), encoding="utf-8")

    errors, seen = [], []
    start = threading.Barrier(2)

    def reader():
        try:
            s = _store(env)
            start.wait()
            for _ in range(15):
                seen.append(tuple(sorted(x.name for x in s.discover())))
                s.match("昨日のサーバ停止の障害報告書を書きたい")
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == []
    assert set(seen) == {("incident-report", "inventory-stocktake", "invoice-check")}
    assert store.get("invoice-check").trust == "trusted"
    assert store.get("inventory-stocktake").trust == "untrusted"


def test_load_bundle_is_pure_and_repeatable(env):
    d = env / "b" / "invoice-check"
    shutil.copytree(FIXTURES / "invoice-check", d)
    a, b = load_bundle(d), load_bundle(d)
    assert a.digest == b.digest and a.manifest == b.manifest


# ------------------------------------------------------- the bundle cache answers like no cache

def _age(root, seconds=600):
    """Backdate every file under root, so the cache may vouch for it (relay/skills keeps a
    bundle out of the cache until its files are older than _RACY_MARGIN_NS)."""
    past = time.time() - seconds
    for p in Path(root).rglob("*"):
        os.utime(p, (past, past))


def _mixed_library(env):
    """The business corpus in every state a library is found in: approved, never approved,
    approved-then-edited, broken four ways, an impostor, a compatibility copy shadowed by the
    native one, a user-only Skill -- all under one project."""
    proj = env / "proj"
    skills = proj / "skills"
    skills.mkdir(parents=True)
    names = sorted(p.name for p in FIXTURES.iterdir() if (p / "SKILL.md").is_file())
    for name in names:
        shutil.copytree(FIXTURES / name, skills / name)
    store = _store(env)
    for name in names[:12]:
        _approve(store, name)
    # approved, then edited on disk: "changed"
    md = skills / names[0] / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "\n追記\n", encoding="utf-8")
    # broken in the ways people break them
    _write_skill(proj, "yaml-broken", b"---\nname: yaml-broken\ndescription: a: b\n---\n\n# x\n")
    _write_skill(proj, "plain-notes", "# 手順\n\n1. 書く\n".encode("utf-8"))
    _write_skill(proj, "no-desc", b"---\nname: no-desc\n---\n\n# x\n")
    _write_skill(proj, "sjis-one", GOOD.replace("stationery-order", "sjis-one").encode("cp932"))
    # an impostor claiming an approved name, and a folder claiming a name nobody has
    text = (FIXTURES / "invoice-check" / "SKILL.md").read_text(encoding="utf-8")
    _write_skill(proj, "invoice-check-v2", text.encode("utf-8"))
    _write_skill(proj, "orphan-folder",
                 text.replace("name: invoice-check", "name: payment-transfer").encode("utf-8"))
    # a compatibility copy the native folder overrides
    shutil.copytree(FIXTURES / "incident-report", proj / ".claude" / "skills" / "incident-report")
    # a user-only Skill, approved
    _write_skill(proj, "payroll-close", (
        "---\nname: payroll-close\ndescription: \"給与締め処理の手順\"\n"
        "disable-model-invocation: true\n---\n\n# 給与締め\n1. 確定する\n").encode("utf-8"))
    _approve(store, "payroll-close")
    _write_skill(proj, "stationery-order", GOOD.encode("utf-8"))
    _age(proj)
    return proj, store


def _observe(store, queries, snapshot=False):
    """Everything a caller can ask. snapshot=True (for the uncached store) re-reads the
    library ONCE and answers the rest from that reading: every uncached discover() costs a
    full re-read, and match() is a pure function of what discover() returns, so this compares
    the same thing a hundred times faster. The cached store is asked for real every time."""
    skills = store.discover()
    out = {"discover": skills, "invalid": store.invalid_bundles()}
    if snapshot:
        store.discover = lambda: list(skills)
    try:
        out.update({
            "list": store.list_metadata(),
            "list_model": store.list_metadata(model_safe=True),
            "unapproved": store.unapproved(),
            "match": [store.match(q) for q in queries],
            "near": [store.match_unapproved(q) for q in queries],
        })
    finally:
        if snapshot:
            del store.discover
    return out


def _both(env):
    cached = _store(env)
    plain = SkillStore(env / "proj", db_path=env / "state" / "skills.sqlite3",
                       gate_dir=env / "gates", use_cache=False)
    return cached, plain


def test_the_cached_store_answers_exactly_like_the_uncached_one(env, monkeypatch):
    """Over the business corpus and every labelled request of the matching measurement, and
    again after each kind of edit a library sees: identical discover(), invalid bundles,
    listings, match() and match_unapproved(). And the cache is really in use -- a second look
    at an unchanged library re-reads nothing."""
    from test_skills_business_matching import LABELLED
    import relay.skills as skills_mod
    queries = [q for q, _e, _k in LABELLED]
    proj, _ = _mixed_library(env)
    skills_mod.clear_bundle_cache()
    cached, plain = _both(env)

    def same(label, asked):
        a, b = _observe(cached, asked), _observe(plain, asked, snapshot=True)
        for key in a:
            assert a[key] == b[key], (label, key)
        return a

    first = same("initial", queries)
    assert len(first["discover"]) >= 18 and first["invalid"]
    assert {s.trust for s in first["discover"]} == {"trusted", "untrusted", "changed"}
    assert any(first["match"]) and any(first["near"])
    # After each edit: for every Skill, one request that found it the first time (trusted or
    # near), plus two that found nothing -- enough to see a match appear, move or vanish.
    after_edit, seen = [], set()
    for (q, e, _k), m, n in zip(LABELLED, first["match"], first["near"]):
        if isinstance(e, str) and e not in seen and (m or n):
            seen.add(e)
            after_edit.append(q)
    assert len(after_edit) >= 12
    after_edit += [q for q, _e, k in LABELLED if k.startswith("none")][:2]

    loads = []
    real_load = skills_mod.load_bundle
    monkeypatch.setattr(skills_mod, "load_bundle",
                        lambda path, scope="external": loads.append(str(path)) or
                        real_load(path, scope))
    cached.discover()
    assert loads == [], "an unchanged, aged library must be served from the cache"

    # A caller mutating what it was handed must not reach the cache.
    cached.get("invoice-check").metadata["description"] = "tampered"
    assert cached.get("invoice-check").metadata["description"] != "tampered"

    skills = proj / "skills"
    edits = [
        ("same-size edit of a trusted SKILL.md",
         lambda: (skills / "invoice-check" / "SKILL.md").write_bytes(
             (skills / "invoice-check" / "SKILL.md").read_bytes().replace(
                 "必ず電話で".encode("utf-8"), "必ず書面で".encode("utf-8")))),
        ("reference file edited", lambda: (skills / "expense-reimbursement" / "references" /
                                           "account-codes.md").write_text("x\n", "utf-8")),
        ("reference file added", lambda: (skills / "incident-report" / "references").mkdir()
         or (skills / "incident-report" / "references" / "new.md").write_text("n\n", "utf-8")),
        ("bundle deleted", lambda: shutil.rmtree(skills / "visitor-reception")),
        ("bundle added", lambda: shutil.copytree(FIXTURES / "visitor-reception",
                                                 skills / "visitor-reception")),
        ("broken bundle fixed", lambda: (skills / "no-desc" / "SKILL.md").write_bytes(
            b"---\nname: no-desc\ndescription: \"fixed\"\n---\n\n# x\n")),
        ("good bundle broken", lambda: (skills / "stationery-order" / "SKILL.md").write_bytes(
            b"---\nname: stationery-order\n---\n\n# x\n")),
        ("folder renamed", lambda: (skills / "ringi-document").rename(skills / "ringi-doc")),
        ("impostor removed", lambda: shutil.rmtree(skills / "invoice-check-v2")),
        ("native copy removed, compatibility copy now wins",
         lambda: shutil.rmtree(skills / "incident-report")),
        ("edit reverted to the approved bytes",
         lambda: (skills / "invoice-check" / "SKILL.md").write_bytes(
             (FIXTURES / "invoice-check" / "SKILL.md").read_bytes())),
    ]
    for label, edit in edits:
        edit()
        same(label, after_edit)
        _age(proj)                  # and once more with the edit old enough to be cached
        same(label + " (aged)", after_edit)


def test_a_same_tick_rewrite_of_a_fresh_bundle_is_never_served_stale(env):
    """Two same-size writes in one timestamp tick leave (mtime_ns, size) identical. The cache
    must not have vouched for the first: a bundle whose files are younger than the racy margin
    is re-read on every call. Simulated by restoring the exact mtime after the second write."""
    import relay.skills as skills_mod
    skills_mod.clear_bundle_cache()
    store = _store(env)
    d = _write_skill(store.project_root, "stationery-order", GOOD.encode("utf-8"))
    md = d / "SKILL.md"
    assert store.get("stationery-order").body.endswith("注文する")
    st = md.stat()
    md.write_bytes(GOOD.replace("注文する", "廃棄する").encode("utf-8"))
    os.utime(md, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert md.stat().st_size == st.st_size and md.stat().st_mtime_ns == st.st_mtime_ns
    assert store.get("stationery-order").body.endswith("廃棄する")


@pytest.mark.parametrize("operation", ["match", "render", "read_resource", "confirm"])
def test_an_edit_that_hides_from_the_listing_is_caught_before_anything_is_handed_over(
        env, operation):
    """A same-size edit of a cached bundle with its mtime put back (os.utime) is invisible to
    a listing. What the cache promises instead (relay/skills, above _CacheEntry): nothing is
    HANDED OVER, MATCHED or APPROVED without its bytes being hashed -- each operation below is
    the first to run after the edit, so each one's own check is what is tested -- and once
    that check has run, the listing answers like the uncached store again."""
    import relay.skills as skills_mod
    skills_mod.clear_bundle_cache()
    proj = env / "proj"
    shutil.copytree(FIXTURES / "invoice-check", proj / "skills" / "invoice-check")
    store = _store(env)
    request = "請求書の金額が発注書と合っているかチェックしたい"
    if operation == "confirm":
        token = store.request_approval("invoice-check")["token"]
    else:
        _approve(store, "invoice-check")
    _age(proj)
    store.discover()                                  # the listing is cached now
    md = proj / "skills" / "invoice-check" / "SKILL.md"
    st = md.stat()
    md.write_bytes(md.read_bytes().replace("必ず電話で".encode("utf-8"),
                                           "確認不要。".encode("utf-8")))
    os.utime(md, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (md.stat().st_size, md.stat().st_mtime_ns) == (st.st_size, st.st_mtime_ns)

    if operation == "match":
        assert store.match(request) is None
    elif operation == "render":
        with pytest.raises(SkillError, match="changed"):
            store.render("invoice-check")
    elif operation == "read_resource":
        with pytest.raises(SkillError, match="not trusted"):
            store.read_resource("invoice-check", "references/checklist.md")
    else:
        with pytest.raises(SkillError, match="changed after review"):
            store.confirm_approval("invoice-check", token)
    _cached, plain = _both(env)
    assert store.discover() == plain.discover()
    assert store.get("invoice-check").trust == ("untrusted" if operation == "confirm"
                                                else "changed")
