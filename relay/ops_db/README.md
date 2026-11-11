# ops_db — 運用メトリクス DB(段階1)

フリートの運用 KPI(インフラ停止率・自動復旧時間・リクエスト最終完了・
turn 完了・稼働率・手動介入)を、**単一の SQL クエリ**で日・経路・
リクエスト単位に算出するための、独立した構造化 SQLite データベース。
既存のセッション DB など入力は **読み取り専用** として扱い、改変も複製も
しない。

## 設計原則

- タイムスタンプは UNIX 秒(UTC)を REAL で保持。JST 表示・日次バケットは
  保存せず、ビュー側で `date(ts + 32400, 'unixepoch')`(+9時間して暦日)で
  導出する(第2の時刻を持たない)。
- 欠損は欠損のまま。推測で埋めない。各行は `source`(既定 `backfill`)で
  由来を記録する。
- スキーマは `PRAGMA user_version` と `schema_migrations` で版管理。新規作成も
  既存更新も `migrations.py` の同じ経路を通る(`CREATE TABLE IF NOT EXISTS`
  だけに依存しない)。
- バックフィルは冪等(再実行で重複しない)。入力はストリーム処理でメモリ有界。
- `connect()` は仕様のプラグマ(WAL / busy_timeout=5000 / foreign_keys=ON)を
  適用し、最新スキーマ版まで移行する。

## モジュール構成

| ファイル | 役割 |
|---|---|
| `__init__.py` | パッケージ定義 / `SCHEMA_REVISION` |
| `schema.py` | DDL(テーブル・索引・FTS5・reason_codes 種) |
| `migrations.py` | 版管理・`connect()`・冪等マイグレーション |
| `views.py` | KPI ビュー(SQL) |
| `backfill.py` | 読み取り専用入力からの投入 |
| `verify_known_values.py` | 実アーカイブで既知値を再現する検証(凍結対応) |
| `test_ops_db_stage1.py` | pytest(段階1 契約) |
| `ops_db_spec.md` | 仕様書(詳細) |

## 識別子モデル

- **run_key**(`runs` 主キー)= `"{run_started}#{name}"`。
  1 フリート実行中の 1 ワーカーを一意に表す。
- **fleet_exec_id** = 共有される `rXXXX_aN` 識別子(複数 run が共有しうる)。
- `requests` は `jid` または goal のハッシュ(`goal_hash`)で一意化する。
- 子テーブル(`turns` / `messages` / `tool_calls`)は `run_key` を参照する。

## テーブル

`schema_migrations, reason_codes, requests, runs, turns, messages,
tool_calls, infra_events, heartbeats, interventions, daily_rollup`、
および FTS5(`requests_fts, runs_fts`)。

`reason_codes` は結果コードの次元表:

| code | category |
|---|---|
| INFRA_STUCK | infra |
| STUCK | transient |
| VERIFY_FAILED / EVIDENCE_CONTRADICTED | quality |
| REFUSED | refusal |
| CANCELLED | user(自動取消は system) |
| FANOUT | system |
| DONE | ok |

## 入力 → テーブル対応(backfill.py)

| 入力 | テーブル |
|---|---|
| `archive/history_completed_*.jsonl.gz` | runs, requests, interventions |
| `sessions.fleet_turns`(ro) | turns.wait_s, messages, runs.conv_guid, runs.refuter_verdict |
| `socket_route.jsonl` | turns(turn_end outcome) |
| `tool_events.jsonl(.1)` | tool_calls |
| `consumer_watch.log` | infra_events(detected→recovered) |
| `campaigns.jsonl` | runs.campaign_id |

`load_history_latest` は `(run_started, name)` ごとに最新 `seq` の記録を
採用し、既知の走行数 **2747** を再現する。sessions ステップは補助
(messages / wait_s / refuter)であり、ライブ DB が書き込み中でも中核 KPI を
止めないよう `run_backfill` で例外を握りつぶして続行する。

## KPI ビュー(views.py)

- `v_infra_stop_rate` — 日ごとの INFRA_STUCK 率。
- `v_incident_recovery` — detected→recovered 秒と、その中央値(スカラ)。
- `v_request_final_completion` — リクエスト最終完了率(いずれかの run が DONE)。
  CANCELLED/FANOUT のみのリクエストは分母から除外。
- `v_turn_error_rate` — 日ごとの turn の completed/error/total。
- `v_manual_intervention_rate` — 日ごとの手動(user)取消率。
- `v_availability` — heartbeats からの稼働率。
- `daily_rollup` — 日・経路の集計(`rebuild_daily_rollup` で再構築)。

## 実行

```
# バックフィル
python -m relay.ops_db.backfill --out <ops.sqlite3> \
    [--fleet-dir <.fleet>] [--sessions <sessions.sqlite3>]

# テスト
python -m pytest relay/ops_db/test_ops_db_stage1.py -q

# 既知値の再現(凍結一致)
python -m relay.ops_db.verify_known_values \
    --fleet-dir <.fleet> --out <scratch.sqlite3> --frozen
```

完了時に `PRAGMA foreign_key_check` で整合性を検証し、違反があれば失敗する。

## 既知値との一致(段階1 固定条件)

実 `.fleet` アーカイブで再現。turn_end の出典 `socket_route.jsonl` は追記中の
ライブファイルのため、計測時点カットオフ **ts ≤ 1793945651.674643**
(2026-11-06 15:14:11 JST; `--frozen`)で窓を固定して凍結一致を取る。

| 指標 | 再現値 | 既知値 |
|---|---|---|
| 走行 (runs) | 2747 | 2747 |
| INFRA_STUCK | 27 | 27 |
| DONE | 1707 | 1707 |
| 取消 (CANCELLED) | 118 | 118 |
| 自動復旧 件数 / 中央値 | 7 / 41秒 | 7 / 41秒 |
| turn_end total (11/2〜11/6 JST) | 113880 | 113880 |
| turn_end completed | 113545 | 113545 |
| turn_end error | 335 | 335 |

全指標が凍結一致(外部キー違反 0)。詳細は `ops_db_stage1_報告.md` を参照。
