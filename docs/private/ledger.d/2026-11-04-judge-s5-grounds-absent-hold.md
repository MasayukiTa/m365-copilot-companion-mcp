## 2026-11-04 - §5-5 根拠欠落バイパスを既定HOLD化（PR #454）

**目的 (purpose).** PR #453（commit f9a9884d）に残っていた未解決のバイパスの修正。
監査 `統合修正_独立監査_20261104.md` の指摘どおり、`tools/validity_judge.py::decide()` は
旧5引数呼び出し（`evidence=None`、`require_evidence=False` 既定）で §5-5 評価ランクゲートを
暗黙にスキップし、根拠未指定のまま GO に到達しえた。正本 `ツール依存可能な主張一覧.md`
(sha256 14ff6bbc3cb9e1a7bec347721b0b24441c6e5c195ae0d904156015fe98788f88) line 4 /
§5 item 5 (line 157) は「根拠が無ければ最大でも保留」「D群・台帳B以下だけの根拠は既定HOLD」
を要求する。よって根拠未指定も既定 HOLD でなければならない。

**手法 (method).** 本番ツリーに触れず、独立 detached worktree
`judge_s5_absent_w341`（branch `fix/judge-s5-grounds-absent-hold-w341`、f9a9884d 起点）で作業。
`require_evidence` の既定を False→True（フェイルクローズ）へ変更。根拠未指定
（None / {} / {claims:[]}）は `R_EVIDENCE_UNKNOWN` で HOLD に落ちる。純数値経路のみを
試す単体テストは `require_evidence=False` を**明示**して意図を保持（opt-out は呼出箇所で可視）。

**実測 (results).** 強正効果区間で evidence だけを変えた合成回帰:
旧5引数/None/空→HOLD(R_EVIDENCE_UNKNOWN)、D群のみ→R_ONLY_DIAGNOSTIC_D、
台帳B以下→R_ONLY_LEDGER_B_OR_BELOW、適格C群台帳A→GO(EFFECT)維持、明示opt-out→GO維持。
§5スイート 68 tests pass（純数値GOテスト4件を明示opt-outへ更新、back-compatテストは
新フェイルクローズ既定の確認へ書き換え）。F821・CIマニフェスト・no-identifying-names・
CRLF(index=lf) 通過。

**考察・未決 (discussion / open).**
- `decide()` を import するのはテストのみ。運用エントリ `relay/judge_gate.py::run_engine_judge`
  は外部エンジンCLIへ委譲する別実装で、本純ロジック層とは未接続。よって本修正は運用判定の
  挙動を直接は変えない。
- **外部エンジン接続は未確認**。外部エンジン側が §5-5 の既定HOLDと等価にフェイルクローズ
  しているかは本作業では未検証 → 別タスク要。運用可能とは断定しない。
- 実データには未適用（合成データのみ）。運用投入前に外部エンジン経路での等価性確認が必要。
- 本番ツリー `judge_integrate_mtk1104` は f9a9884d のまま無変更を確認（未コミット変更を消して
  いない）。H:/SharePoint へは書かず、認証情報・社員ID は記載していない。PowerPoint 不操作。
