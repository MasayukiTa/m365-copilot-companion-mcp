"""Aggregate a monthly sales CSV by customer and by product category (reference script)."""
import sys

import pandas as pd


def main(path, out):
    df = pd.read_csv(path, encoding="cp932")
    by_customer = df.groupby(["得意先コード", "得意先名"], as_index=False)["売上金額"].sum()
    by_product = df.groupby("商品分類", as_index=False)["売上金額"].sum()
    by_product["構成比"] = by_product["売上金額"] / by_product["売上金額"].sum()
    with pd.ExcelWriter(out) as xw:
        by_customer.to_excel(xw, sheet_name="得意先別", index=False)
        by_product.to_excel(xw, sheet_name="商品別", index=False)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
