# ops DB 段階1 実装報告

## 成果物

`relay/ops_db/` パッケージ:

| ファイル | 役割 |
|---|---|
| `__init__.py` | Package initialization |
| `schema.py` | DDL(テーブル・索引・FTS・reason_codes 種) |
| `migrations.py` | `user_version` 版管理・`connect()`・冪等マイグレーション |
| `views.py` | KPI ビュー(infra 停止率・復旧・最終完了・turn 誤り率・手動介入率・稼働率) |
| `backfill.py` | 読み取り専用入力からの投入(冪等・メモリ有界) |
| `ops_db_spec.md` | 仕様書 |
| `test_ops_db_stage1.py` | pytest(14件) |
| `verify_known_values.py` | 実アーカイブで既知値を再現する検証スクリプト(凍結対応) |
| `ops_db_stage1_報告.md` | 本報告 |

## 識別子モデル

- `runs` 主キー = **run_key** = `"{run_started}#{name}"`(1 実行の 1 ワーカー)。
- 共有 `rXXXX_aN` は **fleet_exec_id** に分離。
- 子テーブル(turns / messages / tool_calls)は run_key を参照。

## テスト

```
python -m pytest relay/ops_db/test_ops_db_stage1.py -q
14 passed
```

## 既知値との一致(凍結再現)

実 `.fleet` アーカイブで `run_backfill` を実行。turn_end の出典
`socket_route.jsonl` は追記中のライブファイルのため、計測時点でカットオフを
固定する(凍結)。凍結カットオフ **ts ≤ 1793945651.674643**
(2026-11-06 15:14:11 JST)。再現コマンド:

```
python -m relay.ops_db.verify_known_values \
    --fleet-dir <.fleet> --out <scratch.sqlite3> --frozen
```

| 指標 | 再現値(凍結) | 既知値 | 判定 |
|---|---|---|---|
| 走行 (runs) | 2747 | 2747 | ✅ |
| INFRA_STUCK | 27 | 27 | ✅ |
| DONE | 1707 | 1707 | ✅ |
| 取消 (CANCELLED) | 118 | 118 | ✅ |
| 自動復旧 件数 | 7 | 7 | ✅ |
| 復旧 中央値 | 41 秒 | 41 秒 | ✅ |
| turn_end total (11/2〜11/6 JST) | 113880 | 113880 | ✅ |
| turn_end completed | 113545 | 113545 | ✅ |
| turn_end error | 335 | 335 | ✅ |
| 外部キー違反 | 0 | — | ✅ |

**全指標が既知値に完全一致(凍結一致)。** 凍結カットオフを外した場合
(ライブ)の turn_end は計測後に走行が増えるため total が増加するが、同一
カットオフを与えれば決定的に 113880/113545/335 を再現する。

## 備考

- sessions(messages / wait_s / refuter)は補助であり、ライブ DB が巨大かつ
  書き込み中でタイムアウトしうるため中核 KPI 検証では省略。`run_backfill` は
  sessions 失敗を握りつぶして中核を完走させる。
- 日付切りは rollup と同じ **JST(+9h)** に統一。
