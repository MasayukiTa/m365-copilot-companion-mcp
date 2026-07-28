# -*- coding: utf-8 -*-
"""設備点検記録Excelを読み、月次サマリを生成する。
引数: Excelパス1つ。同じ入力から必ず同じ出力（再現性）。
"""
import sys
import os
import json

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

# ==== 判定基準（規格）を定数として保持 ====
SPEC = {
    "温度(℃)":    (70.0, 76.0),
    "圧力(kPa)":  (100.0, 102.5),
    "流量(L/min)": (44.0, 46.0),
}
METRIC_COLS = ["温度(℃)", "圧力(kPa)", "流量(L/min)"]
TIME_COL = "時刻"


def _setup_font():
    for name in ["Yu Gothic", "MS Gothic", "Meiryo", "Noto Sans CJK JP"]:
        try:
            font_manager.findfont(name, fallback_to_default=False)
            plt.rcParams["font.family"] = name
            break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False


def main(path):
    path = os.path.abspath(path)
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]

    metrics = {}
    for col in METRIC_COLS:
        lo, hi = SPEC[col]
        vals = df[col].astype(float)
        oos = ((vals < lo) | (vals > hi)).sum()
        metrics[col] = {
            "min": round(float(vals.min()), 3),
            "max": round(float(vals.max()), 3),
            "mean": round(float(vals.mean()), 3),
            "spec_min": lo,
            "spec_max": hi,
            "out_of_spec": int(oos),
        }

    # 行単位の規格外れ判定（1つでも範囲外なら規格外れ）
    oos_rows = []
    for _, r in df.iterrows():
        reasons = []
        for col in METRIC_COLS:
            lo, hi = SPEC[col]
            v = float(r[col])
            if v < lo or v > hi:
                reasons.append("%s=%s（%s〜%s外）" % (col, v, lo, hi))
        if reasons:
            oos_rows.append({
                TIME_COL: str(r[TIME_COL]),
                "温度(℃)": float(r["温度(℃)"]),
                "圧力(kPa)": float(r["圧力(kPa)"]),
                "流量(L/min)": float(r["流量(L/min)"]),
                "理由": reasons,
            })

    result = {
        "file": path,
        "row_count": int(len(df)),
        "metrics": metrics,
        "out_of_spec_rows": oos_rows,
        "out_of_spec_count": len(oos_rows),
    }

    outdir = os.path.dirname(path)
    _write_md(result, os.path.join(outdir, "点検サマリ.md"))
    _write_png(df, os.path.join(outdir, "点検サマリ.png"))

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return result


def _write_md(result, mdpath):
    lines = []
    lines.append("# 点検月次サマリ")
    lines.append("")
    lines.append("- 対象ファイル: %s" % result["file"])
    lines.append("- 行数: %d" % result["row_count"])
    lines.append("- 規格外れ行数: %d" % result["out_of_spec_count"])
    lines.append("")
    lines.append("## 項目別統計")
    lines.append("")
    lines.append("| 項目 | 最小 | 平均 | 最大 | 規格下限 | 規格上限 | 規格外れ件数 |")
    lines.append("|---|---|---|---|---|---|---|")
    for col in METRIC_COLS:
        m = result["metrics"][col]
        lines.append("| %s | %s | %s | %s | %s | %s | %d |" % (
            col, m["min"], m["mean"], m["max"], m["spec_min"], m["spec_max"], m["out_of_spec"]))
    lines.append("")
    lines.append("## 規格外れ行")
    lines.append("")
    if result["out_of_spec_rows"]:
        lines.append("| 時刻 | 温度(℃) | 圧力(kPa) | 流量(L/min) | 理由 |")
        lines.append("|---|---|---|---|---|")
        for r in result["out_of_spec_rows"]:
            lines.append("| %s | %s | %s | %s | %s |" % (
                r[TIME_COL], r["温度(℃)"], r["圧力(kPa)"], r["流量(L/min)"], " / ".join(r["理由"])))
    else:
        lines.append("規格外れなし。")
    lines.append("")
    with open(mdpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_png(df, pngpath):
    _setup_font()
    x = list(range(len(df)))
    labels = [str(v) for v in df[TIME_COL]]
    fig, axes = plt.subplots(3, 1, figsize=(8, 9), sharex=True)
    for ax, col in zip(axes, METRIC_COLS):
        lo, hi = SPEC[col]
        ax.plot(x, df[col].astype(float), marker="o", color="tab:blue")
        ax.axhspan(lo, hi, color="tab:green", alpha=0.15)
        ax.axhline(lo, color="tab:green", linestyle="--", linewidth=0.8)
        ax.axhline(hi, color="tab:green", linestyle="--", linewidth=0.8)
        ax.set_ylabel(col)
        ax.grid(True, alpha=0.3)
    axes[-1].set_xticks(x)
    axes[-1].set_xticklabels(labels, rotation=45)
    axes[-1].set_xlabel(TIME_COL)
    fig.suptitle("点検時系列（緑帯=規格範囲）")
    fig.tight_layout()
    fig.savefig(pngpath, dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python summarize.py <Excelパス>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
