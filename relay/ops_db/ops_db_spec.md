# ops DB — 段階1 仕様書

運用 DB(ops DB)は、フリートの運用 KPI を単一の SQL クエリで日・経路・
リクエスト単位に算出するための、独立した構造化 SQLite データベースである。
セッション DB など既存の入力は **読み取り専用** として扱い、改変も複製もしない。

## 1. 原則

- タイムスタンプは UNIX 秒(UTC)を REAL で保持する。JST 表示は保存せず、
  ビュー側で `date(ts + 32400, 'unixepoch')`(+9時間して暦日)で導出する。
- 欠損は欠損のまま。推測で埋めない。各行は `source`(既定 `backfill`)で
  由来を記録する。
- スキーマは `PRAGMA user_version` と `schema_migrations` で版管理し、
  新規作成も既存更新も `migrations.py` の同じ経路を通る。
- バックフィルは冪等(再実行で重複しない)。入力はストリーム処理で
  メモリ有界。

## 2. 識別子モデル

- **run_key**(runs の主キー)= `"{run_started}#{name}"`。
  1 フリート実行中の 1 ワーカーを一意に表す。
- **fleet_exec_id** = 共有される `rXXXX_aN` 識別子(複数 run が共有しうる)。
- requests は `jid` または goal のハッシュ(`goal_hash`)で一意化する。

## 3. テーブル(schema.py)

`schema_migrations, reason_codes, requests, runs, turns, messages,
tool_calls, infra_events, heartbeats, interventions, daily_rollup`、
および FTS5(`requests_fts, runs_fts`)。

`reason_codes` は結果コードの次元表:
`INFRA_STUCK=infra, STUCK=transient, VERIFY_FAILED/EVIDENCE_CONTRADICTED=quality,
REFUSED=refusal, CANCELLED=user(自動取消は system), FANOUT=system, DONE=ok`。

## 4. 入力 → テーブル対応(backfill.py)

| 入力 | テーブル |
|---|---|
| `archive/history_completed_*.jsonl.gz` | runs, requests, interventions |
| `sessions.fleet_turns`(ro) | turns.wait_s, messages, runs.conv_guid, runs.refuter_verdict |
| `socket_route.jsonl` | turns(turn_end outcome) |
| `tool_events.jsonl(.1)` | tool_calls |
| `consumer_watch.log` | infra_events(detected→recovered) |
| `campaigns.jsonl` | runs.campaign_id |

`load_history_latest` は `(run_started, name)` ごとに最新 `seq` の記録を
採用し、既知の走行数 **2747** を再現する。

sessions ステップは補助(messages / wait_s / refuter)であり、ライブ DB が
書き込み中でも中核 KPI を止めないよう `run_backfill` で例外を握りつぶして
続行する(`sqlite3.DatabaseError` を捕捉)。

## 5. KPI ビュー(views.py)

- `v_infra_stop_rate` — 日ごとの INFRA_STUCK 率。
- `v_incident_recovery` — detected→recovered 秒と、その中央値(スカラ)。
- `v_request_final_completion` — リクエスト最終完了率(いずれかの run が DONE)。
  CANCELLED/FANOUT のみのリクエストは分母から除外。
- `v_turn_error_rate` — 日ごとの turn の completed/error/total。
- `v_manual_intervention_rate` — 日ごとの手動(user)取消率。
- `v_availability` — heartbeats からの稼働率。
- `daily_rollup` — 日・経路の集計(`rebuild_daily_rollup` で再構築)。

## 6. 実行

```
python -m relay.ops_db.backfill --out <ops.sqlite3> \
    [--fleet-dir <.fleet>] [--sessions <sessions.sqlite3>]
```

完了時に `PRAGMA foreign_key_check` で整合性を検証し、違反があれば失敗する。
