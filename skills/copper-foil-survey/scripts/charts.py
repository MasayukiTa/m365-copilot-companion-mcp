# -*- coding: utf-8 -*-
"""期限超過が最大のロットを中心に、同じ製品品名の実績を並べて特性ごとの散布図を作る。

既存の調査結果ブック（調査結果/*.xlsx の Sheet1 と chart1〜12）に合わせてある:
  - 特性ごとに1枚。1枚に複数特性を重ねない
  - X は通し番号、Y はその特性だけ
  - 対象の1点だけ別系列にして大きめの四角で描く

対象は「期限超過がいちばん大きいロット」。それを真ん中に置き、同じ製品品名の
ロットで前後を埋めて最大50点にする。50に満たなければ取れるだけでよく、対象が
端に来ることもある（実例あり: 3EC-VLP-18RM は本体9点で対象が最後尾、しかも
測定値が空だった）。母数が少ないほど1点の意味は弱くなるので、点数は必ず図に出す。
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

WINDOW = 50

# Sheet1 の C〜H 列に対応
CHARTS = [("剥離表", "引きはがし強さ　常態(表)"),
          ("剥離裏", "引きはがし強さ　常態(裏)"),
          ("剥離処理後表", "引きはがし強さ　処理後(表)"),
          ("剥離処理後裏", "引きはがし強さ　処理後(裏)"),
          ("絶縁煮沸後", "絶縁抵抗(煮沸後)"),
          ("絶縁常態", "絶縁抵抗(常態)")]

# SPC 本体 (SEI040.exe) が使っているのと同じ経路。SPC は同じ問いに数秒で答えるので、
# 速さの根拠はそこにある。要点は3つで、いずれも実行ファイルから読み出したもの:
#   - 起点は SPC_SEI_HIN_JSK を SEIHIN_HINME で絞り TOP N + SEIZO_DT DESC
#     （UIの「最新30件/50件/100件…」がこの N。既存ブックの50点はこの「最新50件」）
#   - 測定値は SPC_SEI_KENSA_KEKKA に INDEX ヒントを付けて引く
#   - 結合キーは HINME + LOT_NO + LOT_Y の3列。ロット番号だけでは一意にならない
# V_製品検査データ を製造品名で全走査すると同じ答えに100秒かかる。使わないこと。
# 母集団は対象ロットを挟んだ前後。前と後ろを別々に取るのは、片側が足りないときに
# もう片側から補うため（対象が最初や最後のロットということが実際にある）。
# どちらも製造日の範囲シークなので速い。
BEFORE_SQL = """
SET NOCOUNT ON;
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SELECT TOP (?) RTRIM(HINME) HINME, RTRIM(LOT_NO) LOT_NO, RTRIM(LOT_Y) LOT_Y, SEIZO_DT
FROM SPC_SEI_HIN_JSK WITH(NOLOCK)
WHERE SEIHIN_HINME = ? AND SEIZO_DT IS NOT NULL AND SEIZO_DT <= ?
ORDER BY SEIZO_DT DESC, LOT_NO DESC, LOT_Y DESC;
"""

AFTER_SQL = """
SET NOCOUNT ON;
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SELECT TOP (?) RTRIM(HINME) HINME, RTRIM(LOT_NO) LOT_NO, RTRIM(LOT_Y) LOT_Y, SEIZO_DT
FROM SPC_SEI_HIN_JSK WITH(NOLOCK)
WHERE SEIHIN_HINME = ? AND SEIZO_DT IS NOT NULL AND SEIZO_DT > ?
ORDER BY SEIZO_DT ASC, LOT_NO ASC, LOT_Y ASC;
"""

# 試験項目は日本語名ではなくコードで持つ。名前は表記ゆれがあるが
# コードは品名ごとのマスタに定義されている。
ITEMS_SQL = """
SELECT RTRIM(SIKEN_KO_C) SIKEN_KO_C, RTRIM(SKN_ITM_JPN) 項目
FROM SPC_SEI_MASTER_SYOSAI WITH(NOLOCK)
WHERE SEIHIN_HINME = ?;
"""

VALUES_SQL = """
SET NOCOUNT ON;
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SELECT RTRIM(HINME) HINME, RTRIM(LOT_NO) LOT_NO, RTRIM(LOT_Y) LOT_Y,
       RTRIM(SIKEN_KO_C) SIKEN_KO_C, SKNT
FROM SPC_SEI_KENSA_KEKKA WITH(NOLOCK, INDEX(IX_SPC_SEI_KENSA_KEKKA))
WHERE HINME = ? AND LOT_Y IN (%s) AND LOT_NO IN (%s) AND SKNT IS NOT NULL;
"""

# Sheet1 の C〜H に対応する項目名。マスタ側の表記に合わせて前方一致で拾う。
WANTED = [("剥離表", "引きはがし強さ(表)"), ("剥離裏", "引きはがし強さ(裏)"),
          ("剥離処理後表", "引きはがし強さ　処理後(表)"),
          ("剥離処理後裏", "引きはがし強さ　処理後(裏)"),
          ("絶縁煮沸後", "絶縁抵抗(煮沸後)"), ("絶縁常態", "絶縁抵抗(常態)")]


def pick_target(rows):
    """期限超過がいちばん大きい行。survey の並びは既に超過の大きい順だが、
    呼び出し側の都合で並べ替えられても壊れないよう、ここで明示的に選ぶ。"""
    cand = [r for r in rows if isinstance(r.get("保証差日数"), int)]
    return min(cand, key=lambda r: r["保証差日数"]) if cand else None


def window_around(all_rows, target_lot, size=WINDOW):
    """対象を真ん中に置いて前後を埋める。端に寄ったら反対側から補う。"""
    idx = next((i for i, r in enumerate(all_rows) if r["ロットNO"] == target_lot), None)
    if idx is None:
        return all_rows[:size], None
    half = size // 2
    lo = max(0, idx - half)
    hi = min(len(all_rows), lo + size)
    lo = max(0, hi - size)          # 後ろが足りなければ前へ伸ばす
    win = all_rows[lo:hi]
    return win, idx - lo


def fetch_neighbours(cursor, seihin_hinmei, target_date, size=WINDOW):
    """対象の製造日を挟んで前後のロットを、合計 size 件まで取る。

    前半分・後半分を狙うが、片側が足りなければもう片側から埋める。期間の指定は
    しない（対象が最初のロットでも最後のロットでも、取れるものを取るだけ）。
    """
    half = size // 2
    def q(sql, n):
        cursor.execute(sql, n, seihin_hinmei, target_date)
        return [{"HINME": r[0], "ロットNO": r[1], "LOT_Y": r[2], "製造年月日": r[3]}
                for r in cursor.fetchall()]
    before = q(BEFORE_SQL, half + 1)         # 対象自身を含む
    after = q(AFTER_SQL, size - len(before))
    if len(before) + len(after) < size:      # 後ろが足りない分を前へ伸ばす
        before = q(BEFORE_SQL, size - len(after))
    before.reverse()                          # 古い順
    lots = before + after
    if not lots:
        return []
    cursor.execute(ITEMS_SQL, seihin_hinmei)
    code_of = {}
    for code, name in cursor.fetchall():
        for key, want in WANTED:
            if (name or "").strip() == want:
                code_of[code] = key
    # 取得した50ロットだけに絞る。HINME だけで引くと全年度が対象になり返らない。
    hinme = lots[0]["HINME"]
    years = sorted({r["LOT_Y"] for r in lots})
    lotnos = sorted({r["ロットNO"] for r in lots})
    sql = VALUES_SQL % (",".join("?" * len(years)), ",".join("?" * len(lotnos)))
    cursor.execute(sql, hinme, *years, *lotnos)
    vals = {}
    for h, lot, ly, code, sknt in cursor.fetchall():
        key = code_of.get(code)
        if key is None or sknt is None:
            continue
        try:
            v = float(sknt)
        except (TypeError, ValueError):
            continue
        if v <= -100:          # 負値は測定値ではなくコード
            continue
        vals.setdefault((lot, ly), {})[key] = v
    for r in lots:
        r.update(vals.get((r["ロットNO"], r["LOT_Y"]), {}))
    return lots


def draw(win, target_pos, seihin_hinmei, outdir, setup_font):
    setup_font()
    made = []
    for key, title in CHARTS:
        ys = [r.get(key) for r in win]
        if not any(v is not None for v in ys):
            continue
        xs = [i + 1 for i, v in enumerate(ys) if v is not None]
        vs = [v for v in ys if v is not None]
        fig, ax = plt.subplots(figsize=(7, 4.2))
        ax.scatter(xs, vs, s=25, marker="o", color="tab:blue",
                   label="%s (n=%d)" % (title, len(vs)))
        if target_pos is not None and ys[target_pos] is not None:
            ax.scatter([target_pos + 1], [ys[target_pos]], s=110, marker="s",
                       color="tab:orange", zorder=3, label="期限超過が最大のロット")
        elif target_pos is not None:
            # 対象に測定値が無いことは実際に起きる。黙って消すと「無い」ことが
            # 伝わらないので、位置だけ縦線で残す。
            ax.axvline(target_pos + 1, color="tab:orange", linestyle="--",
                       label="対象ロット（測定値なし）")
        # X は 0 から 点数+1 まで。既存グラフがそうなっており、両端に余白が入って
        # 最初と最後の点が軸に貼り付かない。Y は matplotlib の自動調整に任せる
        # （既存も特性ごとに範囲が違い、固定値ではない）。
        ax.set_xlim(0, len(win) + 1)
        if key.startswith("絶縁"):
            ax.set_yscale("log")
        ax.set_title("%s ／ %s" % (title, seihin_hinmei))
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        path = os.path.join(outdir, "散布図_%s.png" % key)
        fig.savefig(path, dpi=120)
        plt.close(fig)
        made.append(os.path.basename(path))
    return made
