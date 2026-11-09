# periodic_scan スクリーニング・報告 入出力スキーマ（処理 (c)(d)）

本ファイルは、処理 (c)(新規スクリーニング) と (d)(報告生成) が共通基盤に依存せず
単独で動くための固定入出力スキーマを定義する。共通基盤 (`periodic_scan.ledger_io` /
`judge_wrapper` / `db_lots`) が揃えばそれらを介して結合できるが、本モジュール群は
それらが無くても合成データ（dict/JSON）だけで完結する。

判定器・基準・Δmin・補正・順序の信頼時計は変更しない。順序の偽装をしない。

## 1. LotRow（ロット明細の1行） — (c) スクリーニング入力
```json
{
  "lot": "74265",          // ロットID（文字列）
  "product": "P001",       // 層別キー
  "line": "L1",            // 製造ライン
  "date": "2026-08-01",     // 取得日（ISO, 時期の偏り評価に使う）
  "exposure": "treated",    // 曝露群: "treated" | "control"
  "value": 1.23             // 結果項目の測定値（数値）
}
```

## 2. ScreenInput — (c) `screen_result_item` の入力
```json
{
  "result_item": "指標値",      // 結果項目名
  "rows": [ LotRow, ... ],         // 上記 LotRow の配列
  "delta_min": 0.0,                // 既存基準の Δmin（呼び元が登録済み値を渡す。本モジュールは解釈のみ）
  "min_lots_per_group": 5          // 群ごと最小ロット数（既定5。基準は変更しない）
}
```

## 3. ConfoundReport — 交絡検出の出力
```json
{
  "result_item": "指標値",
  "by_product": { "P001": {"treated": 10, "control": 8}, ... },  // 層別キー×群のロット数
  "by_line":    { "L1": {"treated": 6, "control": 2}, ... },
  "by_period":  { "2026-08": {"treated": 7, "control": 1}, ... },
  "imbalance": {                 // 偏りの強さ 0..1（1=完全に偏る）
    "product": 0.42, "line": 0.61, "period": 0.80
  },
  "confounded": true,            // いずれかの imbalance が閾値超
  "confounders": ["line", "period"]
}
```

## 4. StrataEstimate — 層別キーで層別した推定の出力
```json
{
  "result_item": "指標値",
  "strata": [                    // 層別キーごと
    {"product": "P001", "n_treated": 10, "n_control": 8,
     "mean_treated": 1.20, "mean_control": 1.00, "delta": 0.20, "weight": 18}
  ],
  "pooled_delta": 0.18,          // 層別重み付き統合効果量（層別キーで揃えた比較）
  "n_total": 36,
  "usable_strata": 3             // 群ごと min_lots を満たした層の数
}
```

## 5. Candidate — (c) スクリーニングが返す候補、(preregister 入力)
```json
{
  "candidate_id": "screen-指標値-20261104",
  "result_item": "指標値",
  "pooled_delta": 0.18,
  "strength": 0.73,             // 強度スコア 0..1
  "strong": true,               // strength が閾値以上
  "confounded": true,
  "confounders": ["line", "period"],
  "strata": [ ... ],            // StrataEstimate.strata
  "observed": {                 // 何を見たか（報告でそのまま使う生値）
    "n_total": 36, "usable_strata": 3, "imbalance": {"product":0.42,"line":0.61,"period":0.80}
  },
  "missing": ["確認用ロットが各群 min_lots に未達の層がある"]  // 何が足りないか
}
```

## 6. PreregRecord — 事前登録レコード（preregister 出力・JSON Lines 追記）
```json
{
  "candidate_id": "screen-指標値-20261104",
  "result_item": "指標値",
  "claim": "treated 群は control 群より 指標値 が pooled_delta だけ高い（層別）",
  "pooled_delta": 0.18,
  "preregistered_at": "2026-11-04T00:00:00Z",
  "order_clock": {"source": "registered", "state": "initial"}  // 順序の信頼時計は登録済みを使う。新規は initial のみ。
}
```

## 7. DetectionRecord — (d) 報告生成の入力（採用 or 強い候補）
```json
{
  "analysis_id": "screen-指標値-20261104",
  "kind": "strong_candidate",     // "adopted"（順序成立の効果あり採用） | "strong_candidate"
  "result_item": "指標値",
  "what_we_saw": [ "..." ],        // 何を見てそう判断したか（文字列配列。数値は本実行の計算/読み取り値のみ）
  "whats_missing": [ "..." ],      // 何が足りないか
  "numbers": { "pooled_delta": 0.18, "n_total": 36, "strength": 0.73 },  // 本実行で算出した数値のみ
  "strata": [ ... ],
  "proposal": {                    // 改善の提案
    "change": "...",               // どの条件を変えるとよいか
    "experiment": "...",           // 確かめる実験の案
    "confirm_design": "..."        // 確認の設計
  },
  "tracking": {                    // 台帳へ加える追跡項目（LedgerEntry に変換される）
    "next_trigger": "確認用ロットが各群 5 に達したら判定器に通す",
    "class": "C"
  }
}
```

## 8. LedgerEntry — 台帳.json の1エントリ（ledger_append 出力）
```json
{
  "analysis_id": "screen-指標値-20261104",
  "class": "C",
  "next_trigger": "確認用ロットが各群 5 に達したら判定器に通す",
  "last_scanned": "2026-11-04"
}
```
