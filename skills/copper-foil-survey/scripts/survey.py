# -*- coding: utf-8 -*-
"""銅箔の資材略称を1つ受け取り、保証期限を超えて使われたロットを調べて帳票にする。

引数: 資材略称（例 "MT18EX5 RM"）。任意で検査開始日（既定 2020-01-01）と出力先。

SQL は 260428_修正版銅箔名から保証差日数.sql をそのまま踏襲している。旧版が
V_銅箔検査結果.製品ロット で結合して常に0件だった経緯があるため、ここを勝手に
書き換えないこと。品証ロットは 2025年以前のトレース側に -XX サフィックスが無く、
2026年以降は両側にあるので、full と base の両方で等値結合する（LIKE は使わない）。

同じ入力からは必ず同じ出力になる。並び順は SQL で固定し、集計の丸めと出力の
キー順もコード内で固定してある。グラフは matplotlib のメタデータを含めても
再実行でバイト一致することを実測済み。
"""
import json
import os
import sys

# skill は束の中身のハッシュで信頼を判定する。同じ場所の charts / workbook を
# import すると __pycache__ ができ、それだけでハッシュが変わって、一度通した
# 承認が次の実行で外れる。ハッシュ側を緩めると .pyc が検査を素通りするので、
# 作らせない方で揃える。import より前に立てないと効かない。
sys.dont_write_bytecode = True

sys.stdout.reconfigure(encoding="utf-8")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

TEST_ITEM = "引きはがし：常態"     # 銅箔の代表試験。ロット1件に1行へ落とすための軸
DEFAULT_FROM = "2020-01-01"

# 実テンポラリ表を使い、結合はすべて等値にする。
#
# 元の版はテーブル変数(@CF/@LNK)と、OR を含む結合(`= full OR = base`)だった。
# テーブル変数は統計を持たず1行と見積もられるため、66,583行の V_SPC_LOT_TRACE 側に
# 最悪の計画が選ばれ、148行との突き合わせが15分でも終わらない。#temp なら統計が付き、
# OR を4本の等値結合に割れば seek できる（実測: @CF 作成 53.6s -> #CF 22.2s）。
#
# 全体を1バッチで実行すること。#temp の寿命は文をまたぐと切れる。
# 重いのは V_銅箔検査結果 の走査だけ（実測 22〜54秒）。既存VBAはここを Excel シートに
# キャッシュして初回だけにしている。同じ設計にする: 材料ごとに1回引いてローカルへ持ち、
# 以降は #CF へ VALUES で流し込む。キャッシュは「その開始日以前まで含んでいるか」で
# 使い回しを判断する（VBA の CacheCoversDate と同じ考え方）。
CF_SQL = r"""
SET NOCOUNT ON;
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SELECT 品証ロット,
    CASE WHEN 品証ロット LIKE '%-[0-9]'               THEN LEFT(品証ロット, LEN(品証ロット)-2)
         WHEN 品証ロット LIKE '%-[0-9][0-9]'          THEN LEFT(品証ロット, LEN(品証ロット)-3)
         WHEN 品証ロット LIKE '%-[0-9][0-9][0-9]'     THEN LEFT(品証ロット, LEN(品証ロット)-4)
         ELSE 品証ロット END AS 品証ロット_base,
    資材略称, 常非区分, ケースNO,
    CONVERT(VARCHAR(10), 入荷日, 23)     AS 入荷日,
    CONVERT(VARCHAR(10), 検査年月日, 23) AS 検査年月日,
    CONVERT(VARCHAR(10), 保証期限, 23)   AS 保証期限
FROM (SELECT 品証ロット, 資材略称, 常非区分, ケースNO, 入荷日, 検査年月日, 保証期限,
             ROW_NUMBER() OVER (PARTITION BY 品証ロット ORDER BY 検査年月日 DESC) rn
      FROM [dbo].[V_銅箔検査結果]
      WHERE 資材略称 = ? AND 試験項目 = N'""" + TEST_ITEM + r"""'
        AND 検査年月日 >= ?) x
WHERE rn = 1;
"""

SQL = r"""
SET NOCOUNT ON;
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SET LOCK_TIMEOUT 60000;

IF OBJECT_ID('tempdb..#CF') IS NOT NULL DROP TABLE #CF;
-- 文字列列には COLLATE DATABASE_DEFAULT が要る。テンポラリ表は tempdb の既定
-- 照合順序になるため、実テーブル側 (Japanese_CS_AS_KS_WS) と等値比較した瞬間に
-- 「照合順序の競合を解決できません」で落ちる。既存VBAも同じ指定をしている。
CREATE TABLE #CF(
    品証ロット_full NVARCHAR(100) COLLATE DATABASE_DEFAULT,
    品証ロット_base NVARCHAR(100) COLLATE DATABASE_DEFAULT,
    資材略称 NVARCHAR(100) COLLATE DATABASE_DEFAULT,
    常非区分 NVARCHAR(100) COLLATE DATABASE_DEFAULT,
    ケースNO NVARCHAR(100) COLLATE DATABASE_DEFAULT,
    入荷日 DATETIME2, 検査年月日 DATETIME2, 保証期限 DATETIME2);
-- 行はキャッシュから VALUES で流し込む（_load_cf が組み立てる）
{CF_VALUES}
CREATE CLUSTERED INDEX ix_full ON #CF(品証ロット_full);
CREATE NONCLUSTERED INDEX ix_base ON #CF(品証ロット_base);

IF OBJECT_ID('tempdb..#LNK') IS NOT NULL DROP TABLE #LNK;
CREATE TABLE #LNK(
    製品ロット NVARCHAR(100) COLLATE DATABASE_DEFAULT,
    面 NVARCHAR(2) COLLATE DATABASE_DEFAULT,
    品証ロット NVARCHAR(100) COLLATE DATABASE_DEFAULT);
INSERT INTO #LNK
SELECT DISTINCT d.SEIHINN_LOTNO, N'表', c.品証ロット_full FROM #CF c
  JOIN [dbo].[TRC_MCL_PRESSvsDOHAKU] d WITH(NOLOCK) ON d.DOHAKU_LOTNO_OMOTE = c.品証ロット_full
UNION
SELECT DISTINCT d.SEIHINN_LOTNO, N'表', c.品証ロット_full FROM #CF c
  JOIN [dbo].[TRC_MCL_PRESSvsDOHAKU] d WITH(NOLOCK) ON d.DOHAKU_LOTNO_OMOTE = c.品証ロット_base
UNION
SELECT DISTINCT d.SEIHINN_LOTNO, N'裏', c.品証ロット_full FROM #CF c
  JOIN [dbo].[TRC_MCL_PRESSvsDOHAKU] d WITH(NOLOCK) ON d.DOHAKU_LOTNO_URA = c.品証ロット_full
UNION
SELECT DISTINCT d.SEIHINN_LOTNO, N'裏', c.品証ロット_full FROM #CF c
  JOIN [dbo].[TRC_MCL_PRESSvsDOHAKU] d WITH(NOLOCK) ON d.DOHAKU_LOTNO_URA = c.品証ロット_base
UNION
SELECT DISTINCT d2.SEIHIN_LOT_NO, N'表', c.品証ロット_full FROM #CF c
  JOIN [dbo].[SCM_DAT_RST_SHIKOMI_DTL_RAI] d2 WITH(NOLOCK) ON d2.DOUHAKU_NO_U = c.品証ロット_full
UNION
SELECT DISTINCT d2.SEIHIN_LOT_NO, N'表', c.品証ロット_full FROM #CF c
  JOIN [dbo].[SCM_DAT_RST_SHIKOMI_DTL_RAI] d2 WITH(NOLOCK) ON d2.DOUHAKU_NO_U = c.品証ロット_base
UNION
SELECT DISTINCT d2.SEIHIN_LOT_NO, N'裏', c.品証ロット_full FROM #CF c
  JOIN [dbo].[SCM_DAT_RST_SHIKOMI_DTL_RAI] d2 WITH(NOLOCK) ON d2.DOUHAKU_NO_S = c.品証ロット_full
UNION
SELECT DISTINCT d2.SEIHIN_LOT_NO, N'裏', c.品証ロット_full FROM #CF c
  JOIN [dbo].[SCM_DAT_RST_SHIKOMI_DTL_RAI] d2 WITH(NOLOCK) ON d2.DOUHAKU_NO_S = c.品証ロット_base;
CREATE CLUSTERED INDEX ix_lot ON #LNK(製品ロット);

-- 試験値の負値は測定値ではなくコード。-100 以下を素通しすると、剥離強度に
-- -200 や -777777 が混ざって平均も散布図も壊れる。はんだ耐熱は値そのものが
-- 判定コードなので文字へ変換する。いずれも既存VBAから写した符号化で、
-- 推測で導けるものではない。
IF OBJECT_ID('tempdb..#P') IS NOT NULL DROP TABLE #P;
SELECT ロットNO, MAX(製造品名) 製造品名, MAX(製造年月日) 製造年月日,
  MAX(CASE WHEN 試験項目=N'引きはがし強さ(表)'        AND 試験値>-100 THEN 試験値 END) 剥離表,
  MAX(CASE WHEN 試験項目=N'引きはがし強さ(裏)'        AND 試験値>-100 THEN 試験値 END) 剥離裏,
  MAX(CASE WHEN 試験項目=N'引きはがし強さ　処理後(表)' AND 試験値>-100 THEN 試験値 END) 剥離処理後表,
  MAX(CASE WHEN 試験項目=N'引きはがし強さ　処理後(裏)' AND 試験値>-100 THEN 試験値 END) 剥離処理後裏,
  MAX(CASE WHEN 試験項目=N'絶縁抵抗(常態)'   AND 試験値>-100 THEN 試験値 END) 絶縁常態,
  MAX(CASE WHEN 試験項目=N'絶縁抵抗(煮沸後)' AND 試験値>-100 THEN 試験値 END) 絶縁煮沸後,
  MAX(CASE WHEN 試験項目 LIKE N'はんだ耐熱性(表)%' THEN
      CASE WHEN 試験値=-777777 THEN N'不合格' WHEN 試験値=-300 THEN N'合格'
           WHEN 試験値=-299 THEN N'不合格?' ELSE N'要確認' END END) はんだ表,
  MAX(CASE WHEN 試験項目 LIKE N'はんだ耐熱性(裏)%' THEN
      CASE WHEN 試験値=-777777 THEN N'不合格' WHEN 試験値=-300 THEN N'合格'
           WHEN 試験値=-299 THEN N'不合格?' ELSE N'要確認' END END) はんだ裏
INTO #P
FROM [dbo].[V_製品検査データ] WITH(NOLOCK)
WHERE 製造年月日 IS NOT NULL AND ロットNO IN (SELECT 製品ロット FROM #LNK)
GROUP BY ロットNO;
CREATE CLUSTERED INDEX ix_p ON #P(ロットNO);

SELECT DISTINCT
    cf.資材略称, cf.常非区分, cf.ケースNO, lnk.製品ロット, lnk.面, lnk.品証ロット,
    CONVERT(VARCHAR(10), cf.入荷日, 23)     AS 入荷日,
    CONVERT(VARCHAR(10), cf.検査年月日, 23) AS 検査年月日,
    CONVERT(VARCHAR(10), cf.保証期限, 23)   AS 保証期限,
    p.製造品名,
    CONVERT(VARCHAR(10), p.製造年月日, 23)  AS 製造年月日,
    DATEDIFF(DAY, cf.検査年月日, p.製造年月日) AS 経過日数,
    DATEDIFF(DAY, p.製造年月日,   cf.保証期限) AS 保証差日数,
    p.剥離表, p.剥離裏, p.剥離処理後表, p.剥離処理後裏,
    p.絶縁常態, p.絶縁煮沸後, p.はんだ表, p.はんだ裏
FROM #LNK lnk
JOIN #CF cf ON cf.品証ロット_full = lnk.品証ロット
JOIN #P p ON p.ロットNO = lnk.製品ロット
WHERE cf.検査年月日 <= p.製造年月日
  -- 36か月の窓と、保証期限が検査年月日より前という壊れたデータの除外。どちらも
  -- 既存VBAにあり、無いと無関係な遠い製造ロットと壊れた行が結果に紛れ込む。
  AND p.製造年月日 <= DATEADD(MONTH, 36, cf.検査年月日)
  AND cf.保証期限 >= cf.検査年月日
  AND DATEDIFF(DAY, p.製造年月日, cf.保証期限) < 0
ORDER BY 保証差日数 ASC, ケースNO ASC, 製品ロット ASC, 面 ASC;
"""


def _connect():
    """.env の readonly 資格情報でつなぐ。書き込み権限は要らないし持たせない。"""
    import pyodbc
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.environ.get("COPPER_FOIL_ENV") or os.path.join(
        os.path.expanduser("~"), "Desktop", "50repo", "github_lab", "ExcelSQL", ".env")
    env = {}
    with open(env_path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v
    cs = ("DRIVER={SQL Server};SERVER=%s;DATABASE=%s;UID=%s;PWD=%s;Connection Timeout=30"
          % (env["DB_SERVER"], env["DB_NAME_CU"], env["DB_USER"], env["DB_PASS"]))
    return pyodbc.connect(cs, timeout=900)


# キャッシュも束の外へ置く。束の中に置くと、キャッシュが増えるたびに skill の
# ハッシュが変わり、承認が外れる。中身は使い回せる方が速いので消したくはない。
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".companion_cache", "copper-foil-survey")


def _cache_path(material):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in material)
    return os.path.join(CACHE_DIR, "cf_%s.json" % safe)


def load_cf(cursor, material, from_date, refresh=False):
    """V_銅箔検査結果 の走査結果を材料ごとに1回だけ取り、以降は使い回す。

    ここが全体の所要時間のほとんどを占める（実測 22〜54秒）。既存VBAが Excel シートに
    キャッシュしているのと同じ理由で、同じ判断（要求された開始日をキャッシュが
    含んでいるか）で再利用する。含んでいなければ引き直す。
    """
    path = _cache_path(material)
    if not refresh and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                c = json.load(f)
            if c.get("from_date", "9999") <= from_date:
                return [r for r in c["rows"] if r["検査年月日"] >= from_date], True
        except Exception:
            pass
    cursor.execute(CF_SQL, material, from_date)
    cols = [d[0] for d in cursor.description]
    rows = [{k: ("" if v is None else str(v).strip())
             for k, v in zip(cols, r)} for r in cursor.fetchall()]
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"material": material, "from_date": from_date, "rows": rows},
                  f, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)
    return rows, False


def _cf_values(rows):
    """#CF への INSERT ... VALUES を組み立てる。文字列は素で埋めずエスケープする。"""
    if not rows:
        return "-- 該当行なし"
    def q(v):
        return "N'" + str(v).replace("'", "''") + "'" if v != "" else "NULL"
    out, chunk = [], []
    for r in rows:
        chunk.append("(%s,%s,%s,%s,%s,%s,%s,%s)" % (
            q(r["品証ロット"]), q(r["品証ロット_base"]), q(r["資材略称"]),
            q(r["常非区分"]), q(r["ケースNO"]),
            q(r["入荷日"]), q(r["検査年月日"]), q(r["保証期限"])))
        if len(chunk) == 500:          # 1文あたりの行数上限に触れないよう分割
            out.append("INSERT INTO #CF VALUES " + ",".join(chunk) + ";")
            chunk = []
    if chunk:
        out.append("INSERT INTO #CF VALUES " + ",".join(chunk) + ";")
    return "\n".join(out)


def _setup_font():
    # 既存の調査ブックと同じ MigMix 1P を最優先にする。ここが違うと、同じ調査の
    # 成果物なのに並べたときに別物に見える。以降は入っていない環境向けの控え。
    for name in ("MigMix 1P", "BIZ UDGothic", "Yu Gothic", "Meiryo", "MS Gothic"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False


def _write_md(res, path):
    L = ["# 銅箔 保証期限超過 調査結果", "",
         "- 対象: %s" % res["material"],
         "- 検査開始日: %s 以降" % res["from_date"],
         "- 超過して使用されたロット: %d 件" % res["count"], ""]
    if res["count"]:
        w = res["worst"]
        L += ["## 最も超過が大きいもの", "",
              "- 製品ロット %s（%s面） / 製造品名 %s" % (w["製品ロット"], w["面"], w["製造品名"]),
              "- 保証期限 %s に対し 製造年月日 %s ＝ **%d 日超過**"
              % (w["保証期限"], w["製造年月日"], -w["保証差日数"]), "",
              "## 超過日数の分布", "",
              "| 区分 | 件数 |", "|---|---|"]
        for k, v in res["buckets"]:
            L.append("| %s | %d |" % (k, v))
        L += ["", "## 一覧（超過が大きい順・上位30件）", "",
              "| 製品ロット | 面 | 製造品名 | 製造年月日 | 保証期限 | 超過日数 |",
              "|---|---|---|---|---|---|"]
        for r in res["rows"][:30]:
            L.append("| %s | %s | %s | %s | %s | %d |" % (
                r["製品ロット"], r["面"], r["製造品名"], r["製造年月日"],
                r["保証期限"], -r["保証差日数"]))
    else:
        L.append("該当なし（保証期限を超えて使用されたロットは見つからなかった）")
    L.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


# Sheet1 の C〜H 列に対応する。既存の調査結果ブック
# (調査結果/MT18EX5RM銅箔調査 (1).xlsx) のグラフ仕様に合わせてある。
CHARTS = [("剥離表", "引きはがし強さ　常態(表)"),
          ("剥離裏", "引きはがし強さ　常態(裏)"),
          ("剥離処理後表", "引きはがし強さ　処理後(表)"),
          ("剥離処理後裏", "引きはがし強さ　処理後(裏)"),
          ("絶縁煮沸後", "絶縁抵抗(煮沸後)"),
          ("絶縁常態", "絶縁抵抗(常態)")]


def _write_scatters(res, win, target_pos, outdir):
    """特性ごとに1枚ずつ散布図を出す。

    形は既存ブックのグラフをそのまま踏襲する（chart1〜12 はすべて scatterChart で、
    X が通し番号 B3:B52、Y が特性1列、そこへ対象の1点だけを別系列として重ねている）。
    横軸を超過日数にした相関図を勝手に作ったことがあるが、あれは別物で、ここで
    見たいのは「過去の実績の並びの中で、対象の1点がどこに来るか」である。

    母集団は「対象ロットを真ん中に置いた前後の実績」（同じ製造品名）。ここに
    期限超過63件を並べ、最後の点を対象として描いていた時期があるが、それは別物
    だった。並びの意味が違ううえ、対象が右端に来て「周囲のどこに位置するか」が
    まるで読めない。ブック側は最初から win / target_pos で描いていたので、
    同じ調査から出る2つの成果物が食い違っていた。母集団と対象位置は呼び出し元で
    1回だけ決めて、両方へ同じものを渡す。

    絶縁抵抗は桁が10^9〜10^15と広いので対数軸にする。線形のままだと大きい点以外が
    軸に張り付いて読めない。
    """
    _setup_font()
    made = []
    for key, title in CHARTS:
        pairs = [(i + 1, r[key]) for i, r in enumerate(win)
                 if isinstance(r.get(key), (int, float))]
        if not pairs:
            continue
        # 縦横比はブック側のグラフ（7.6 x 6.5 cm = 1.17）に合わせる。PNG だけ 1.67 の
        # 横長にしていた頃は、同じ調査の成果物なのに並べると別物に見えた。
        fig, ax = plt.subplots(figsize=(5.98, 5.12))
        tx = (target_pos + 1) if target_pos is not None else None
        others = [(x, v) for x, v in pairs if x != tx]
        target = [(x, v) for x, v in pairs if x == tx]
        ax.scatter([x for x, _ in others], [v for _, v in others], s=25, marker="o",
                   color="tab:blue", label="%s（前後 %d ロット）" % (title, len(win)))
        if target:
            ax.scatter([x for x, _ in target], [v for _, v in target], s=110, marker="s",
                       color="tab:orange", label="対象ロット（期限超過が最大）")
        elif tx is not None:
            # 対象の測定値が無いことがある。点を消すと「そこに無い」のか
            # 「測っていない」のか区別がつかないので、位置だけ残す。
            ax.axvline(tx, color="tab:orange", linestyle="--", linewidth=1.6,
                       label="対象ロット（この特性は測定値なし）")
        ax.set_xlim(0, len(win) + 1)
        if key.startswith("絶縁"):
            ax.set_yscale("log")
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=9)
        fig.tight_layout()
        path = os.path.join(outdir, "散布図_%s.png" % key)
        fig.savefig(path, dpi=120)
        plt.close(fig)
        made.append(os.path.basename(path))
    return made


def _write_png(res, path):
    _setup_font()
    fig, ax = plt.subplots(figsize=(8, 4.5))
    if res["count"]:
        labels = [k for k, _ in res["buckets"]]
        vals = [v for _, v in res["buckets"]]
        ax.bar(range(len(vals)), vals, color="tab:red", alpha=0.75)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=20)
        ax.set_ylabel("件数")
        for i, v in enumerate(vals):
            if v:
                ax.text(i, v, str(v), ha="center", va="bottom")
    else:
        ax.text(0.5, 0.5, "該当なし", ha="center", va="center", fontsize=20)
        ax.set_axis_off()
    ax.set_title("%s ： 保証期限の超過日数" % res["material"])
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


BUCKETS = ((1, 30, "1〜30日"), (31, 90, "31〜90日"), (91, 180, "91〜180日"),
           (181, 365, "181〜365日"), (366, 10 ** 9, "366日以上"))


def _neighbour_window(res, cursor):
    """散布図の母集団と、その中での対象ロットの位置を1回だけ決める。

    対象＝期限超過がいちばん大きいロット。母集団＝同じ製造品名の、対象を挟んだ
    前後（最大50、取れなければ取れるだけ）。対象は真ん中に来る。

    ここを2か所で別々に決めていたせいで、ブックは前後の実績・PNG は期限超過63件、
    と同じ調査から違う図が出ていた。決めるのはここだけにする。

    引けなかったときは (None, None) を返す。母集団が引けないことと、調査そのものが
    失敗したことは別なので、呼び出し元は他の成果物を出し切る。
    """
    if not res["rows"]:
        return None, None
    try:
        from charts import fetch_neighbours, pick_target, window_around

        target = pick_target(res["rows"])
        if target is None:
            return None, None
        lots = fetch_neighbours(cursor, target["製造品名"], target["製造年月日"])
        if not lots:
            return None, None
        win, pos = window_around(lots, target["製品ロット"])
        return win, pos
    except Exception as e:
        print("母集団を引けず: %s: %s" % (type(e).__name__, e), flush=True)
        return None, None


def _write_workbook(res, win, target_pos, outdir):
    """Excel ブックを出す。依頼の成果物は xlsx であって PNG ではない。

    母集団と対象位置は _neighbour_window が決めたものをそのまま使う。ここで別に
    引き直すと、散布図PNGとブックで違う図が出る（実際そうなっていた）。

    ここが失敗してもブック以外の成果物は残す。DB 側の都合で母集団が引けないとき、
    調査そのものまで道連れにする理由はない。
    """
    if not res["rows"] or not win:
        return None
    try:
        import workbook
        path = os.path.join(outdir, "%s銅箔調査.xlsx" % res["material"])
        return workbook.build(res, win, target_pos, path)
    except Exception as e:
        print("ブック出力を見送り: %s: %s" % (type(e).__name__, e), flush=True)
        return None


def main(material, from_date=DEFAULT_FROM, outdir=None):
    # 既定をカレントにしていたら、skill の中で走らせたときに成果物が束の中へ落ちた。
    # skill は中身のハッシュで信頼を判定するので、走らせるたびにハッシュが変わり、
    # 一度承認しても次の実行で承認が外れる。成果物は束の外へ出す。
    outdir = outdir or os.path.join(os.path.expanduser("~"), "Desktop", "銅箔調査")
    os.makedirs(outdir, exist_ok=True)
    cn = _connect()
    cu = cn.cursor()
    cf_rows, cached = load_cf(cu, material, from_date)
    print("銅箔検査 %d 件（%s）" % (len(cf_rows), "キャッシュ" if cached else "DBから取得"),
          flush=True)
    cu.execute(SQL.replace("{CF_VALUES}", _cf_values(cf_rows)))
    while cu.description is None:      # DECLARE/INSERT の分を読み飛ばす
        if not cu.nextset():
            break
    cols = [d[0] for d in cu.description]
    rows = [dict(zip(cols, r)) for r in cu.fetchall()]
    for r in rows:
        r["保証差日数"] = int(r["保証差日数"])
        r["経過日数"] = int(r["経過日数"])
        for k in list(r):
            if r[k] is None:
                r[k] = ""
            elif isinstance(r[k], (int, float)):
                continue          # 測定値は数値のまま。文字にすると散布図が描けない
            else:
                v = str(r[k]).strip()
                try:
                    r[k] = float(v) if k.startswith(("剥離", "絶縁")) and v else v
                except ValueError:
                    r[k] = v

    buckets = []
    for lo, hi, label in BUCKETS:
        buckets.append((label, sum(1 for r in rows if lo <= -r["保証差日数"] <= hi)))

    res = {
        "material": material,
        "from_date": from_date,
        "count": len(rows),
        "buckets": buckets,
        "worst": rows[0] if rows else None,
        "rows": rows,
    }
    with open(os.path.join(outdir, "銅箔調査.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, sort_keys=True)
    _write_md(res, os.path.join(outdir, "銅箔調査.md"))
    _write_png(res, os.path.join(outdir, "銅箔調査.png"))
    # 母集団と対象位置は1回だけ決めて、散布図とブックへ同じものを渡す
    win, target_pos = _neighbour_window(res, cu)
    if win:
        print("散布図の母集団 %d ロット / 対象は %d 番目" % (
            len(win), (target_pos + 1) if target_pos is not None else -1), flush=True)
    made = _write_scatters(res, win or [], target_pos, outdir)
    book = _write_workbook(res, win, target_pos, outdir)
    if book:
        print("ブック: %s" % os.path.basename(book), flush=True)
    print(json.dumps({k: res[k] for k in ("material", "from_date", "count", "buckets")},
                     ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: survey.py <資材略称> [検査開始日 YYYY-MM-DD] [出力先]")
        sys.exit(2)
    main(sys.argv[1],
         sys.argv[2] if len(sys.argv) > 2 else DEFAULT_FROM,
         sys.argv[3] if len(sys.argv) > 3 else None)
