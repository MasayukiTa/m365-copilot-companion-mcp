# issue #328 ゲートウェイ実行ガード — 実装報告

ブランチ `feature/gateway-exec-guard`(起点 origin/main)。対象リポジトリ
`MasayukiTa/m365-copilot-companion-mcp`。作業は作業用複製(worktree)内のみ。live チェックアウト
(`C:/Users/.../resonac-mcp`)と本番サーバーには触れていない(読み取りも worktree 側で完結)。

---

## 1. 読んで確かめた実行モデル(issue が「未確認」としていた点)

issue は実行モデルを「ワーカープロセス化が可能か、既存の直列実行器の構造は未確認」としていた。
コードを読んで確かめた結果は次のとおり。

- **トランスポート**: `main.py` は FastMCP(`mcp.http_app(path="/mcp",
  transport="streamable-http")`)を uvicorn で単一プロセス起動する(`main.py` 末尾
  `uvicorn.run(app, host="127.0.0.1", port=8000, ...)`)。
- **ツールの実行場所**: FastMCP は各 `tools/call` を **anyio のワーカースレッド**で実行する。
  根拠は main.py 内コメント「the tool in an anyio worker thread, which is the route that
  normally carries it」と、/health の docstring「tool bodies are now offloaded ... from the
  loop」。つまりイベントループ(= `initialize` / `tools/list` / `@mcp.custom_route` の
  `/health`)はツール本体を走らせない。だから詰まっていても tools/list は数秒で答える、という
  issue の観測と一致する。
- **「直列」の実体**: 真の直列ではなく、**ワーカースレッドプールの枠が有限**であること。重い
  `find_files` / `glob` がプールの枠を占有し続けると、後続の `tools/call` はスレッド割り当て待ち
  で詰まる。ループ上の経路だけが生き続ける。
- **ゲートウェイ本体**: `call_tool`(main.py、`MCP_TOOL_MAP=1` のとき定義)は**同一プロセス内の
  普通の Python 関数**で、内部ツールを `fn(**_args)` と**同じスレッドで同期的に直接呼ぶ**。
  呼び出しの前後に tool_ledger の `record_call` / `record_outcome`、`_t0 = time.time()` の計測が
  既にある(可視化フックを足す足場として使える)。
- **別経路**: `/health` は `@mcp.custom_route` で FastMCP の認証層の外・ループ上にあり、
  `derive_identity`(loopback 判定)+ bearer で保護される。tools/list とは完全に別経路。
  → 提案2の可視化を載せる正しい場所。

この事実が提案1・5の可否を決める(下記 §2)。

---

## 2. 提案5点の実装状況

優先順位(安全な順): 3 → 2 → 4 → 1 → 5。

### 提案3: find_files / glob のガードレール — **実装済み(入れた)**

- `tools/walk.py`: `iter_files(..., max_depth=N)` を追加。os.walk 中に `dirs[:]` を切り詰め、
  深い木へ**降りる前に**枝刈りする(深い木のコストを払わない)。`max_depth=None` は従来どおり無制限。
  深さは探索ルートから数える(ルート直下のファイルが depth 0、`max_depth=0` はルートのみ)。
- `WalkDeadline(seconds=None)`: 明示引数 > 設定値 > 従来定数(20s)の優先順。ゼロ/負値は無視。
- 設定読み取り関数 `search_timeout_seconds()` / `search_max_depth()` / `search_concurrency()` を
  追加。設定ファイルを**毎検索で読み直す**ので、再起動なしに次の検索へ反映。不正値・欠損は従来
  既定へフォールバック(検索が設定不備で死なない)。
- `tools/search_ops.py`: `glob` / `find_files` に `max_depth` 引数を通し、docstring に明記。
- 上限到達時は既存の `WalkDeadline` + `pruned_note` により**部分結果+打ち切り明示**を返す経路が
  既にあり、これを維持。

### 提案2: 実行中・待ち行列の可視化(tools/list と別経路)— **実装済み(入れた)**

- `tools/exec_inflight.py`(新規): プロセス内のスレッドセーフな在庫。`begin(tool, call_id,
  started_ts)` / `end(token)` / `snapshot()` / `executing_count()`。token はプロセス内で一意な
  連番(同名ツールの同時実行も別行として扱い、end が取り違えない)。経過は `time.monotonic()` で
  計算(巻き戻らない真の経過)、`started_ts` は表示用の壁時計。
- `call_tool` 本体に配線: `record_call` の直後に `begin`(call_id と `_t0` を渡す)、`finally` の
  先頭で `end`(無条件・total)。**戻らない呼び出しも在庫に残る**ので、詰まりの占有者を名指せる。
  ledger と同じく例外は握りつぶす(可視化の失敗がツール実行を止めない)。
- `main.py` に `@mcp.custom_route("/exec_status", GET)` を追加。`/health`・`tools/list`・ツール
  経路のいずれとも別の、ループ上の経路。短いロックを取るだけでツール・ファイル・サブプロセスに
  触れないので、**全ツールスレッドが占有されていても答える**。認証は /health と同一(loopback or
  bearer)。非認証の呼び出しには `{status, executing_count}` のみ返す(占有ツール名・経過は伏せる)。
- 返す内容: `executing_count`、`executing`(占有が長い順=ハンティング対象が先頭に、各
  `{tool, call_id, started_ts, elapsed_s}`)、`longest_elapsed_s`、`as_of_ts`。

「待ち件数」について: 真の待ち行列長は FastMCP/anyio のスレッドプール層にあり、ゲートウェイ関数
 からは観測できない。ここで観測できるのは**実行中(execute 経路に入った)件数**で、それが
 `executing_count`。これが占有の有無と占有者の特定という提案2の目的を満たす。

### 提案4: キュー満杯時の即時拒否(fail fast)— **検索経路について実装済み(入れた)**

- `tools/search_ops.py` の `_bounded_search`: 空き枠が無ければ**キューに積まず即エラー**
  (`busy: N searches already running, retry later (raise search_concurrency in settings...)`)。
  これが issue の「沈黙のまま待たせない」を検索について満たす。`search_concurrency`(既定4)が上限。
- ゲートウェイ全体の待ち行列上限は、FastMCP/anyio のプール層に属し、ここで安全に拒否を差し込むと
  本番の dispatch を壊すため**入れない**。可視化(`executing_count`)で占有を見える化し、検索という
  実証済みの占有源に fail-fast を効かせる、という範囲に留めた。

### 提案1: サーバー側ウォールクロック上限+実殺 — **入れない(見送り)**

- 理由: ツールは anyio ワーカー**スレッド**で走る。Python のスレッド強制終了は信頼できない
  (issue 自身も指摘)。真の「実殺」には issue が推奨するとおり**呼び出しごとのワーカープロセス化**が
  必要。だが現行の `call_tool` は `fn(**_args)` の同期直呼びで、プロセス境界に移すには全ツールの
  引数/戻り値のシリアライズ、共有プロセス内状態(security の presented-token、tool_ledger、unlock
  状態、evidence_trace、exec_inflight)の分離が要り、本番の dispatch 経路を全面的に作り替える。
  「無理に入れて本番を壊さない」の指示に従い見送る。
- **必要な設計変更**(持ち主判断用): (a) `call_tool` を ProcessPoolExecutor ベースに変更し、各呼び
  出しを子プロセスで実行、タイムアウト時にその PID のプロセスグループのみ終了(プロセス名 kill は
  禁止)。(b) 共有状態を子へ渡す/親に戻す経路の設計(token は引数で既に渡る足場がある)。(c)
  Windows ではプロセスグループ終了の作法が POSIX と異なるため分岐が要る(ただしテストは Linux CI、
  §テスト参照)。(d) 子プロセス起動コスト(数十〜数百 ms)の許容可否。
- 代替として入れた部分: 提案3の `WalkDeadline` は、最も実証された占有源(find_files/glob)に対する
  **協調的なウォールクロック上限**として機能する。スレッドを殺さず、walk ループが期限で自ら止まり
  部分結果を返す。実殺ではないが、観測された 3 回の詰まりの原因そのものを無害化する。

### 提案5: キャンセル伝播 — **入れない(見送り)**

- 理由: 提案1と同根。クライアント切断時に実行中スレッドへ割り込んで安全に中止する一般機構は、
  スレッドモデルでは存在しない(協調チェックポイントを全ツールに入れるのは別の大改修)。walk には
  既に `WalkDeadline` という協調停止点があるが、これは「切断」ではなく「期限」で止まる。
- **必要な設計変更**: 提案1のワーカープロセス化が前提。そのうえでクライアント切断を検知し、該当
  呼び出しを「中止対象」にマークして次の安全点またはタイムアウトで子プロセスを終了する。現行モデル
  では、切断を安全に伝播して実行を止める地点が無い。

---

## 3. 設定の宣言(tools/settings_keys.py)

issue の観測(2026-10-27〜28 に同型の詰まりが3回、占有者は find_files/glob の無制限実行、回復手段は
サーバー再起動のみ)を根拠として、3キーを宣言済み。根拠なしの数値は置いていない。

- `search_timeout_seconds`(EACH_GATE、既定 20 = #328 前の定数): 1検索の壁時計上限。毎検索で読み直す
  ので再起動なしで次検索へ反映。per-call 引数が優先。
- `search_max_depth`(EACH_GATE、既定 なし=無制限 = #328 前の挙動): 探索の最大深さ。既定を無制限に
  倒し、正規の深い検索を壊さない。引き上げ(=この場合は制限)は設定か per-call `max_depth` で明示。
- `search_concurrency`(BRIDGE_START、既定 4 = #328 前の定数): 同時検索数。BoundedSemaphore を import
  時に確定するため引き上げは再起動が要る旨を宣言。超過分は即拒否(キューしない)。

いずれも**既定値は正規の重い用途を壊さない側**(無制限 or 従来定数)で、**明示的な引き上げ手段**
(設定キー/per-call 引数)を用意。宣言と実装の一致は
`tools/test_a_setting_declares_when_it_takes_effect.py` が検査する。

可視化(提案2)は常時オン・読み取り専用で数値設定を要しないため、settings への追加は無し。

---

## 4. テスト

隣接する `test_*.py` の様式(REPO を sys.path に入れ、fastmcp に依存しない単体で書く)に従った。

- `tools/test_a_search_walk_is_bounded_in_depth_and_time.py`(提案3・4設定): max_depth が正確にその
  段数だけ降りる/None は無制限/深すぎる木へ降りない(os.walk をスパイして実証)/設定が再起動なし
  で反映/不正値はフォールバック/WalkDeadline が設定値を採用。パラメトライズ含め多数。
- `tools/test_the_gateway_says_what_it_is_running_now.py`(提案2、新規): 戻らない呼び出しが可視/経過
  が伸びる/end が idempotent・total/最長占有が先頭/同名ツール2件が別行/多スレッド同時 begin-end で
  破綻しない/snapshot が JSON 可能な組込み型のみ。計8ケース。

実行結果: 対象2ファイルで **23 passed**(ローカル pytest)。

修正前に失敗することの担保: 提案2のテストは `tools.exec_inflight` の import に依存するため、本モジュール
と `begin/end`・`/exec_status` が無い修正前では収集時に失敗する。提案3のテストは #327 由来の
`iter_files(max_depth=...)` が無い修正前で失敗する。

CI 登録: `.github/workflows/ci.yml` の pytest 明示リストに両ファイルを追加済み。
`python scripts/check_ci_test_manifest.py` は RC 0(manifest OK, 976 files)。
テストに Windows 専用記述は入れていない(Linux CI で落ちない)。

---

## 5. 残る課題

- 提案1(実殺)・提案5(キャンセル伝播)は未実装。入れるには `call_tool` のワーカープロセス化という
  実行モデルの作り替えが前提(§2 に必要な設計変更を記載)。持ち主の実装判断に委ねる事項。
- ゲートウェイ全体の待ち行列上限による fail-fast は、FastMCP/anyio のプール層に属するため未着手。
  現状は可視化(`executing_count`)+ 検索経路の fail-fast で、占有の検知と最頻占有源の抑止までを担保。
- `/exec_status` は観測のみで、停止操作は持たない(設計上の線引き。実殺が入るまでは「名指し」までが
  安全な範囲)。

---

DONE: 実行モデルを読んで確定し、安全に入る提案3・2・4(検索)を実装・テスト(23 passed)・CI 登録・
設定宣言まで完了。提案1・5はスレッド実行モデルでは安全に入らないため理由と必要な設計変更を明記して
見送った。
