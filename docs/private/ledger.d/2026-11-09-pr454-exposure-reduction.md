## 2026-11-09 - PR #454 露出削減（テストの合成フィクスチャ化 + denylist 除去）

**目的 (purpose).** 公開リポジトリの PR #454 ブランチから、テスト内のビジネス固有のサンプルケース・名称・データセットを除去し、禁止語リスト（正規表現）に一致する導入行をゼロにする。

**手法 (method).** 本番チェックアウトに触れず、`%TEMP%` 配下の隔離クローン（`agents/judgeclean/pr454`）で作業。テスト内のビジネス固有の製品コード・定数・ラベルを、テスト内で生成する合成の汎用フィクスチャ（トークン名を非禁止語の汎用語へ）に置換。統計的アサーション（効果区間・HOLD/GO 判定・ランクゲート）はそのまま維持。origin/main 最新先端に rebase。

**対象 (scope).** `tools/test_validity_judge.py`、`tools/test_validity_judge_exploratory_hold.py`、`tools/test_validity_judge_s5_rank.py`、`tools/test_validity_judge_s5_require_evidence.py`、`tests/test_validity_harness.py`、`relay/test_judge_gate_harness.py` ほか PR #454 差分のテスト群。

**実測 (results).**
- rebase 後の `origin/main...HEAD` 差分の追加行（`+`）を denylist 全パターンで照合 = **一致 0 件**。除去行（`-`）側にのみ旧禁止語の除去を確認。
- 対象テスト 136 passed（統計的アサーション維持を確認）。
- `--force-with-lease` で feature ブランチのみ push（main への push・merge は行わない）。

**固定条件の遵守 (constraints).** denylist パターンは一切出力せず、件数・インデックスでのみ扱った。H:/SharePoint へは書かず、rustup/cargo は実行していない。認証情報・社員 ID は記載していない。

**未決 (open).** push 後の CI `test` ジョブは実行中（他のチェックは pass）。クローンは作業後に削除する。
