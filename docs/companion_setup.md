# 専用Edge起動 & 並列実行 セットアップ手順

## 0. 最短セットアップ（ワンクリック）

`quickstart.bat` をダブルクリックするだけで、次が一括で進みます:

1. Python / venv / requirements の導入（`setup.bat` の再開可能ブートストラップ）
2. `.env` に新規シークレットを生成し、**Bearer トークン（`MCP_API_KEY`）と
   アンロックパスワード（`MCP_UNLOCK_PASSWORD`）をターミナルに表示**（コピペ用）
3. git の更新確認（`fetch` のみ）。更新があれば「pull するか？」と聞く（**push はしない**）
4. MCP サーバ起動

> 再実行しても安全（既存の `.env` シークレットは上書きしません。トークン/パスワードは
> 毎回表示されるので控え忘れても困りません）。CDP 用の Edge は次章の専用 Edge を使います。

---



このプロジェクトの中継器（bridge / relay / 並列実行）は、Edge を **CDP（リモートデバッグ
ポート）** 経由で attach して M365 Copilot の Web UI を操作します。ここでは「専用・隔離Edge」
の起動から並列実行までの手順をまとめます（README とは別の運用メモ）。

---

## 1. なぜ「専用・隔離 Edge」なのか

普段使いの Edge をそのまま使うと、次の失敗が起きます（実測で確認済み）:

- M365 Copilot タブは 1 枚あたり **0.3〜0.6 GB** の重い SPA。
- 普段使い Edge に M365 タブを多数開いた状態で並列実行すると **物理メモリが枯渇** → Edge が
  クラッシュ → **`--remote-debugging-port` 無し**で自動再起動 → ポート 9222 が消え、
  bridge / relay / 並列実行のすべてが `ECONNREFUSED` で死ぬ。

`start_companion_edge.ps1` は **別プロファイル（別 user-data-dir）＋固定ポート** で Edge を
起動します。利点は 2 つ:

1. 別プロファイル＝必ず新規インスタンス。だから `--remote-debugging-port` が確実に bind する
   （「Edge が既に起動していると debug フラグが無視される」典型ハマりを回避）。
2. 普段使い Edge と完全分離。本体に M365 タブを何枚開いても、専用 Edge の RAM を奪わず、
   本体のクラッシュに巻き込まれない。並列実行が完了タブを閉じても解放されるのは専用 Edge の
   メモリだけ。

---

## 2. 起動手順

```powershell
# 専用・隔離 Edge を起動（既定ポート 9222）。別ポートにしたいなら -Port 9333 など
.\start_companion_edge.ps1
```

- 別プロファイルなので **初回はそのウィンドウで M365 に一度サインイン**してください。以後は
  プロファイルが永続するので無ログインで attach します（Windows の SSO が効いていれば初回も
  自動サインインのことがあります）。
- すでに同じポートが listen していれば何もしません（多重起動しない）。
- 起動後、CDP は `http://127.0.0.1:9222` で待ち受けます。
- **既定で最小化（バックグラウンド）起動**します。CDP はウィンドウが見えなくても駆動でき、
  スロットリング防止フラグ付きなので最小化中でもエージェントは鈍りません。ユーザーは Edge を
  一切気にせず、チャット／コックピットにタスクを打ち込めます。
- **サインインが必要なとき**（SSO 失効など）は自動で前面に出ます（フリートがログイン画面を検知）。
  手動で出したいときは `.\start_companion_edge.ps1 -Surface`。最小化したくない場合は `-Foreground`。

> 普段使いの Edge は閉じる必要はありません。専用 Edge とは別物として共存します。

---

## 3. 並列実行（複数ゴールを同時に進める）

```powershell
# ゴールを直接渡す
.\.venv\Scripts\python.exe -m relay.fleet_runner -g "ゴールA" -g "ゴールB" -g "ゴールC"

# ゴールをファイルから（1 行 1 ゴール、# 始まりはコメント）
.\.venv\Scripts\python.exe -m relay.fleet_runner --goals-file goals.txt
```

- **同時に開くタブ数**は既定 3。コックピットの「最大タブ」ステッパー（= `settings.txt` の
  `maxtabs`）で変更できます。CLI では `--max-concurrent N`（`0` = 空き RAM から自動算出）。
- ゴール数が同時数を超えた分は **待機列**に並び、前のタブが**完了した瞬間に解放**されて次が開き
  ます（メモリを使い切らない）。
- **ターン上限**は既定 1000（実質無制限）。`--max-turns N` で変更可。
- 進捗はライブで `.fleet/status.json` に書き出され、コックピット（`ui\FleetCockpit.exe`）が
  これを読んで 1 ゴール 1 カードで表示します。カードの **「解放」ボタン**でそのタスクを停止して
  タブを解放できます（`.fleet/commands.json` 経由）。

---

## 3.5 詰まったときの復旧（重要）

並列実行を酷使すると、専用 Edge が応答しなくなり、フリートの `attach`（タブ生成）で
固まることがあります。**窓を × で閉じる／プロセスを kill するだけでは、Edge の
「セッション復元」で再起動時に詰まったタブごと復活し、同じ症状が再発します**。正しい復旧：

- **Edge がまだ応答する場合**：タブを1枚ずつ閉じる（復元対象から外れる）
  ```powershell
  .\.venv\Scripts\python.exe -m relay.edge_recover --to-agent
  ```
  CDP 越しに全タブを `page.close()` で1枚ずつ閉じ、新しいエージェントchatを1枚だけ残します。

- **Edge が完全無応答（CDP も死んでいる）の場合**：ハードリセット
  ```powershell
  .\start_companion_edge.ps1 -HardReset
  ```
  専用 Edge を kill → **セッション復元状態（Sessions/ 等）を削除** → 再起動。詰まったタブは
  復元されず、クリーンな1タブ＋サインイン済みで立ち上がります（実機検証済み）。

どちらも普段使いの Edge には触れません（専用プロファイルのみ対象）。

---

## 4. .env（任意）

```
MCP_CDP_URL=http://localhost:9222
MCP_CDP_PORT=9222
MCP_IMPL_AGENT_URL=https://m365.cloud.microsoft/chat/agent/T_<GUID>.<id>
# 並列実行が駆動するエージェント（未指定なら MCP_IMPL_AGENT_URL）
MCP_FLEET_AGENT_URL=https://m365.cloud.microsoft/chat/agent/T_<GUID>.<id>
```

エージェント URL はテナント GUID を含むため **.env（gitignore 済み）**にのみ置きます。
