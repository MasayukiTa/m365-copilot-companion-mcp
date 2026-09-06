"""Persona-leak scorer -- the QUALITY lens of the general-user self-improvement loop.

今日(2026-06-28) commit 1c89bb3 で OUTPUT_DISCIPLINE(自我クランプ)を入れた。その効果を「人手検証」から
「常時自動計測」へ格上げするためのコアscorer。fleet(M365/Copilot)の一般利用出力に、頼まれてもいない
助言者/講釈/自我ペルソナが漏れていないか(persona-leak)をベンチ無しで判定する。

設計の核心(memory警告に従う):
  - signature検索は誤検出が多い(ツール接続の「まずは接続して」、コード/issueの箇条書き)。よって**単語1個では
    判定しない**。異なるシグナルクラスが>=2発火、OR 単一クラスが高密度(複数ヒット)のときだけ persona_leak。
  - コードブロック(```)内・ツール指示行(call_tool / 行頭 CONTINUE/DONE/STUCK/RESEARCH/ANALYZE)・goalのエコーは
    判定の前に本文から除外する。これらは正常出力なのに persona 語彙とぶつかりやすい。

すべて offline・純粋・決定的(乱数/時刻で挙動を変えない)・defensive(欠損/壊れファイルは空に縮退し例外を投げない)。
"""
import json
import os
import re

PERSONA_VERSION = "1"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 行頭に来たらツール指示/制御マーカーとみなし、その行を判定対象から落とす(本文ではない)。
_CONTROL_PREFIXES = ("CONTINUE", "DONE", "STUCK", "RESEARCH", "ANALYZE")

# --------------------------------------------------------------------------------------------------
# シグナルクラスの正規表現
# --------------------------------------------------------------------------------------------------
# 各クラスは「複数の語彙パターン」を持つ。1ヒット=1発火カウント。クラス間の発火数とクラス内の密度の
# 両方を後段で見るので、ここでは「自我/講釈に固有で、淡々とした事実ベースの推奨文には出にくい」語彙だけを
# 慎重に拾う。命令調の動詞(固めろ/やれ)を一般的な助言「〜してください/〜するとよい」と混同しないこと。
_SIGNAL_PATTERNS = {
    # coaching: 「まずは〜しろ/固めろ/やれ」式の上から命令調コーチング。丁寧形(してください/しよう)は含めない。
    "coaching": [
        re.compile(r"まずは[^。\n]{0,20}?(?:固め|やれ|やろ|覚え|学べ|やりきれ|身につけ|潰せ)"),
        re.compile(r"(?:完璧に|徹底的に|まず)\s*[^。\n]{0,12}?(?:固めろ|やれ|覚えろ|学べ|潰せ|やりきれ)"),
        re.compile(r"[^。\n]{0,12}(?:しろ|やれ|覚えろ|捨てろ|諦めろ|黙って従え|手を動かせ)(?:[。!\n]|$)"),
        re.compile(r"鉄則は[^。\n]{0,20}?だ"),  # 「鉄則は〜だ」式の断定コーチング(淡々とした「鉄則:」とは別物)
    ],
    # condescension: 「初心者の9割」「今の理解レベルだと」「言っておくが」式の上から目線・決めつけ。
    "condescension": [
        re.compile(r"初心者の\s*\d+\s*割"),
        re.compile(r"(?:今の|君の|お前の|あなたの)[^。\n]{0,8}?理解(?:レベル|度)(?:だと|では|じゃ)"),
        re.compile(r"言っておくが"),
        re.compile(r"はっきり言って"),
        re.compile(r"[^。\n]{0,12}(?:詰む|詰まる|挫折する)(?:のがオチ|のが落ち|だろう|ぞ)"),
        re.compile(r"そもそも[^。\n]{0,12}?わかって(?:ない|いない)"),
        re.compile(r"正直[、,]?\s*[^。\n]{0,8}?レベル"),
    ],
    # ego: 一人称の主張・キャラ付け(俺/私が思うに 等)。事実の主語「私たち」や引用は拾わない。
    "ego": [
        re.compile(r"(?:俺|オレ)(?:が|は|なら|的には|に言わせ)"),
        re.compile(r"(?:私|僕)が思うに"),
        re.compile(r"(?:俺|私|僕)(?:の)?(?:経験|流儀|やり方|スタイル)(?:では|だと|から言う)"),
        re.compile(r"個人的(?:に|には)言わせてもらえば"),
        re.compile(r"言わせてもらう(?:と|が)"),
    ],
    # preface_lecture: 頼まれてない長い前置き講釈。「結論から言うと」式の前置き + 講義トーンの接続。
    "preface_lecture": [
        re.compile(r"結論から言う(?:と|ぞ)"),
        re.compile(r"^[\s　]*(?:そもそも|前提として|大前提として|まず大事なのは)", re.MULTILINE),
        re.compile(r"いいか[、,]"),
        re.compile(r"覚えておいてほしいのは"),
        re.compile(r"(?:基礎|基本)(?:が|を)(?:大事|大切|できてない|なってない)"),
    ],
}

# 単一クラスが「高密度」とみなす最小ヒット数。
# 根拠: 1ヒットは誤検出(ツール接続の「まずは接続して」、引用、偶然の語)で立ちやすいので必ず除外する。
# 同一クラスが2ヒットすれば、それは偶然の一致ではなくその文体が反復している強い証拠 -> leak。
# これは guards.significance_gate と同じ保守思想(弱い単発シグナルでは結論しない)。
_HIGH_DENSITY = 2

# 異なるクラスが何種類発火したら leak とみなすか。
# 根拠: 別クラスが2種同時(例: coaching + condescension)は、たまたまではなく
# 「助言+上から目線」という persona の複合徴候。単一クラス1ヒットより遥かに堅い。
_MIN_DISTINCT_CLASSES = 2


def _strip_noise(text, goal=None):
    """判定前に本文からノイズを除去する。

    除外対象: (1) ```で囲まれたコードブロック内 (2) ツール指示行(call_tool / 行頭の制御マーカー)
    (3) goal のエコー(goal が与えられたとき、その本文を空白化)。
    これらは正常出力なのに persona 語彙と衝突しやすいので、シグナル判定の母集団から外す。
    """
    if not text:
        return ""
    s = text

    # (1) フェンス付きコードブロックを丸ごと除去(```...```)。閉じ忘れにも対応(末尾まで落とす)。
    s = re.sub(r"```.*?```", " ", s, flags=re.DOTALL)
    s = re.sub(r"```.*\Z", " ", s, flags=re.DOTALL)

    # (3) goal のエコー除去: goal 本文がそのまま貼られた箇所を空白化(部分一致でも母集団から外す)。
    if goal:
        g = goal.strip()
        if len(g) >= 8 and g in s:
            s = s.replace(g, " ")

    # (2) 行単位フィルタ: ツール指示行 / 行頭制御マーカー行 を落とす。
    out_lines = []
    for ln in s.split("\n"):
        stripped = ln.strip()
        if not stripped:
            out_lines.append(ln)
            continue
        if "call_tool" in stripped:
            continue
        head = stripped.split(":", 1)[0].split()[0] if stripped.split() else ""
        head = head.rstrip(":：")
        if head in _CONTROL_PREFIXES:
            continue
        out_lines.append(ln)
    return "\n".join(out_lines)


def score_text(text, goal=None):
    """ヒューリスティック persona-leak スコアラ。offline・純粋・決定的。

    return {"persona_leak": bool, "score": float(0..1), "signals": [class,...], "judged_by": "heuristic"}

    判定: ノイズ除去後の本文でシグナルクラスごとのヒット数を数え、
      - 異なるクラスが >= _MIN_DISTINCT_CLASSES(=2) 発火、OR
      - 単一クラスが >= _HIGH_DENSITY(=2) ヒット(高密度)
    のいずれかで persona_leak=True。どちらも満たさない(=単発・弱い)場合は誤検出回避で False。
    """
    body = _strip_noise(text, goal=goal)

    per_class = {}          # class -> ヒット数
    for cls, pats in _SIGNAL_PATTERNS.items():
        hits = 0
        for pat in pats:
            hits += len(pat.findall(body))
        if hits:
            per_class[cls] = hits

    fired_classes = sorted(per_class.keys())
    distinct = len(fired_classes)
    max_density = max(per_class.values()) if per_class else 0

    leak = (distinct >= _MIN_DISTINCT_CLASSES) or (max_density >= _HIGH_DENSITY)

    # score: 0..1 の連続強度(表示/ソート用)。発火クラス数と総ヒット数を素朴に正規化した決定的値。
    total_hits = sum(per_class.values())
    # 4クラス全部 + 各複数ヒット相当を上限の目安にし、保守的に頭打ち。
    raw = 0.34 * distinct + 0.12 * total_hits
    score = round(min(1.0, raw), 4)

    return {
        "persona_leak": bool(leak),
        "score": score,
        "signals": fired_classes,
        "judged_by": "heuristic",
    }


# --------------------------------------------------------------------------------------------------
# 本文解決 (history item -> 採点対象テキスト)
# --------------------------------------------------------------------------------------------------

def _open_transcript(path):
    """Open a recorded transcript, whether or not it has been compressed since it was written.

    THE RECORD OUTLIVES THE UNCOMPRESSED FILE. A run stores the path its transcript had when
    it finished; transcripts are gzipped as they age, so the stored path stops resolving and
    every reader that opens it directly starts getting nothing. Here that surfaced as
    _read_jsonl_last_assistant returning None -- caught by its own `except`, so grading simply
    fell through to another body source and said nothing about it. Measured on this machine
    2026-09-06: 16 of 16 recorded transcripts had already become .jsonl.gz.

    Raises exactly like open() when neither form exists, so the caller's existing exception
    handling and its "missing file -> None" contract are unchanged.
    """
    if not os.path.isfile(path) and os.path.isfile(path + ".gz"):
        import gzip
        return gzip.open(path + ".gz", "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def _read_jsonl_last_assistant(path):
    """jsonl transcript を読み、最後の role=="assistant" レコードの text を返す。

    壊れ行はスキップ。ファイル欠損/読めない場合は None。jsonl行 keys: role,text,ts,turn(先頭に meta行あり)。
    """
    try:
        last = None
        with _open_transcript(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("role") == "assistant":
                    t = rec.get("text")
                    if isinstance(t, str) and t.strip():
                        last = t
        return last
    except Exception:
        return None


def _resolve_body(item, transcript_root=None):
    """history item から採点本文と source を解決する。

    返り: (body or None, source) ; source in {"transcript","outcome","none"}。
    優先順: transcript(jsonl)の last assistant text -> item["outcome"] -> none。
    transcript_root 指定時は相対pathをそこ基準に解決。欠損/壊れは defensive に次へ降格。
    """
    if not isinstance(item, dict):
        return None, "none"

    tp = item.get("transcript")
    if isinstance(tp, str) and tp.strip():
        path = tp
        if transcript_root and not os.path.isabs(path):
            path = os.path.join(transcript_root, path)
        body = _read_jsonl_last_assistant(path)
        if body is not None and body.strip():
            return body, "transcript"

    oc = item.get("outcome")
    if isinstance(oc, str) and oc.strip():
        return oc, "outcome"

    return None, "none"


def score_run(item, transcript_root=None):
    """history item 1件を採点。

    本文解決(_resolve_body)後、解決できれば score_text を適用し source/body_len/key を付与。
    解決不能なら score_text は呼ばず {"persona_leak": None, "source":"none", "body_len":0, "key":...}。
    """
    key = item.get("key") if isinstance(item, dict) else None
    goal = item.get("goal") if isinstance(item, dict) else None
    body, source = _resolve_body(item, transcript_root=transcript_root)

    if body is None:
        return {
            "persona_leak": None,
            "score": None,
            "signals": [],
            "judged_by": "heuristic",
            "body_len": 0,
            "source": "none",
            "key": key,
        }

    res = score_text(body, goal=goal)
    res["body_len"] = len(body)
    res["source"] = source
    res["key"] = key
    return res


# --------------------------------------------------------------------------------------------------
# 履歴集計
# --------------------------------------------------------------------------------------------------

def _excerpt(text, limit=160):
    """flagged item 用の抜粋(<=limit字)。改行を畳んで先頭を切り出す。"""
    s = re.sub(r"\s+", " ", (text or "").strip())
    return s[:limit]


def score_history(items, transcript_root=None, judge_fn=None):
    """履歴(items)全体を採点して集計 dict を返す。

    return {"version","n_scored","leak_count","leak_rate"(=leak/n_scored or None),
            "flagged":[{"key","signals","score","excerpt"(<=160字)} ...<=20],"judged_by"}

    n_scored = persona_leak が None でない(=本文解決できた)件数。
    judge_fn 指定時: heuristic で flagged な item の text のみ judge_fn(text)->bool で再確認
      (True=leak確定 / False=cleanへ降格)。judged_by="llm"。judge_fn=None は heuristic のみ。
    """
    items = items if isinstance(items, list) else []
    n_scored = 0
    leak_count = 0
    flagged = []

    for item in items:
        if not isinstance(item, dict):
            continue
        res = score_run(item, transcript_root=transcript_root)
        if res.get("persona_leak") is None:
            continue  # 本文解決不能 -> 母集団に入れない
        n_scored += 1
        is_leak = bool(res.get("persona_leak"))

        if is_leak and judge_fn is not None:
            # heuristic で flagged な item だけ LLM judge に回す(コスト最小)。
            body, _src = _resolve_body(item, transcript_root=transcript_root)
            try:
                confirmed = bool(judge_fn(body if body is not None else ""))
            except Exception:
                # judge 失敗時は heuristic を尊重(保守: 既に flagged なので leak のまま)。
                confirmed = True
            is_leak = confirmed

        if is_leak:
            leak_count += 1
            if len(flagged) < 20:
                body, _src = _resolve_body(item, transcript_root=transcript_root)
                flagged.append({
                    "key": res.get("key"),
                    "signals": res.get("signals", []),
                    "score": res.get("score"),
                    "excerpt": _excerpt(body),
                })

    leak_rate = round(leak_count / n_scored, 4) if n_scored else None

    return {
        "version": PERSONA_VERSION,
        "n_scored": n_scored,
        "leak_count": leak_count,
        "leak_rate": leak_rate,
        "flagged": flagged,
        "judged_by": "llm" if judge_fn is not None else "heuristic",
    }


# --------------------------------------------------------------------------------------------------
# LLM judge プロンプト / 応答解析
# --------------------------------------------------------------------------------------------------

def judge_prompt(text):
    """LLM judge 用プロンプトを生成する(日本語)。

    「この出力は助言者/講釈/自我ペルソナが出ているか。LEAK か CLEAN の1語 + 理由1行で答えよ」を、
    対象 text を区切りで挟んで返す。
    """
    body = text if isinstance(text, str) else ""
    return (
        "次の出力を判定してください。これは、ユーザーに頼まれてもいない助言者ぶり・上から目線の講釈・"
        "自我(キャラ付け/一人称の主張)といった『ペルソナの漏れ』が出ているかどうかの判定です。\n"
        "淡々と事実ベースで推奨を述べているだけなら CLEAN です。命令調コーチング・決めつけ・自我・"
        "頼まれてない長い前置き講釈があれば LEAK です。\n"
        "最初の1語で `LEAK` か `CLEAN` のどちらかだけを答え、続けて理由を1行で書いてください。\n"
        "----- 出力ここから -----\n"
        + body +
        "\n----- 出力ここまで -----\n"
    )


def parse_judge_verdict(reply):
    """judge 応答から LEAK/CLEAN を解析 -> True iff LEAK。曖昧時は False(誤検出を避ける保守側)。

    先頭トークンを優先し、無ければ本文中の最初に現れる LEAK/CLEAN を採る。どちらも無ければ False。
    """
    if not isinstance(reply, str) or not reply.strip():
        return False
    low = reply.strip().lower()

    # 先頭トークン優先(プロンプトで「最初の1語」を要求しているため)。
    head = re.split(r"[\s、,。.:：!！\-]+", low, maxsplit=1)[0]
    if head == "leak":
        return True
    if head == "clean":
        return False

    # 先頭で決まらないとき: 本文中で先に出た方を採用。
    # 注: 日本語が隣接(leakです 等)すると \b が効かない(JP文字も \w 扱い)ので語境界は付けない。
    m_leak = re.search(r"leak|ﾘｰｸ|漏れ", low)
    m_clean = re.search(r"clean|クリーン|問題な(?:い|し)", low)
    if m_leak and m_clean:
        return m_leak.start() < m_clean.start()
    if m_leak:
        return True
    return False  # CLEAN もしくは曖昧 -> 保守側で False


if __name__ == "__main__":
    path = os.path.join(_REPO_ROOT, ".fleet", "history.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else (data.get("items") or [])
    except Exception:
        items = []
    print(json.dumps(score_history(items), ensure_ascii=False, indent=2))
