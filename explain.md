# 時間切れした「動作する」ターンが STUCK になる仕組みと本修正

ジョブ r6ae0d5a6_a0_w3。実測 2026-10-27 15:53 (.fleet/status.json): 再起動後のフリート
(約 300 担当、CPU 90〜100%、物理メモリ空き 1.6〜2.5 GB)で 90 担当が
`not re-sent: the turn may already have been delivered and this goal performs an action
(delivery=unknown, turn timeout without a reply)` で STUCK。run_id r6ae04307_a0。turn 内訳は
turn2 x72・turn1 x16・turn3 x2。多くはファンアウトの子で、動作を伴う目標(機能実装、PR180 確認、
PR184 修正、PR #304)を持つ。

以下はすべて修正前の relay/relay_fleet.py (origin/main 910bb189)の行番号を根拠にする。

## 1. 「配達されたか」をどう判定していたか — 判定していなかった

時間切れ経路は RelayWorker.poll の中にある。応答がまだ無く、かつ自前の時計が予算を超えたとき
(`if not _has_new_answer and time.time() - self._t_send > _bound:`、9322 行)に入る。

- 9355-9356 行: `_timeout_resend_decision()` を呼び、動作目標で効果を確認できないときは
  `_timeout_resend != "resend"` となる(9357 行)。
- 9363-9368 行: ソケットが開いていれば `_retry_socket(..., assume_landed=True)` で同じ会話の
  「続きのターン」を試みる。
- 続きが成立しないと 9376-9379 行へ進み、ここで
  `self._refuse_resend("turn timeout without a reply", "unknown")` を無条件に呼ぶ。

つまり delivery 引数は コード上で "unknown" にハードコードされていた(9378 行)。この経路は
「このターンにモデルが実際に配達されたか(＝応答トークンが届いたか)」を一切見ていない。

`_refuse_resend`(8204-8215 行)は受け取った delivery をそのまま STUCK 理由と台帳に書く:

```
self.reason = ("not re-sent: the turn may already have been delivered and this goal "
               "performs an action (delivery=%s, %s)" % (delivery, reason[:120]))   # 8207-8208
```

これが status.json で観測された文面そのもの。delivery=unknown は判定結果ではなく、
判定を省いた既定値だった。

### 配達の証拠はすでに存在していた

ドライバ(relay/socket_driver.py)は、このターンに届いたトークンを partial_text()
(416-419 行)で、完了フレームの到着を _turn_answered(100 行、settled_text が 433 行で参照)で
保持している。再接続経路はこの同じ情報を saw_output として既に読んでいる。時間切れの拒否だけが
それを参照せず、応答が「遅かっただけ」のターン(トークンは届いていた)も unknown 扱いで STUCK に
していた。

## 2. 時間切れの秒数(_bound)がどう決まるか

比較対象の予算 _bound は poll 内 9302-9311 行で決まる。

- 9302 行: 基準は `_bound = self.per_turn_timeout_s`。既定の毎ターン上限。
- 9306-9308 行: ソケット担当で既に per_turn を超えていても、`_socket_tool_wait_alive()` が真
  (このターンに帰属するツール実行が進行中)なら `_bound = max(_bound, SOCKET_TURN_TIMEOUT_S)` に広げる。
- 9309-9311 行: ブリッジ中のターンはツールの往復を含むので `tool_bridge.turn_limit_s()` まで広げる。

定数の根拠は relay/relay_fleet.py 冒頭:

- SOCKET_TURN_TIMEOUT_S(3078 行)= 既定 1200 秒(MCP_FLEET_SOCKET_TURN_S)。1 担当のソケットターンが
  走ってよい長さ。3067-3077 行のコメントのとおり、既定 600 秒では 4 か月分のメール検索のような重い
  目標が時計に殺されていたため 20 分に引き上げた。
- 比較に使う per_turn_timeout_s 自体は 240 秒系で、9287-9301 行のコメントが「ソケットの予算を
  1200 に広げるのは誤り(届いていた応答を 1200 秒待たせる)」と測定を根拠に述べている。
- SOCKET_MEANINGFUL_IDLE_S(3090 行, 既定 60 秒)や SOCKET_LIVE_STREAM_IDLE_S(3096 行, 300 秒)は
  別経路(無活動失速)の時計で、9285-9286 行の _socket_meaningful_idle_stalled が使う。

したがって 90 担当は、per_turn(240 秒)の予算を超えた時点で応答がまだポーリングに見えず、続きの
ターンも成立せず、9378 行の ハードコード unknown で拒否されて STUCK になった、という経路である。

## 3. 本修正(既定オン、切替可)

9376-9379 行の拒否直前に、配達の証拠を読む判定を挟んだ。設定 verify_delivery_before_giving_up
(既定 on、relay/delivery_verify.py、tools/settings_keys.py に each_gate 登録)がオンのとき:

- self._saw_output_for_turn()(新設、partial_text()/_turn_answered を読む)が真なら
  transport_policy.delivery_seen_from_output(True) -> "delivered"。このときは STUCK にせず
  return False で応答を待ち続ける(同一ターンの逐語再送は動作をもう一度要求してしまうため)。
- 出力が無ければ正直に "unknown" のままとし、従来の慣重な拒否を変えない。

設定オフ時は 9378 行の旧来のハードコード unknown 拒否と完全に同一。delivery=unknown は
ハードコードから実測値へ置き換わり、挙動が変わるのは「出力が届いていた＝delivered」の場合だけで、
それは実トークンという根拠に裏打ちされる。
