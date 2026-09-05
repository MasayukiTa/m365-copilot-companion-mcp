# 設定リファレンス（`.env`）

`.env` の全キーの意味と既定値をまとめます。全体像は [README](../README.md) を先に読んでください。設定値は全部 `.env`（git 管理外）に集約されます。テンプレートは `.env.example`。

---

## `.env` は自分で編集しなくてよい

`quickstart.bat` / `setup.bat` が秘密を自動生成し、`configure_env.bat` のダイアログがエージェント URL を書き込むので、通常は手で `.env` を触る必要はありません。ここは中身を理解したい人向けのリファレンスです。

---

## 全キー一覧

| 変数 | 意味 | 既定 / 設定方法 |
|---|---|---|
| `MCP_API_KEY` | MCP サーバーへの Bearer 認証キー（読取系ツールに必要）。40 桁のランダム hex | 自動生成。Copilot Studio に貼る |
| `MCP_UNLOCK_PASSWORD` | 書込・実行系ツールの IP 単位ロック解除パスワード | 自動生成。`unlock(password=...)` で使う |
| `MCP_UNLOCK_TTL_DAYS` | unlock した IP を信頼し続ける日数 | `30` |
| `MCP_ALLOWED_BASE` | エージェントがアクセスできるフォルダの上限。`~` = ホーム全体。`~/work` 等に絞ると隔離が固くなる | `~` |
| `MCP_TOOL_MAP` | map mode の有効化。Copilot Studio では **必ず `1`** | `1` |
| `MCP_TOOL_MAP_MAX` | map mode で先頭に登録する高価値ツールの数。**公開ツール総数ではない**（下記） | `8` |
| `MCP_TOOL_MAP_INCLUDE` | map mode で追加で第一級ツールとして載せたいツール名（カンマ区切り） | 空 |
| `MCP_IMPL_AGENT_URL` | bridge / fleet が駆動する主 Copilot エージェントの URL（テナント固有 `T_…`） | 手動で貼る（**必須**） |
| `MCP_FLEET_AGENT_URL` | fleet 専用エージェント URL | 未指定なら `MCP_IMPL_AGENT_URL` |
| `MCP_REVIEW_P2C` | 深掘りレビュー `/deep-review` `/deep-security-review` の表示・実行と保証レベル | `0`（無効）/ `1`（深掘り）/ `2`（フル検証） |
| `MCP_EXECUTION_PROFILES` | 回答本文非依存の実験的 `LOCAL_LOOP` MCPツールを登録 | `0`（無効）/ `1`（有効） |
| `MCP_LOCAL_JOB_DB` | `LOCAL_LOOP` のSQLite状態ストア | 未指定なら `.jobs/jobs.sqlite3` |
| `MCP_DEEP_REVIEW_TRANSPORT` | Deep Reviewの輸送方式。`auto`は実行プロファイル有効時にLOCAL_LOOP | `auto` / `local_loop` / `fleet` |
| `MCP_LOCAL_REVIEW_MAX_CONCURRENT` | LOCAL_LOOP Deep Reviewの同時会話数 | `2` |
| `MCP_LOCAL_ROTATE_AFTER_TURNS` | 新しい会話へ切り替えるターン数 | `3` |
| `MCP_LOCAL_EDGE_MB_LIMIT` | 会話切替を促すcompanion Edge使用量（MB） | `1400` |
| `MCP_RESEARCHER_AGENT_URL` | `/research` が使う調査エージェント（Researcher `…dr_work`） | 内蔵既定・通常は空 |
| `MCP_ANALYST_AGENT_URL` | `/analyze` が使う分析エージェント（Analyst `…diceberry`） | 内蔵既定・通常は空 |
| `MCP_CDP_URL` | 専用 Edge の CDP エンドポイント | `http://localhost:9222` |
| `MCP_CDP_PORT` | 専用 companion Edge の CDP ポート | `9222` |
| `MCP_BRIDGE_PORT` | bridge チャット UI のポート | `8765` |
| `MCP_TUNNEL_NAME` | devtunnel のトンネル名 | `setup_devtunnel.ps1` が記録 |
| `MCP_TUNNEL_URL` | devtunnel の公開 URL | `setup_devtunnel.ps1` が記録 |
| `MCP_DB_<NAME>` | 名前付き ODBC 接続文字列（社内 DB を使うなら追記） | 任意。[ADVANCED.md](ADVANCED.md) |

---

## エージェント URL について

- **実質「メイン エージェント」（`MCP_IMPL_AGENT_URL`）だけ貼れば動きます。** リサーチ用とアナリスト用は Microsoft 第一者エージェント（全ユーザー共通）で、コード側（`relay/agent_profiles.py`）に既定 URL を内蔵しているので通常は設定不要です。
- リサーチとアナリストは互いに**別 URL**（別エージェント）で、同じにはなりません。
- 自分のテナントで既定 URL のエージェントが開けなかった場合は、`/research` や `/analyze` 実行時に**「正しい URL を貼り付けてください」というダイアログが自動で開く**ので、M365 Copilot のアドレスバーの URL を貼れば `.env` に保存されます（＝**既定値で試し、つながらなければダイアログ**）。

**URL の取り方:** M365 Copilot（`https://m365.cloud.microsoft/chat`）を開き、対象エージェントを選んでチャットを開始したときの **URL バーの URL** をコピーします。

---

## `.env` に自動で書き込まれるキー（あなたは触らなくてよい）

セットアップは次を書き込みます:

- `MCP_API_KEY`・`MCP_UNLOCK_PASSWORD`（`quickstart`/`setup` が乱数生成）
- `MCP_UNLOCK_TTL_DAYS`・`MCP_ALLOWED_BASE`・`MCP_TOOL_MAP`／`MCP_TOOL_MAP_MAX`（テンプレートからそのままコピー）
- 続いて Dev Tunnel ステップ（`setup_devtunnel.ps1`）が `MCP_TUNNEL_NAME`・`MCP_TUNNEL_URL` を追記
- エージェント URL ダイアログ（`configure_env`）が `MCP_IMPL_AGENT_URL` などの各エージェント URL を追記

**これら以外（bridge / relay / ODBC などの任意項目）は、あなたが自分で有効化するまで `.env` 内でコメントアウトされたまま**です。

---

## map mode（`MCP_TOOL_MAP`）を必ず ON にする理由

Copilot Studio はエージェントを 70 ツールに制限し、かつ各ツールの JSON スキーマは入力トークンを消費します。約 138 ツールを全部登録すると、実作業の前にターン 1 でモデルのトークン枠を溢れさせます（`OpenAIModelTokenLimit`）。そこで高価値な小サブセットだけを登録し、残りは 1 つの `call_tool(name, arguments)` ゲートウェイ経由で公開します:

```
call_tool(name="")                   → 全ツールを列挙（名前＋1 行要約）
call_tool(name="X")                  → X のシグネチャ／使い方を表示
call_tool(name="X", arguments={...}) → X を実行
```

実証済みの良い構成は `MCP_TOOL_MAP=1` かつ `MCP_TOOL_MAP_MAX=8` です。`MCP_TOOL_MAP` を未設定にすると全ツールを登録します（Claude Code のようなフル対応クライアント向け。Copilot Studio では不可）。

**`MCP_TOOL_MAP_MAX` は「先頭に登録する数」であって公開ツール総数ではありません。** `tools/auto/` の
forged ツールはこの数の**後から上乗せ**で登録されます。`MCP_TOOL_MAP_MAX=10` のサーバが 72 ツールを
公開し、クライアント上限の 70 を無言で越えていた実例があります。確認すべき数は `tools/list` が実際に
返す数です。

**`MCP_TOOL_MAP_INCLUDE` で pin したら、その数だけ `MCP_TOOL_MAP_MAX` を上げてください。** 優先セット
だけで 8 が埋まるため、上げずに pin したツールは黙って落ちます。落とした場合はサーバが起動時に
stderr へ `[tool_map] ... cut N tool(s) that were asked for: ...` と出し、落とさなければ何も出しません。

---

## 何がどこに保存されるか（別 PC への移行）

「設定値」は全部 `.env` に集約されますが、「サインイン状態」は性質上 `.env` に入れられません。**別 PC に移すときは、設定は `.env` ごとコピーで済みますが、サインインは各 PC で 1 回ずつやり直し**になります。

| 保存先 | 中身 | 別 PC へ持ち運べる？ |
|---|---|---|
| **`.env`**（git 管理外） | 秘密・エージェント URL・トンネル・ポート類 | ✅ コピーで設定はそのまま |
| **専用 Edge プロファイル** `copilot-companion-edge`(:9222) / `copilot-bridge-edge`(:9223) | M365 サインイン状態（Cookie） | ❌ 各 PC で初回 1 回サインイン |
| **devtunnel** のローカルトークン | Dev Tunnel ログイン状態 | ❌ 各 PC で 1 回 `setup_devtunnel.ps1` |
| **Copilot Studio**（クラウド） | エージェント本体・MCP コネクタ登録 | ☁ クラウドに 1 個作れば全 PC 共通。URL を `.env` に貼るだけ |

---

## 深掘りレビュー

`MCP_REVIEW_P2C=0` が既定で自動生成されます。`1` または `2` に変更して `start_all.bat` を
実行すると、`/deep-review` と `/deep-security-review` がコマンド一覧に表示されます。

- `0`: 無効。通常レビューのみ。
- `1`: 拒否・タイムアウトに耐える深掘りレビュー。意図を偽装せず、安全に分割・再試行します。
- `2`: 同じコマンド名でフル検証を実行します。許可されたローカル範囲で動的検証を優先し、
  全severityの確認済み指摘を再現、3視点反証、完全性検査を実施します。必要な動的証跡が
  欠ける場合は `INCONCLUSIVE`、脆弱性を確認した場合は `VULNERABLE` として非ゼロ終了し、
  「問題なし」にはしません。

レベル2の `VERIFIED_WITHIN_SCOPE` は、記録された対象・時点・証跡の範囲に限る判定です。
将来の変更や未知の攻撃を含む無条件の安全保証ではありません。
この2コマンドだけが Fresh Session Replay と上限付きタスク分割を使い、従来の
`/review` と `/security-review` の挙動は変わりません。

既存の `.env` にキーがない場合は、起動時に `0` が追記されます。既に設定した
`0` / `1` や他の値は上書きされません。

---

← [README に戻る](../README.md)
