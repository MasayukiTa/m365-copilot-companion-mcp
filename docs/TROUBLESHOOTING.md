# トラブルシューティング

困ったときはまず `doctor.bat` をダブルクリックしてください。サーバ→Dev Tunnel→専用 Edge→M365 サインイン→Bearer 認証まで全リンクを緑/赤でチェックし、赤にはその場で直し方を表示します。それで解決しない場合にこのページを見てください。全体像は [README](../README.md)。

---

## 🔌 まず最初に疑うこと — Copilot がこう言い出したら、ほぼ MCP/トンネル切れ

Copilot が次を返したら、エージェントの故障ではなく **MCP サーバーか Dev Tunnel が落ちている**可能性が高いです:

> **「申し訳ございません。それに応答できませんでした。他に何かお手伝いできることはありますか?」**
> （英語も同等の "Sorry, I couldn't respond to that…" 系）

確認順:

1. ローカルでサーバー生存: `Test-NetConnection localhost -Port 8000`
2. トンネルが host 中か: `devtunnel show <tunnel> | Select-String "Host connections"`（**0 なら切れ**。プロセスは生きていても host 接続だけ落ちることがある）
3. supervisor が動いているか（下記「常時起動」参照）。動いていれば数十秒で自動復活する
4. 手動復旧: サーバー起動 → `devtunnel host <tunnel>`

---

## 常時起動 / 切断対策（supervisor）

Dev Tunnel の host 接続は、**プロセスが生きていても relay 接続だけが静かに落ちる**ことがあります（これが上の Copilot エラーの主因）。`supervisor.ps1` はポート 8000 とトンネルの `Host connections` を定期監視し、落ちていれば自動で張り直します（デバウンス＋接続確立待ち付き）。

手動起動:

```powershell
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass `
  -File .\scripts\supervisor.ps1 -TunnelName <あなたのトンネル名>
```

**ログオンのたびに自動起動**させたい場合（管理者権限不要。Task Scheduler が組織ポリシーで弾かれる環境でも通る）— スタートアップフォルダにランチャを置く:

```powershell
$startup = [Environment]::GetFolderPath('Startup')
$root = (Get-Location).Path
@"
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$root\scripts\supervisor.ps1"" -TunnelName <あなたのトンネル名>", 0, False
"@ | Set-Content -Encoding ASCII (Join-Path $startup "start-companion-supervisor.vbs")
```

これで再起動・スリープ復帰後もログオン時に supervisor が立ち上がり、サーバー＋トンネルを復活させます（多重起動は内部の mutex で防止）。

---

## 初期設定でよく詰まる箇所

| 症状 | 確認・対処 |
|---|---|
| `quickstart.bat` が Python が見つからないと止まる | `winget install Python.Python.3.12` → 再実行 |
| `devtunnel login` が何度やっても "Not logged in" のまま | `supervisor.ps1` が動いていたら一度止める。フォアグラウンドの新しいターミナルで `devtunnel login` を実行し、ブラウザの「続行」を確実にクリックする（`docs/STARTUP_devtunnel_login.md` 参照） |
| Copilot Studio のツール登録でツール一覧が出ない | Dev Tunnel が `host` 状態かを確認（`devtunnel show <tunnel> \| Select-String "Host connections"`。`0` ならトンネルが落ちている）、MCP サーバーが起動しているか確認 |
| エージェントが「申し訳ございません。それに応答できませんでした。」と返す | MCP サーバーか Dev Tunnel が落ちている。`Test-NetConnection localhost -Port 8000` → `devtunnel show` → 手動で `.\scripts\start.ps1` → `devtunnel host` で復旧 |
| `rebuild_ui.ps1` が `csc.exe not found` で失敗 | .NET Framework 4.x が入っていない。「Windowsの機能の有効化または無効化」→「.NET Framework 4.8」を有効化 |
| `start_companion_edge.ps1` でサインインが要求され続ける | `-Foreground` でウィンドウを表示してサインインを完了させる → 次回から `-Headless` で起動 |
| `unlock` を何度も要求される | Copilot Studio バックエンドの送信元 IP が変わった（VPN 切替など）。その都度 `unlock(password=...)` を呼ぶのが正常動作 |

---

## ツールが動かないとき

| 症状 | 対処 |
|---|---|
| ツール一覧に出るのに呼ぶとエラー | そのツールの前提タグ（🪟 / 📦）を確認。対応する OS / アプリ / ライブラリを入れるか、その環境では使わない |
| `odbc_*` が接続不可 | ODBC Driver 18 for SQL Server をインストール、`odbc_drivers` で確認 |
| `ocr_*` が空を返す | Tesseract と言語データ（`jpn.traineddata` 等）を入れて `which("tesseract")` で確認。または `read_image` で Opus に直接読ませる |
| `pptx_export_png` 失敗 | ホスト PC に Microsoft PowerPoint がインストールされている必要あり（COM 経由） |
| `outlook_*` 失敗 | ホストに Outlook 本体が必要。なければ Copilot Studio の純正メールコネクタ側でやる |
| `render_diagram` で SSL エラー | 社内プロキシが Kroki をブロック。CA bundle を直すか、本ツールを使わず matplotlib でローカル描画させる |
| Copilot Studio がタイムアウト | 1 リクエストの実質予算は ~90 秒。`run_in_background` → `job_wait` で分割 |
| `unlock` を何度も要求される | 呼び出し元 IP が変わった（VPN 切替、Copilot Studio バックエンドの hop）。再 unlock |

---

## doctor.bat の各チェックの意味

`doctor.bat`（= `scripts/doctor.ps1`）は次のリンクを順に緑/赤で判定します。赤が出た行がそのまま直す場所です:

- **MCP サーバー**: `localhost:8000` が待受しているか。赤なら `.\scripts\start.ps1`（または `supervisor.ps1`）でサーバーを起動。
- **Dev Tunnel**: トンネルが host 状態か（`Host connections` > 0）。赤なら `devtunnel host <tunnel>`、または supervisor を起動して自動復旧させる。
- **専用 Edge (:9222)**: CDP エンドポイントが応答するか。赤なら `.\scripts\start_companion_edge.ps1` で起動。
- **M365 サインイン**: 専用 Edge が M365 にサインイン済みか。赤なら `-Foreground` で起動して手動サインイン。
- **Bearer 認証**: `MCP_API_KEY` でサーバーに通るか。赤なら `.env` の `MCP_API_KEY` と Copilot Studio 側に貼った `Bearer ...` 値が一致しているか確認。

---

## 初回動作確認（接続テスト）

**① MCP サーバーが生きているか:**

```powershell
Test-NetConnection localhost -Port 8000
# TcpTestSucceeded : True が出れば OK
```

**② エージェントから `list_my_tools` を叩く:** Copilot Studio のエージェント（または `CopilotChat.exe`）に「`list_my_tools` を呼んで。」と伝える。一覧が返れば配線 OK。🟢 のものはすぐ使え、🪟 / 📦 のものは依存が揃っていなければエラーになりますがサーバー全体は落ちません。

**③ 書込・実行系ツールのアンロック（初回のみ）:** `write_file` 等を初めて呼ぶと `[locked client IP: '...'] Call unlock(password='...') first.` と言われます。「`unlock(password="<MCP_UNLOCK_PASSWORD>")` を呼んで。」と伝えると、その IP が `MCP_UNLOCK_TTL_DAYS` 日間解錠されます。

**④ Dev Tunnel を通る接続:** Copilot Studio のバックエンドから呼ぶと IP が毎回変わることがあり `unlock` を再要求されます。これは正常動作です。

---

← [README に戻る](../README.md)
