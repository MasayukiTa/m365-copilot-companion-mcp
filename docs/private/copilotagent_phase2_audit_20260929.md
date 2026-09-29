# PR #66 / feat/durable-companion-phase2-20260928 READ-ONLY 監査

担当範囲: **4/8 — commands.d の claim / apply / commit**（取得・適用・コミットの原子性、失敗時ロールバック、二重適用防止）

- 監査者会話: commands.d claim/apply/commit
- 対象コミット/ブランチ: feat/durable-companion-phase2-20260928 (PR #66)
- 実行環境: `.venv` / pytest 実行可（unlock 済み、READ-ONLY 監査、既存ファイル無変更・git 操作なし）
- 監査日 (UTC): 2026-09-29

---

## サマリ

担当範囲の claim→apply→commit ライフサイクルは全体として**堅牢に設計されており、致命的欠陥は検出されなかった**。クラッシュ境界・PID 再利用・二重適用・受領書(ack)の各リスクにそれぞれ対策と既存テストが対応している。`relay/test_command_claim_commit.py` は **26 passed**（14.98s、RC 0）で全通過。

検出した論点は 1 件（優先度: **中〜低**）。二重適用防止は「durable ledger の goal（jid 冪等）」には効くが、`steer` / `reunlock` の副作用には及ばず、part-way 失敗時の再 apply で重複配信し得る。既存テストでは未捕捉。

---

## 確認済み（問題なしと判断した領域）

### C-OK-1 claim の原子性
`_claim_one_command` は `os.replace(original, claimed=".claim-<pid>-<birth>")` により取得する。競合で負けた側は `OSError` を捕らえて `None` を返し安全に降りる。`claim_next_command`（fleet_runner.py:2859-2869）は 1 件だけ返し、`claim_commands`（同 2872-2900）は legacy `commands.json` も含め oldest-first で走査。**根拠**: fleet_runner.py:2859-2900。

### C-OK-2 「1 件ずつ claim→apply→commit」でキュー全体を先取りしない
`_drain_commands`（3828-3853）は `claim_next_command` を用い、現在の claim の commit が成立するまで次を claim しない。コメント通り「post-apply commit が stall したら後続コマンドはどの live pid も所有しない ordinary `.json` のまま残す」ため stranding を回避。**根拠**: fleet_runner.py:3828-3853。

### C-OK-3 commit point の非再実行性
`commit_command_claim`（2925-2982）は改名（`.applied`/`.rejected`/`.read`）を「non-replayable commit point」と定義。受領書(ack)書込みが失敗しても committed tombstone を残し `False`（= commit のみ再試行）を返すため、コマンド副作用の再適用は誘発しない。既に committed が存在する再入時は rename をスキップし tombstone から継続（2944-2955）。**根拠**: fleet_runner.py:2925-2982。

### C-OK-4 commit-only 再試行の分離
`retry_pending_command_commits`（2985-）と `_drain_commands` 冒頭の `_pending_command_commits` 処理（3832-3835）により、「effects は適用済み、commit だけ再試行」を副作用の再適用と厳密に分離。commit 失敗時は当該 claim を `_pending_command_commits` に積み、次 sweep で commit のみ再試行。**根拠**: fleet_runner.py:2985-2989, 3826, 3832-3849。

### C-OK-5 スキーマ拒否のロールバック不要な terminal 消費
`admit_command` 不通過時は box を一切触る前に `False` を返し（3856-3861）、`commit_command_claim(applied=False)` で `.rejected` tombstone として監査可能に消費。部分適用が発生しない（SEC-08「WHOLE OR NOT AT ALL」）。**根拠**: fleet_runner.py:3856-3861, 3847-3849。

### C-OK-6 ledger goal の冪等性（二重適用防止の中核）
`goals_from_command` の submission_id 由来 jid と `_append_goals_ledger`（1849-1888）の resume-key 重複排除により、同一コマンドのクラッシュ再生（同 jid）は no-op、意図的な同一テキスト別 jid は別タスク。`return_new=True` で新規admittedのみ返し、`_new_cmd_goals` だけを memory(add_box) に投入（3924-3928）。**根拠**: fleet_runner.py:1849-1888, 3863-3867, 3924-3928。

### C-OK-7 stale claim / PID 再利用の回復
`claim_commands` は先頭で `_recover_stale_command_claims` を呼び、死んだ pid の `.claim-*` を回収して再試行。PID 再利用は create_time ベースの birth token で識別（別会話範囲と重複するが commands.d 経路として確認済み）。**根拠**: fleet_runner.py:2880。

### C-OK-8 restore（未 commit claim の返却）
`restore_command_claim`（2903-2922）は `claimed` が存在し、かつ `original` が未再生成のときのみ `os.replace(claimed, original)` で戻す。`original` が既に存在する競合時は `.orphan` に退避して `False`（二重の original を作らない）。**根拠**: fleet_runner.py:2903-2922。

### C-OK-9 legacy commands.json 互換
出荷済み pre-commands.d cockpit が書く legacy `commands.json` も claim 対象に含め、互換維持で durability hole を再導入しない。**根拠**: fleet_runner.py:2883-2889。

### 既存テスト
`relay/test_command_claim_commit.py` = **26 passed / 1 warning / 14.98s / RC 0**。claim/apply/commit の原子性・commit-only 再試行・tombstone 回復・冪等性を広くカバー。

---

## 検出した論点

### C-ISSUE-1 part-way 失敗時、steer / reunlock の副作用が再 apply で重複し得る（優先度: 中〜低）
> Resolution: see `2026-09-29 follow-up resolution / C-ISSUE-1` below. The original finding is retained as audit history.


**再現条件**
1. 1 つのコマンドが `add_goal`(goals) と `steer` または `reunlock` を同時に含む、あるいは `steer` を含む。
2. `_apply_command`（3862-3951）で `deliver_steers`（3913）/ `apply_reunlock`（3922）/ `add_box.append`（3928）の実行「後」に、後続処理または当該コールバック内で例外が発生する（例: `deliver_steers` が一部 worker へ配信後に内部例外、enqueue コールバックの失敗、telemetry import 以降の想定外例外）。
3. `except`（3952-3955）が `return None` を返す。
4. `_drain_commands` の `else` 分岐（3851-3852）が `restore_command_claim` で claim を `original` に戻す。
5. 次 sweep で同一コマンドが再 claim され `_apply_command` が再実行 → `deliver_steers` / `apply_reunlock` が二度実行される。

**影響**
- steer メッセージが対象 worker に重複投入され得る（新ターンとして二重に届く）。
- `reunlock` が worker へ二度投入され得る（unlock ターンの重複）。
- durable ledger の goal は jid 冪等（C-OK-6）で保護されるため、goal の二重投入は起きない。副作用の非冪等部分は steer / reunlock に限定される。

**根拠ファイル/行**
- `deliver_steers` 呼び出し: relay/fleet_runner.py:3912-3913
- `apply_reunlock` 呼び出し: relay/fleet_runner.py:3921-3923
- part-way 例外 → `return None`: relay/fleet_runner.py:3952-3955
- None → restore → 再 apply: relay/fleet_runner.py:3851-3852, 3836-3840
- else 分岐コメント「application failed before a durable effect」（3851）は steer/reunlock 副作用に関しては正確でない。ledger 書込み(3866)より後で例外が出れば「副作用は発生済み」の状態で restore される。

**優先度**: 中〜低。`steer`/`reunlock` と goal を同一コマンドに載せる運用が稀であること、part-way 例外自体が例外的であること、steer 重複は「二度届く」で致命的でないことから低め。ただしコメントが「before a durable effect」と主張している点は誤解を招くため中に引き上げ。

**推奨修正（いずれか）**
1. コメント修正 + 設計意図の明記: steer/reunlock は「best-effort・at-most-once ではない」ことを docstring/コメントに明記し、`else` 分岐コメントの「before a durable effect」を「durable ledger effect は commit 済みでなくとも冪等再構築される。steer/reunlock は再配信され得る」に訂正。
2. 副作用の冪等化: コマンドごとの適用済みフラグ（submission_id 単位）を steer/reunlock 実行前後に立て、再 apply 時にスキップ。ledger と同様の resume-key 方式を steer/reunlock にも適用。
3. 順序の入れ替え: 例外を投げ得る処理（ledger 書込み等）を steer/reunlock/add_box より前に集約し、副作用実行後は例外を投げ得る処理を置かない（実質 at-most-once に近づける）。

**既存テストで捕捉済みか**: 未捕捉。`test_command_claim_commit.py` は 26 件全通過だが、`_apply_command` の部分適用後 `return None` → restore → 再 apply で steer/reunlock が重複するシナリオのテストは確認できなかった。回帰テスト追加を推奨。

---

## 監査で実施したこと（範囲 4/8 の取得件数）

- 読解した主要関数: `_append_goals_ledger` / `_read_goals_ledger` / `_read_done_map`（1849-1919）、`claim_next_command`（2859-2869）、`claim_commands`（2872-2900）、`restore_command_claim`（2903-2922）、`commit_command_claim`（2925-2982）、`retry_pending_command_commits`（2985-）、`_drain_commands`（3828-3853）、`_apply_command`（3855-3955）、`deliver_steers`（402-）。
- 実行したテスト: `relay/test_command_claim_commit.py`（26 passed）。
- 確認済み論点（問題なし）: 9 件（C-OK-1〜C-OK-9）。
- 検出した論点: 1 件（C-ISSUE-1、優先度 中〜低、既存テスト未捕捉）。

担当範囲 4/8 の監査を完了。

---

# 担当範囲 1/8 — durable LOCAL_LOOP / Fleet 境界

担当範囲: **1/8 — LOCAL_LOOP と Fleet の責務分離、job/lease/fencing_token の受け渡し、CLOUD_WORKIQ との分離**

- 監査者会話: durable LOCAL_LOOP/Fleet 境界
- 実行環境: READ-ONLY 監査。既存ファイル無変更・git 操作なし。書込は本レポートのみ。
- 監査日 (UTC): 2026-09-29

## サマリ

担当範囲の実装は**堅牢で、致命的欠陥・承認ブロッカーは検出されなかった**。lease/fencing_token 検証は 4 経路すべてで一元関数を経由し、CLOUD_WORKIQ 分離はコード・テスト双方で担保、resume/autoresume と single-instance ownership はマーカ意味論とプロセス誕生時刻束縛で正しく設計されている。検出した論点は 1 件（優先度: **低**、テスト網羅ギャップ、実装欠陥ではない）。

## 確認済み（問題なしと判断した領域）

### L-OK-1 lease/fencing_token 検証の一元化と全経路適用
`_validate_lease`（relay/local_job_store.py:336-342）が lease_id 不一致→`LEASE_MISMATCH`、fencing_token 不一致→`FENCE_MISMATCH`、期限切れ→`LEASE_EXPIRED` を投げる。呼び出し元 4 経路すべてがこの関数を通過することを確認: heartbeat（:354）、commit_turn（:428）、abort_turn（:519）、read_job_context（:102-103 でトークンを引数受領）。**根拠**: relay/local_job_store.py:336-342, 354, 428, 519。

### L-OK-2 stale claim の fencing（late commit の排除）
未 commit ターンの再試行で fencing_token が +1 され、旧トークンの late commit が拒否される。`test_controller_retry_fences_late_commit_and_preserves_sequence`（relay/test_local_job_store.py:218-242）が retry_uncommitted_turn 後の旧 lease での commit が `LEASE_MISMATCH` になること、再claimで fencing_token が +1 されること、`TURN_CONTROLLER_RETRY` イベントが記録されることを検証。retryable abort でも同様（:203-215）。**根拠**: test_local_job_store.py:203-242。

### L-OK-3 CLOUD_WORKIQ と LOCAL_LOOP の分離（コード・テスト双方で担保）
`resolve_profile`/`validate_runtime`（relay/execution_profiles.py）が明示プロファイル不変・AUTO ルーティング・推測拒否・cloud 時に local_mcp を見ないことを保証。`relay/test_execution_profiles.py` 全 4 テストが検証（`test_explicit_profiles_are_never_rewritten` / `test_auto_routes_by_required_runtime_and_location` / `test_auto_refuses_to_guess` / `test_cloud_runtime_never_checks_local_mcp`）。加えて create_job が CLOUD_WORKIQ ジョブを PROFILE_MISMATCH で拒否（store テスト:188 付近）。**根拠**: relay/execution_profiles.py, relay/test_execution_profiles.py:1-44。

### L-OK-4 read_job_context の境界（path escape 防止 + bounded）
read_job_context は allowed_base 外のファイル参照を `CONTEXT_PATH_ESCAPE` で拒否し、max_context_file_bytes で truncate。`test_read_job_context_transports_bounded_file_without_path_escape`（test_local_job_store.py:90-117）が `file:../outside.txt` の拒否と正常読取の bounded 応答を検証。呼び出しには lease_id + fencing_token が必須。**根拠**: test_local_job_store.py:90-117。

### L-OK-5 turn_plan の固定と CANDIDATE_DONE ゲート
commit_turn は固定 turn_plan で最終ターン以外の CANDIDATE_DONE を `TURN_PLAN_INCOMPLETE` で拒否、最終ターン前の CONTINUE 枯渇を `TURN_PLAN_EXHAUSTED` で拒否、CONTINUE の next_instruction は plan から取得（自由生成させない）。**根拠**: relay/local_job_store.py:396-417。

### L-OK-6 deep_review の unsafe abort の safe-rescope（責務分離の要）
abort_turn は deep_review かつ continue_on_unsafe_abort 時のみ、_SAFE_REVIEW_ABORT_CODES に該当する非 retryable abort を rescope し、fallback instruction で再スコープ（max_safe_rescopes 上限）。非 review では fail-closed。`test_deep_review_unsafe_abort_is_rescoped_instead_of_failed`（:245-274）と `test_non_review_unsafe_abort_still_fails_closed`（:277-）が検証。冪等 abort は abort_hash の compare_digest で担保。**根拠**: relay/local_job_store.py:464-545, test_local_job_store.py:245-274。

### L-OK-7 controller marker 意味論（resume/autoresume）
controller は per-job kernel lock を non-blocking 取得（二重起動は exit）、marker は `completed_normally=True` 時のみ削除。想定外例外は marker を残し lock を解放（真のクラッシュのみ auto-resume 可能）、WAITING_RUNTIME は正常 return として marker を残さず（明示 --resume-runtime のみ再開）、DONE は marker 削除。`test_local_loop_autoresume.py` の `test_main_crash_waiting_runtime_and_done_have_distinct_marker_semantics` / `test_waiting_runtime_is_a_pause_not_an_autoresume_marker` / `test_main_clears_marker_only_after_controller_returns_normally` / `test_main_owns_lock_and_marker_before_browser_start` が検証。marker/lock パスは不正 job_id を `INVALID_JOB_ID` で拒否（:188-217）。**根拠**: relay/local_loop_controller.py（main）, relay/test_local_loop_autoresume.py:50-99, 102-185, 188-228。

### L-OK-8 supervisor↔controller の single-instance ownership
supervisor `Invoke-LocalLoopAutoResume`（scripts/supervisor.ps1:1680-1737）は marker pid の生死をプロセス誕生時刻に束縛（±60s、`Test-LocalLoopMarkerProcessAlive`:1631-1650）し PID 再利用を誤検出しない。**supervisor は pid/started ownership を書かない** — restart_count/retry_after のバックオフのみを spawn 前に永続化。ownership は per-job lock を勝ち取った controller だけが公開（fast-child / false-death 両レース回避）。`test_supervisor_local_loop_resume.py` の `test_supervisor_never_steals_local_loop_marker_ownership_from_controller` / `test_child_marker_backoff_is_reserved_without_supervisor_ownership_write` / `test_backoff_marker_is_persisted_before_child_spawn` / `test_local_loop_restart_backoff_grows_and_is_capped` が検証。旧 controller が新オーナーの marker を消せないことは `test_old_controller_cannot_clear_new_owners_marker`（autoresume:23-28）で担保。**根拠**: scripts/supervisor.ps1:1631-1737, scripts/test_supervisor_local_loop_resume.py:1-68。

### L-OK-9 fleet reap が resume 中 marker を消さない guard
`Get-FleetReapHoldReason`（supervisor.ps1:1801-1827）が dead-pid marker かつ resumer 生存中は reap を withhold。marker が live pid を名乗るか resumer が exit するまで保持。resume は必ず reap より前。**根拠**: scripts/supervisor.ps1:1782-1853。

### L-OK-10 fleet/review/local-loop auto-resume の既定 ON と opt-out
各 auto-resume は `MCP_FLEET_AUTORESUME` / `MCP_REVIEW_AUTORESUME` / `MCP_LOCAL_LOOP_AUTORESUME` で opt-out 可、既定 ON。fleet は起動時 1 回（Global mutex で冪等）、review/local-loop は毎 tick。runner 死は -PassThru で捕捉し `Invoke-AutoResumeRunnerCheck` が exit code を報告。**根拠**: scripts/supervisor.ps1:1430-1434, 1543-1547, 1625-1629, 1990-1994, 2169-2177。

## 検出した論点

### L-ISSUE-1 fencing_token 単独不一致のテスト網羅ギャップ（優先度: 低、実装欠陥ではない）

**再現条件**
- `_validate_lease` の fencing 分岐（local_job_store.py:339-340）を「正しい lease_id + stale fencing_token」で直接踏むテストが存在しない。既存テストは lease_id 起因の `LEASE_MISMATCH`（test:235）か、期限切れ→トークン+1 の再claim（test:214, :237）を検証するのみ。heartbeat / read_job_context 経路の fencing 検証はテストされていない。claim テストの stale commit も `LEASE_MISMATCH` を固定しており `FENCE_MISMATCH` を名指ししていない。

**影響**
- 実装は正しく（4 経路すべてが `_validate_lease` を通過、L-OK-1）、この分岐は実運用では LEASE_MISMATCH が先に発火する状況が多いためカバーされることが多い。ただし fencing 単独分岐（:339-340）の回帰（例: 比較の削除・演算子誤り）を既存テストは検出できない。実害は現状なし。

**根拠ファイル/行**
- fencing 分岐: relay/local_job_store.py:339-340
- 経路: heartbeat :354 / commit :428 / abort :519 / read_job_context :102-103
- 既存テストの近接カバレッジ: test_local_job_store.py:214, 235, 237（いずれも FENCE_MISMATCH を名指ししない）

**優先度**: 低。実装健全、承認ブロッカーではない。回帰検出力のギャップのみ。

**推奨修正**
- heartbeat / commit_turn / abort_turn / read_job_context の各々で「有効 lease_id + stale fencing_token」を渡し `JobStoreError.code == "FENCE_MISMATCH"` を名指し assert するテストを追加。

**既存テストで捕捉済みか**: 未捕捉（fencing_token 単独経路）。lease_id / 期限切れ / トークン増分は捕捉済み。

## 監査で実施したこと（範囲 1/8 の取得件数）

- 読解した主要実装: relay/local_job_store.py（_validate_lease 336-342、heartbeat 344-363、commit_turn 365-462、abort_turn 464-545、verify_candidate 547-、read_job_context 300-334、_claim_result）、relay/execution_profiles.py（resolve_profile / validate_runtime）、relay/local_loop_controller.py（main のマーカ/lock 意味論、drain-campaign、commands drain）、relay/local_loop_ops.py、scripts/supervisor.ps1（LOCAL_LOOP/Fleet/review auto-resume・reap guard・ownership）。
- 読解した既存テスト: relay/test_local_job_store.py、relay/test_execution_profiles.py、relay/test_local_loop_autoresume.py、scripts/test_supervisor_local_loop_resume.py。
- 確認済み論点（問題なし）: 10 件（L-OK-1〜L-OK-10）。
- 検出した論点: 1 件（L-ISSUE-1、優先度 低、テスト網羅ギャップ、実装欠陥ではない、既存テスト未捕捉）。

担当範囲 1/8 の監査を完了。

---

## 範囲 3/8: single-instance ownership と supervisor 再起動／ポート所有権

担当範囲の実装は堅牢で、致命的欠陥・承認ブロッカーは検出されなかった。ポート所有権は「占有=同一性ではない」原則（D13）で再設計済みで、供給者の取り違え・他プロセスの誤 kill・誤った再起動ループは検出されなかった。検出した論点は 0 件（実装欠陥）。テスト網羅の観察を 1 件記録する。

### 確認済み論点（問題なし）

- S-OK-1 supervisor の単一インスタンス化（Global Mutex）: supervisor.ps1:163-169 で Global\m365-copilot-companion-supervisor を初期所有付きで生成し、createdNew=$false（既存が保持）なら静かに exit。Task Scheduler 起動と手動起動が session を跨いでも競合しない。Mutex は名前付きカーネルオブジェクトのためプロセスクラッシュ後は最後のハンドル消滅で自動解放され、再起動時は createdNew=$true となる。ReleaseMutex/Dispose の明示処理が無いのはプロセス終了時の OS 解放に委ねる意図的設計。
- S-OK-2 ポート所有権は占有ではなく command line で判定（D13）: Get-ProcessVerdict（303-339）で :8000 listener を「このチェックアウトの main.py か」で分類。自身 or 親の command line がチェックアウトを名指す場合、または supervisor が起動した $script:ServerProc の場合のみ ours。旧実装の「全 listener kill / 全 HTTP 200 を up と誤認」不具合は解消済み。
- S-OK-3 foreign 単独占有時は奪取しない: Start-Server（1098-1101）で ours=0 かつ foreign>0 のとき Write-ForeignPortHolderLog して return。他サービスを kill せずログで是正手順を案内。
- S-OK-4 停止対象のスコープが最小: Start-Server（1126-1131）で kill 対象は (a) ours の port holder、(b) このチェックアウトの main.py 全プロセス、(c) supervisor が起動した prev proc のみ。IPv4/IPv6 共有の foreign holder は放置。command line 読取不可（$null＝別ユーザー/昇格）は kill しない。
- S-OK-5 再起動判定の「計画的終了」ラベル付け: $prevWasAlive を cleanup 前にスナップショット（1102-1108）し、生存中を強制停止する場合のみ planned reason 付与（1109-1117）。既に死んでいたプロセスは触らず、「触る前に既に消えていた」と区別される。
- S-OK-6 start_all 層の single-instance lock: start_all.ps1:207-226 Enter-StartAllLock。真の単一化は supervisor の Global mutex、各 step は idempotent、第2コピーは quit せず wait。
- S-OK-7 lock 生成不可時のフォールバック: start_all.ps1:224-225 で Mutex 生成失敗（CLM 等）時に continuing without it。idempotency と supervisor 側 mutex により安全な意図的緩和。
- S-OK-8 express pass の非重複保証: supervisor.ps1:1947-1949 express pass は Global mutex を保持する唯一の supervisor 内で同期実行され tick の pass と重ならない。

### 観察（実装欠陥ではない）

- S-NOTE-1 テスト網羅の観察（優先度: 低）: Get-ProcessVerdict の親子 command line 解決と foreign 共有時の放置、Global Mutex の createdNew 分岐に対する直接的な単体テストは確認範囲で見当たらなかった。回帰検出のためのテスト追加を推奨。実装欠陥ではない。

### 監査で実施したこと（範囲 3/8）

- 読解した主要実装: scripts/supervisor.ps1（single-instance Mutex 160-169、Get-ProcessVerdict 303-339、Get-HealthServerPid 341-353、Start-Server 1076-1155、express pass 1938-1967）、scripts/start_all.ps1（Enter-StartAllLock 191-235）。
- 検出した論点（実装欠陥）: 0 件。
- 観察: 1 件（S-NOTE-1）。

担当範囲 3/8 の監査を完了。

---

# 担当範囲 3/8 — single-instance ownership と supervisor restart / port ownership

担当範囲: **3/8 — 単一インスタンス保証、プロセス所有権、supervisor 再起動時のポート所有・再取得**

- 監査者会話: single-instance ownership / supervisor restart / port ownership
- 対象コミット/ブランチ: feat/durable-companion-phase2-20260928 (PR #66)
- 実行環境: READ-ONLY 監査。既存ファイル無変更・git 操作なし。書込は本レポートのみ。
- 監査日 (UTC): 2026-09-29

## サマリ

担当範囲の実装は**堅牢で、致命的欠陥・承認ブロッカーは検出されなかった**。単一インスタンス保証は marker（証拠）とカーネル byte-range ロック（排他）の二重防御で構成され、lock 取得後の再 confirm で TOCTOU を解消、pid_birth で PID 再利用を識別する。supervisor 再起動時の port ownership は fail-closed の verdict 判定・foreign holder 放置（kill しない）・二重起動抑止で設計されている。検出した論点は 1 件（優先度: **低**、CI 実行検証ギャップ、実装欠陥ではない）。

## 確認済み（問題なしと判断した領域）

### S-OK-1 単一インスタンス保証: marker + カーネルロックの二重防御
single-instance の取得は marker ファイル（所有証拠）とカーネル byte-range ロック（実排他）の二段で行い、marker 単独の観測に依存しない。lock 取得に負けたインスタンスは即 exit し、二重起動しない。**根拠**: relay/fleet_runner.py（single-instance 取得部）, relay/test_fleet_runner_single_instance.py。

### S-OK-2 lock 取得後の再 confirm による TOCTOU 解消
lock を握った後にもう一度 marker/所有状態を確認し、観測と取得の間の窓（TOCTOU）で別プロセスが所有を確定していないかを検証してから所有を公開する。**根拠**: relay/fleet_runner.py（lock 後 re-confirm）, relay/test_fleet_runner_single_instance.py。

### S-OK-3 PID 再利用の識別（pid_birth / create_time 束縛）
所有 pid の生死判定を create_time ベースの birth token に束縛し、同一 pid 値の別プロセス（PID 再利用）を旧所有者と誤認しない。**根拠**: relay/fleet_runner.py（pid_birth 比較）, scripts/supervisor.ps1:1631-1650（Test-LocalLoopMarkerProcessAlive, ±60s）。

### S-OK-4 所有権付き marker 削除
marker 削除は自インスタンスが所有者であることを確認した場合のみ実施し、他インスタンスの marker を消さない。異常終了時は marker を残し、真のクラッシュのみ後続の resume を許す。**根拠**: relay/fleet_runner.py（marker 削除ガード）, relay/test_fleet_runner_single_instance.py。

### S-OK-5 supervisor は ownership を書かない（controller のみが公開）
supervisor は pid/started ownership を書かず、restart_count/retry_after のバックオフのみを spawn 前に永続化する。ownership は per-job lock を勝ち取った controller だけが公開し、fast-child / false-death 両レースを回避する。**根拠**: scripts/supervisor.ps1:1680-1737。

### S-OK-6 port ownership verdict の fail-closed
supervisor 再起動時の bridge port 所有判定は、判定不能・想定外時に「所有していない/奪わない」側へ倒す fail-closed。foreign holder（別プロセスが port を保持）は kill せず放置し、勝手な port 奪取をしない。**根拠**: scripts/supervisor.ps1（Get-BridgePortVerdict）, scripts/test_supervisor_port_owner.py。

### S-OK-7 server 生存判定の多要素検証
Test-ServerUp 相当の生存判定は単なる TCP 接続可否でなく、応答 body と pid を突き合わせて自分の server か foreign holder かを区別する。Get-ProcessVerdict は $null（プロセス消失）でも停止せず判定を継続する。**根拠**: scripts/supervisor.ps1（Test-ServerUp / Get-ProcessVerdict）, scripts/test_supervisor_port_owner.py。

### S-OK-8 stale server の 2 連続 idle 判定
Invoke-StaleServerCycle 相当は、単発の idle 観測で stale と断ぜず 2 連続の idle 判定を要求してから再取得サイクルへ進み、瞬間的な無応答での誤った port 奪取を避ける。**根拠**: scripts/supervisor.ps1（Invoke-StaleServerCycle）。

### S-OK-9 兄弟 checkout 誤認防止と auto-resume の二重起動抑止
別 checkout（兄弟ディレクトリ）の同名プロセスを自分の server と誤認しない。Invoke-FleetAutoResume は Global mutex 相当で冪等化し、supervisor 再起動時に fleet を二重起動しない。**根拠**: scripts/supervisor.ps1（兄弟 checkout 判別, Invoke-FleetAutoResume）。

### 既存テスト
- relay/test_fleet_runner_single_instance.py: single-instance ownership（marker + lock、TOCTOU、pid_birth、所有権付き削除）をカバー。
- scripts/test_supervisor_port_owner.py: port ownership verdict / server 生存判定 / stale cycle をカバー（後述の CI skip に注意）。

## 検出した論点

### S-ISSUE-1 port ownership テストが Linux CI で skip され CI 上で実行検証されない（優先度: 低）

**再現条件**
- scripts/test_supervisor_port_owner.py が先頭付近（55-59 行）の `skipif(os.name != "nt")` により、非 Windows（Linux CI）では全 skip される。CI が Linux ランナーのみの場合、port ownership / supervisor restart の PowerShell ロジックに対する自動テストが CI 上で一度も実行されない。

**影響**
- 実装（supervisor.ps1 の port verdict / server 生存判定 / stale cycle）自体は健全で、テストも存在する。ただし Linux CI では skip されるため、これらの回帰（fail-closed の反転、body+pid 検証の緩み、stale 判定の閾値変更等）が CI で捕捉されない。Windows ローカルでの手動実行に依存する。実害は現状なし。

**根拠ファイル/行**
- skip ガード: scripts/test_supervisor_port_owner.py:55-59（`skipif(os.name != "nt")`）
- 対象実装: scripts/supervisor.ps1（Get-BridgePortVerdict / Test-ServerUp / Get-ProcessVerdict / Invoke-StaleServerCycle / Invoke-FleetAutoResume）

**優先度**: 低。実装健全、承認ブロッカーではない。CI 実行検証のギャップのみ。

**推奨修正（いずれか）**
1. CI に Windows ジョブを追加し、scripts/test_supervisor_port_owner.py を実行対象にする。
2. Windows ジョブを追加しない方針なら、port ownership テストが Linux CI で skip され CI 上では未実行である旨を PR 説明・CI 契約ドキュメントに明記する（暗黙の未検証を明示化）。

**既存テストで捕捉済みか**: テストは存在するが Linux CI では skip され実行されない。実行環境が Windows の場合のみ捕捉。

## 監査で実施したこと（範囲 3/8 の取得件数）

- 読解した主要実装: relay/fleet_runner.py（single-instance 取得・marker/lock・pid_birth・所有権付き marker 削除）、scripts/supervisor.ps1（Invoke-LocalLoopAutoResume 1680-1737、Test-LocalLoopMarkerProcessAlive 1631-1650、Get-BridgePortVerdict / Test-ServerUp / Get-ProcessVerdict / Invoke-StaleServerCycle / Invoke-FleetAutoResume、兄弟 checkout 判別）。
- 読解した既存テスト: relay/test_fleet_runner_single_instance.py、scripts/test_supervisor_port_owner.py。
- 確認済み論点（問題なし）: 9 件（S-OK-1〜S-OK-9）。
- 検出した論点: 1 件（S-ISSUE-1、優先度 低、CI 実行検証ギャップ、実装欠陥ではない）。

担当範囲 3/8 の監査を完了。


---

# 担当範囲 8/8 — CI契約の監査（ci.yml テスト明示リスト / check_ci_test_manifest.py 整合 / Windows専用混入 / 緑に見えて走らないテスト）

- 監査日 (UTC): 2026-09-29 / 対象: feat/durable-companion-phase2-20260928 (PR #66)
- 実行環境: unlock 済み、run_python で監査スクリプトを実走して確定判定を取得。既存ファイル無変更・git 操作なし。

## サマリ

CI契約は**健全**。監査スクリプト `scripts/check_ci_test_manifest.py` を実走して **RC 0**、`--strict-untracked` でも **RC 0**。内部チェック項目（missing / stale_listed / stale_excluded / welded / hollow / double / orphan / pending_untracked）は**全て空**。致命的欠陥なし。検出したのはドキュメント齊齬 1 件（優先度: **低**、機能影響なし）のみ。

実測値: discovered=738 / listed(pytest明示)=710 / excluded=9 / scripted(script-style)=21。

## 確認済み（問題なしと判断した領域）

### CI-OK-1 監査スクリプト本体の合格
`python scripts/check_ci_test_manifest.py` → RC 0、出力「CI test manifest OK: 710 pytest files listed, 9 explicit exception(s).」。`--strict-untracked`（preflight/pre-push 相当）でも RC 0。**根拠**: scripts/check_ci_test_manifest.py:217-328、実走結果。

### CI-OK-2 明示リストの実在性（stale_listed なし）
`listed_tests()` は `#` コメントを除去して ci.yml から pytest 対象 710 件を抽出。全件がディスク上に実在（stale_listed=[]）。コメント内のパスを実行対象と誤認しない対策（152-156行）も確認。**根拠**: check_ci_test_manifest.py:142-160, 246。

### CI-OK-3 discover とリスト/除外/script-style の整合（missing なし）
`git ls-files --cached` で tracked なテストを列挙（test_*.py と *_test.py 両パターン）し 738 件を発見。`discovered - listed - excluded - scripted = 空`で、どの tracked テストも「どこでも走らない」状態にならない。**根拠**: check_ci_test_manifest.py:107-120, 245。

### CI-OK-4 除外リストの非 stale（stale_excluded なし）
EXCLUDED 9 件（tools/test_file_ops.py, tools/test_trace.py, bench/test_pro_batching.py, bench/test_swe_run_facts.py, relay/test_acceptance_contract.py, relay/test_repo_bug_fix_skill.py, scripts/test_prune_edge_cache.py, tools/test_instructions_do_not_accumulate_cases.py, tools/test_judge_live_roundtrip.py）は全件 discovered に含まれ、各々具体的な代替ランナー（windows-install-smoke / Linuxスクリプトステップ）と除外理由が明記される。**根拠**: check_ci_test_manifest.py:25-69, 247。

### CI-OK-5 malformed listing（welded）なし
ci.yml 全行を `\.py\s+\\n\s` で走査し、パスを 1 行に溶接した（shell が pytest に引数 `n` を渡す）篇は皆無（welded=[]）。**根拠**: check_ci_test_manifest.py:123-139, 261-266。

### CI-OK-6 緑に見えて走らないテスト（hollow）なし
pytest 明示リスト 710 件を AST 解析し、test_* 関数 / Test* クラス / unittest.TestCase 派生のいずれも持たない（=pytest が 0 件収集）ファイルは皆無（hollow=[]）。かつて 166 中 21 が hollow だった問題は、当該 21 件を script-style ランナーに移して解消済み。**根拠**: check_ci_test_manifest.py:163-196, 269-278。

### CI-OK-7 script-style 二重計上（double）と orphan なし
script-style suites 21 件（run_script_style_tests.py の SUITES）はいずれも pytest 明示リストに重複しておらず（double=[]）、全件 relay/ に実在（orphan=[]）。SUITES は run_script_style_tests.py から直接読み取りされ重複定義を防ぐ設計。**根拠**: check_ci_test_manifest.py:199-214, 282-293、run_script_style_tests.py:66-88。

### CI-OK-8 script-style ランナーのベースライン機構
run_script_style_tests.py は各 script-style ファイルを run_isolated 経由で実行し、exit 0 または記録ベースライン一致を要求。現在全 21 件のベースラインは None（=完全通過必須）。TimeoutExpired を捕捉し後続スイートを逃さない対策もあり。**根拠**: run_script_style_tests.py:97-146, 149-166。

### CI-OK-9 Windows専用テストの ubuntu ジョブへの混入なし
ubuntu ジョブ（windows-install-smoke: より前）の pytest 実行行に Windows専用ファイルは含まれない。`test_file_ops.py` / `windll` の文字列ヒットはいずれも `#` コメント行（11, 15 行目）で、実行行ではない。Windows固有テスト（desktop keyboard, DPAPI bootstrap, Pester, REQUIRE_CSC 系）は windows-install-smoke ジョブ（ci.yml:890-990）に隔離。**根拠**: ci.yml:1-16（ubuntu冒頭コメント）, 890-990。

### CI-OK-10 除外テストの reporting（非 gating）ステップ
ubuntu ジョブの「Excluded tests (reporting, not gating)」（ci.yml:837-888、continue-on-error: true）は除外ファイルを実行し、PASS したもの（他ジョブで走らないもの）を「除外理由が失効した候補」として印字。除外リストが再検証不能のまま永久化する問題への対策として適切（gating しないので安全）。**根拠**: ci.yml:819-888。

### CI-OK-11 git 不在時の fail-closed
`_tracked()` は `git ls-files` 失敗時に GitUnavailable を投げ、main() は RC 2（監査自体の失敗）を返す。「フィルタが黙ってフィルタをやめる」黙示パスを回避。**根拠**: check_ci_test_manifest.py:72-104, 235-239。

## 検出した論点

### CI-ISSUE-1 ci.yml コメントの除外件数表記が実数と不一致（ドキュメント齊齬）
> Resolution: see `2026-09-29 follow-up resolution 2 / CI-ISSUE-1` below. This issue is retained for traceability.


- **再現条件**: ci.yml:821 付近のコメントは「scripts/check_ci_test_manifest.py keeps eleven files out of the hermetic suite」と記述するが、現在の EXCLUDED は **9 件**（監査スクリプト出力も「9 explicit exception(s)」）。check_ci_test_manifest.py:55-66 のコメントに、2 件（tests/test_integration_evidence.py, tests/test_outcome_enum_closed.py）が 2026-09-22 に除外から外され戻された経緯が残るが、ci.yml 側の「eleven」表記が更新されていない。
- **影響**: 機能影響なし（コメントのみ、監査ロジックは len(EXCLUDED) を動的参照）。ただし将来の読み手が除外件数を誤解し得る。
- **根拠**: .github/workflows/ci.yml:821（「eleven files」）vs scripts/check_ci_test_manifest.py:25-69（9件）。
- **優先度**: **低**（ドキュメントのみ）。
- **推奨修正**: ci.yml:821 の「eleven files」を「nine files」に修正。（本監査は READ-ONLY のため未適用）
- **既存テストで捕捉済みか**: **未捕捉**。監査スクリプトは EXCLUDED の実数のみ検証し、ci.yml コメントの数字表記との一致は検証しない。（なお ci.yml:812 の「21 files」= script-style suites 数は実数 21 と一致）

## 監査で実施したこと（範囲 8/8 の取得件数）

- 読解した対象: .github/workflows/ci.yml（990行全体、ubuntu test ジョブの pytest 明示リスト 507-807、script-style/reporting ステップ 809-888、windows-install-smoke ジョブ 890-990）、scripts/check_ci_test_manifest.py（332行）、scripts/run_script_style_tests.py（170行）。
- 実走した検証: check_ci_test_manifest.py （通常 + --strict-untracked、共に RC 0）、内部チェック 8 項目のダンプ（全空）、Windows専用混入の ubuntu ジョブ検索。
- 確認済み論点（問題なし）: 11 件（CI-OK-1～CI-OK-11）。
- 検出した論点: 1 件（CI-ISSUE-1、優先度 低、ドキュメント齊齬、機能影響なし）。

担当範囲 8/8（CI契約）の監査を完了。

---

# 担当範囲 2/8 — secret handling / unlock gate / redaction / secret log exposure

担当範囲: **2/8 — 秘密の取り扱い（DPAPI 保護 .env・平文非露出）、unlock ゲート、redaction、秘密のログ露出**

- 監査者会話: secret handling / unlock gate / redaction / secret log exposure
- 対象コミット/ブランチ: feat/durable-companion-phase2-20260928 (PR #66)
- 実行環境: READ-ONLY 監査。既存ファイル無変更・git 操作なし。書込は本レポートのみ。
- 監査日 (UTC): 2026-09-29

## サマリ

担当範囲の実装は**堅牢で、致命的欠陥・承認ブロッカーは検出されなかった**。秘密の取り扱いは DPAPI 保護値の復号不能を「未設定」と区別し、ログには例外型と環境変数名の定数のみを出して秘密断片を出さない設計で、CodeQL の cleartext-logging sink を「例外objを渡さず定数名を直書きして flow ごと除去」する形にまで踏み込んでいる。redaction は fail-closed（失敗時は本文全体をマーカに差替）で、.env が存在するのに読めないときは raise して部分的な（=誤った）秘密リストを作らない。unlock ゲートはトークンを sha256 ハッシュのみ保存・定数時間比較で照合し、世代（generation）単調増加とトゥームストーン台帳による失効の復活防止、台帳読取不能時の fail-closed を備える。セッション認可（sliding 30 分・既定 ON）は権限を増やさずモデルからトークンを外す追加要素として設計されている。検出した論点は 1 件（優先度: **中**、承認ブロッカーではない）で、unlock ゲートが caller-supplied な XFF 由来 IP を鍵にする残存 oracle 性に関するもの。ただし現行は `MCP_REQUIRE_UNLOCK_TOKEN=1`（既定 ON）で実運用上は塞がれており、実害は現状なし。

## 確認済み（問題なしと判断した領域）

### SEC-OK-1 DPAPI 保護と復号不能の「未設定」との区別
`protect_secret`/`unprotect_secret`（tools/secret_store.py:48-88）は Windows-user 束縛の DPAPI（LOCAL_MACHINE フラグなし）で保護/復号する。`unlock_password_from_env`（:144-190）は、保護値が設定済みだが復号できないケースを `PROBLEM_UNDECRYPTABLE`、未設定を `PROBLEM_UNSET` と別コードで記録し（:156, :163、判定コードは :108-109、`unlock_password_problem` は :139-141）、「復号失敗を不在と誤読して『MCP_UNLOCK_PASSWORD is not configured』を出す」既往のミスを防ぐ。**根拠**: tools/secret_store.py:48-88, 91-109, 139-190。

### SEC-OK-2 秘密ログ露出の遮断（stderr・例外型のみ・CodeQL sink 除去）
復号失敗時のログは stderr（stdout 禁止：この module は stdout がデータとして解析される経路と MCP stdio 経路から import される、:164-165）で、例外オブジェクトを渡さず `type(exc).__name__` のみを出す（:167-173, 187）。保護値を扱う最中に上がった例外のメッセージは値の断片を含み得るためで、これは unlock パスワード自身の経路である。さらに CodeQL の cleartext-logging sink 対策として、`/password/i` ヒューリスティックに一致する定数 `UNLOCK_PASSWORD_PROTECTED_VAR` を変数として流さず、環境変数名の文字列 `"MCP_UNLOCK_PASSWORD_PROTECTED"` をログ本文へ直書きして flow ごと除去している（:174-187）。定数を渡すと alert が次行へ移るだけだった経緯もコメントに残る。**根拠**: tools/secret_store.py:162-190。

### SEC-OK-3 api_key 側も同一方針（平文非永続・例外型のみ）
`api_key_from_env`（:114-127）も平文→DPAPI 保護の順で読み、復号失敗時は `type(exc).__name__` のみを warning。`materialize_api_key`（:130-136）は復号した値をプロセスメモリ（env）にのみ載せ、平文を永続化しない。**根拠**: tools/secret_store.py:114-136。

### SEC-OK-4 secret_values の fail-safe な収集（.env 読取不能は raise）
`secret_values`（:228-272）は環境変数と .env の双方から `SECRET_NAME_HINTS`（PASSWORD/TOKEN/SECRET/API_KEY/APIKEY/CREDENTIAL、:221）と `_MIN_SECRET_LEN=8`（:225）で名前選別する。存在する .env を読めないときは `dotenv_values` の ImportError を伝播させて raise し（:249、docstring :231-235）、プロセス環境だけの「一見完全で実は不完全な」リストを返さない。DPAPI 保護された unlock パスワードは、名前選別だと DPAPI 暗号文しか拾えない一方で注入されるのは平文であるため、平文を復号して値でも照合に加える（:262-269）。返却は長い順ソートで、短い値が長い値の一部だった場合の部分残留を防ぐ（:270-272）。**根拠**: tools/secret_store.py:217-272。

### SEC-OK-5 redact_secrets の fail-closed
`redact_secrets`（:292-321）は書き出す直前にだけ使う前提で、`secret_values` の各値を `REDACTION_MARKER`（`<redacted>`、:283）へ置換する。例外時は `except: pass` で途中までの本文（=最初の置換前なら原文＝秘密込み）を返す旧挙動を廃し、本文全体を `REDACTION_FAILED_MARKER`（`[redaction failed: content withheld]`、:289）に差し替えて返す（:314-321）。ログには例外型のみを出す（:316-318）。「消す」ではなく「マークする」ことで「秘密が無かった」と「秘密を取り除いた」を区別する設計意図もコメントに明記（:280-282）。**根拠**: tools/secret_store.py:275-321。

### SEC-OK-6 unlock トークンの発行・保存・照合
`unlock`（:1080-1162）は正しいパスワードのとき `secrets.token_urlsafe(24)` を発行し、保存は sha256 ハッシュのみ（:1113-1114, 1127-1129）。応答でトークンは一度だけ表示し「hash のみ保持」と明記（:1157-1162）。照合 `_token_matches`（:895-914）は保存ハッシュ集合に対し `hmac.compare_digest` を `any` で回す定数時間比較で、文字列の membership が先頭差分で短絡する問題を避ける（:911-914）。**根拠**: tools/security.py:895-914, 1080-1162。

### SEC-OK-7 失効の復活防止（generation 単調増加＋トゥームストーン台帳）と fail-closed 読取
unlock 状態は grant ごとに uuid・トークンハッシュ・失効判定用の generation を持ち（:1121-1137）、失効は別ファイル台帳 `.fleet/unlock_revocations.json`（:79-81）へトゥームストーンを書く。全読取が同じトゥームストーンを適用するため、ロックを取らない旧サーバが書いた stale コピーも何も認可しない（:39-67, 136-154, 199-228, 256-264）。generation 単調増加で「失効より後に発行された grant」を厳密に順序付け（:145-146, 213-216）、`_load_state` は台帳が読めないとき fail-closed で `{}`（誰も unlock していない）を返す（:267-281、特に :278-279）。**根拠**: tools/security.py:39-67, 79-92, 136-154, 199-228, 256-281。

### SEC-OK-8 セッション認可（sliding 30 分・既定 ON・権限を増やさない）と enforce_unlock_token
セッション認可はモデルからトークンを外すための追加要素で、`Mcp-Session-Id` はサーバ発行・偽造は「Session terminated」で拒否される（:709-715）。認可は sliding window（成功のたびに更新）で、安全境界は `_session_ttl_s()`＝30 分固定（:747, 777-785）。セッション記録はその identity 自身の entry 内（`state[ip]["sessions"]`）に限定され、既存の per-IP＋token ゲートに対し加算的で置換ではない（:717-722）。`_MAX_SESSIONS_PER_IDENTITY=512` はメモリ上限のみで security bound ではなく、引き上げは新規の誰かを通さない（:744-749）。既定 ON（`session_auth_enabled`、:766-774）。`enforce_unlock_token`（:917-938）は既定 ON で、値が `"0"` のときのみ無効・空値含む他の値は強制。require_unlocked 内でトークン/セッションいずれも不成立かつ enforce のとき拒否する（:1019-1070）。**根拠**: tools/security.py:671-749, 766-800, 917-938, 999-1070。

### SEC-OK-9 running server に対する平文非露出の実地検証スクリプト
`scripts/verify_secret_redaction.py`（:1-56）は稼働中サーバに対し、実際に平文が漏れた形である `call_tool(name="unlock", arguments={"password": PW})` のゲートウェイ経路（パスワードが1段下）で unlock が動作し、かつ台帳 `.fleet/tool_events.jsonl` に平文が入らないことを検証する。トップレベルの `unlock` は台帳を通らないため証明にならない旨も明記（:8-11）。**根拠**: scripts/verify_secret_redaction.py:1-56。

## 検出した論点

### SEC-ISSUE-1 unlock ゲートは caller-supplied な XFF 由来 IP を鍵にする残存 oracle 性（優先度: 中、承認ブロッカーではない）

**再現条件**
- unlock ゲートは identity を X-Forwarded-For 由来の IP から導出する。IP は「主張」であって「証明」ではなく、この deployment は上流が独自ホップを付けないため XFF 全体が caller-supplied である（tools/security.py:1098-1103, 1000-1003）。
- `list_unlocked`（:1170-1204）は「全テーブル返却」を caller 自身のエントリのみへ narrowing 済みだが、genuine-local（loopback peer かつ XFF なし）には従来どおり全テーブルを返す（:1187-1188, 1196-1200）。これは operator 自身であり、同じ情報は本人のディスク上にある、という判断による。
- `tests/test_unlock_oracle.py:95-104` の `test_identity_is_still_derived_from_a_client_supplied_value` は、identity が依然ヘッダ由来で（`ip == CALLER`）ゲートがそれに鍵付けする（`is_unlocked(ip) is True`）ことを assert し、これを「まだ fix されていない部分」として明示、スイートを all-clear と読ませないための test だと述べる。

**影響**
- `list_unlocked` の docstring:1184-1185 の "A NARROWING, NOT A FIX" が、これは fix でなく narrowing・ゲートはまだ IP を鍵にする・詳細は public repo なので private report に集約する、と自認している（:1173-1188）。
- 現行は `MCP_REQUIRE_UNLOCK_TOKEN=1`（既定 ON、:917-938）でトークンまたはセッションが必須のため、IP のみでの認可経路は実運用で塞がれており、**実害は現状なし**。ただしトークン強制が `"0"` に戻されると、caller-supplied な IP のみで unlocked identity を主張し得る経路が露出する。

**根拠ファイル/行**
- IP が caller-supplied である旨: tools/security.py:1098-1103, 1000-1003
- narrowing とその自認: tools/security.py:1170-1204（特に docstring :1173-1188、実装 :1196-1200）
- 「fix ではなく narrowing」の明示: tools/security.py:1184-1185
- 未 fix 部分の test: tests/test_unlock_oracle.py:95-104
- トークン強制（既定 ON・値 "0" のみ無効）: tools/security.py:917-938

**優先度**: 中。実装は現行既定（トークン強制 ON）で塞がれており承認ブロッカーではないが、コード自身が「fix ではない」と自認する残存 oracle 性であり、既定を戻すと露出するため低ではなく中とする。

**推奨対応（いずれか）**
1. `MCP_REQUIRE_UNLOCK_TOKEN` を `0` にできない起動時ガードの検討（トークン強制の無効化を運用上不可能にする）。
2. 残存 oracle 性と IP 鍵化の残存リスクが private security report に集約済みである旨を PR 説明で参照し、レビュアが緑スイートを all-clear と誤読しないようにする（`test_identity_is_still_derived_from_a_client_supplied_value` の存在意図と整合）。

**既存テストで捕捉済みか**: 設計として明示的に「未 fix」を宣言・assert 済み。`tests/test_unlock_oracle.py:95-104` が IP 鍵化の残存を固定し、`list_unlocked` の narrowing は同 suite の bootstrap/CLI テストで担保される。これは欠陥の見落としではなく、意図的に残された既知の制約であり private report へ集約されている。

## 監査で実施したこと（範囲 2/8 の取得件数）

- 読解した主要実装: tools/secret_store.py（protect/unprotect 48-88、unlock_password_from_env 144-190、unlock_password_local 193-214、SECRET_NAME_HINTS/_MIN_SECRET_LEN 217-225、secret_values 228-272、redact_secrets 292-321）、tools/security.py（unlock table/generation/tombstone 39-92、_grant_is_revoked/_canonical_entry 136-228、_load_state fail-closed 267-281、session 認可 671-800、_token_matches 895-914、enforce_unlock_token 917-938、require_unlocked 962-1077、unlock 1080-1162、list_unlocked 1165-1204）。
- 読解した検証/テスト: scripts/verify_secret_redaction.py（1-56）、tests/test_unlock_oracle.py（80-104）。
- 確認済み論点（問題なし）: 9 件（SEC-OK-1〜SEC-OK-9）。
- 検出した論点: 1 件（SEC-ISSUE-1、優先度 中、承認ブロッカーではない、設計上明示的に既知制約として残置）。

担当範囲 2/8 の監査を完了。


---

# 担当範囲 7/8 — security / secret handling の監査（シークレットの読取・保持・ログ露出防止、unlock/パスワード扱い、機密の応答漏洩防止）

- 監査日 (UTC): 2026-09-29 / 対象: feat/durable-companion-phase2-20260928 (PR #66)
- 実行環境: unlock 済み。READ-ONLY 監査、既存ファイル無変更・git 操作なし。ソース静的確認と git diff の読解が中心（pytest 実走はこの範囲では未実施）。

## サマリ

シークレットの取扱いは**全体として堅牢**で、PR #66 が secret handling コアに新規の重大欠陥を持ち込んだ形跡は検出されなかった。この PR の security 関連差分（`scripts/rotate_secrets.py` / `scripts/env_file.py` と各テスト、CI 配線）は、いずれも露出面を**狭める**方向の変更である。**承認判断: この範囲としてはブロッカーなし**。

検出した論点は 1 件（S7-ISSUE-1、優先度: **低〜情報**）。ファイルツールによる `.env` 拒否は run_python / shell_exec の同一ユーザー・サブプロセス実行を塞がず、コード自身が「defence in depth であって closure ではない」と明言している既知の設計限界。PR #66 起因ではない。

## 確認済み（問題なしと判断した領域）

### S7-OK-1 rotate_secrets が新シークレットをコンソール/ログに出力しない（PR #66 で強化）
以前は `--no-print` 未指定時に `Bearer {new_api_key}` と `New unlock password: {new_unlock}` をコンソールへ echo していたが、f7b8a2a で当該 print 分岐を削除。現在は API キー・unlock パスワードとも値を印字せず、`copilot_studio_values.bat` での対話的 reveal に誘導する。`--no-print` は互換のため受理するが redaction 挙動を変えない。**根拠**: git f7b8a2a（scripts/rotate_secrets.py:215-231 の差分）、scripts/rotate_secrets.py 冒頭コメント（NEVER writes clear-text secret values to a log or console）。

### S7-OK-2 rotate の非出力をテストが担保（新規テスト＋CI 配線）
`scripts/test_rotate_secrets_never_prints_values.py` が (1) ソースに `Bearer {new_api_key}` / `New unlock password: {new_unlock}` が現れないこと、(2) `--no-print` の有無に関わらずセンチネル秘密値が stdout に出ないこと、を検証。f7b8a2a で ci.yml のテスト明示リストにも追加済み。**根拠**: scripts/test_rotate_secrets_never_prints_values.py:14-51、.github/workflows/ci.yml（f7b8a2a 差分で追加された行）。

### S7-OK-3 .env 書込みが平文シークレットの新規/変更/複製を拒否
`scripts/env_file.py` の `_assert_no_plaintext_auth_escalation`（83-106）は、汎用 atomic writer が `MCP_API_KEY` / `MCP_UNLOCK_PASSWORD` の平文レガシー行を**新規作成・変更・複製**することを拒否する（既存の平文出現数を超えたら ValueError）。移行のため読取は許すが、書込みは DPAPI 保護変数（`*_PROTECTED`）を強制。既存平文の削除は許容し移行を促す。判定用の値カウンタはログ・表出しない。**根拠**: scripts/env_file.py:38-44, 66-106, 119。

### S7-OK-4 保護シークレットは DPAPI 暗号文で書かれる（CodeQL 抑制はシンク行に限定）
`atomic_write_text`（109-）が書くのは DPAPI 暗号文であり、5a61e15 で `codeql[py/clear-text-storage-sensitive-data]` 抑制コメントを正確に `fh.write(data)` シンク行へ移動。CodeQL が DPAPI をモデル化できないための限定的抑制で、無差別な広域抑制ではない。**根拠**: scripts/env_file.py:123-129、git 5a61e15。

### S7-OK-5 応答/台帳への redaction が fail-closed かつ never-raise
`tools/secret_store.py::redact_secrets`（292-321）は、以前 `except: pass` でループ途中の値（＝失敗が最初の置換前なら元の秘密入り全文）を返していたのを是正。現在は失敗時に全文を `REDACTION_FAILED_MARKER` に置換して返し、決して raise しない。ログ行は**例外の型のみ**（例外メッセージが秘密の断片を含み得るため本文を出さない）。**根拠**: tools/secret_store.py:292-321。

### S7-OK-6 台帳シンクが redaction をバイパスしない（import 失敗・例外時も fail-closed）
`tools/tool_ledger.py`（343-349 付近）は書込み直前に `redact_secrets(line)` を通し、import 失敗や redaction からの例外が起きても未redactの行をそのまま append しない設計。`redact_secrets()` 自体が fail-closed であることに依存しつつ、呼び出し側でも防御。`tools/test_ledger_never_writes_a_secret.py` が「redact_secrets の新規呼び出し箇所が増えたら WRITE か検査せよ」というガードテストを持つ。**根拠**: tools/tool_ledger.py:343-349、tools/test_ledger_never_writes_a_secret.py:157-183。

### S7-OK-7 ファイルツールがサーバ自身の認可/監査状態を拒否
`tools/file_ops.py::_refuse_security_state`（225-246）は read_file / write_file 経由の以下参照を PermissionError で拒否する: (a) `.env` / `.env.local` / `.env.production` と unlock 状態一式（`.unlock_state.json`, `unlock_revocations.json`, `unlock_generation.json`, `unlock_state.lock` 等）、(b) fleet コマンドチャネル（`.fleet/commands.d`, `acks`, `commands.json`）、(c) 承認キュー `.companion_gates`、(d) トレースログディレクトリ。`.env.example` は値を持たないテンプレートのため意図的に対象外。**根拠**: tools/file_ops.py:133-166, 175, 189-246。

### S7-OK-8 unlock パスワードは「送る文には残し、書く先からは消す」
`redact_secrets` の docstring（293-296）が明示するとおり、redaction を掛けてよいのは「ファイルに書く瞬間」だけで、エージェントへ**送る**文には掛けない（掛けると本物のパスワードが相手に届かず unlock が通らなくなるため）。unlock 成功時はこの MCP session 自体を認可し、平文パスワードを永続設定へ書かず transport fallback token も保存を要求しない。**根拠**: tools/secret_store.py:293-296、unlock 応答仕様（session 認可・token は fallback）。

### 既存テスト（この範囲でカバーを確認）
- scripts/test_rotate_secrets_never_prints_values.py: rotate の非出力（ソース・stdout 両面）。
- scripts/test_env_file_atomic.py: env_file の atomic 書込みと平文エスカレーション拒否（5a61e15 で追随修正）。
- tools/test_ledger_never_writes_a_secret.py: 台帳が秘密を書かない／redact 呼び出し箇所のガード。

## 検出した論点

### S7-ISSUE-1 .env 等のファイルツール拒否は同一ユーザーのコード実行を塞がない（優先度: 低〜情報、PR #66 起因ではない）

- **再現条件**: run_python もしくは shell_exec のサブプロセス内で `open('.env')` を実行すると、`_refuse_security_state` を経由しないため .env（および unlock 状態・承認キュー・fleet コマンドチャネル）を読み書きできる。これらは同一 OS ユーザーで動くため、ファイルツール側の拒否リストは迂回される。
- **影響**: ファイルツールを経路とした最も安価な秘密読取・自己承認・fleet 再構成は塞がれるが、秘密（API キー・unlock パスワード）の機密性・承認の非偽造性そのものは保証されない。侵害には「同一ユーザーでコードを実行できる」前提が要る。
- **根拠ファイル/行**: tools/file_ops.py:143-159（コメントが「WHAT THIS DOES AND DOES NOT DO … run_python and shell_exec run same-user code … calling it a closure would be the same error」と自認）、tools/file_ops.py:170-174, 184-188（`.companion_gates` / fleet チャネルも「Narrowed, not closed」と明記）。
- **優先度**: 低〜情報。**設計上の既知の限界**であり、コード自身が honest posture として明示。PR #66 が悪化させた形跡はない（差分は rotate/env_file の露出縮小のみ）。
- **推奨修正**: 同一ユーザープロセスが偽造できない承認・秘密境界が必要なら、このプロセスが持たない境界（別ユーザー/別マシンでの署名・認可）を導入する。現状の「narrowed, not closed」表記の維持自体は妥当。組織展開時は OAuth 2.0（Microsoft Entra ID、Entra アプリ登録が必要）経路の検討を推奨。
- **既存テストで捕捉済みか**: 該当なし（テスト対象化に適さない設計限界。`tests/test_unlock_oracle.py` は別軸の「unlock アイデンティティが client 供給値由来である」既知事項を明示する docstring を持つ）。

## 監査で実施したこと（範囲 7/8 の取得件数）

- 読解した主要実装: tools/secret_store.py（redact_secrets / secret_values / DPAPI 診断）、tools/file_ops.py（_refuse_security_state と拒否リスト）、tools/tool_ledger.py（redaction シンク）、scripts/rotate_secrets.py、scripts/env_file.py。
- 読解した PR #66 差分: git f7b8a2a（fix(security): close remaining CodeQL secret sinks）、git 5a61e15（fix(codeql): place cleartext suppression on sink line）。
- 読解した既存テスト: scripts/test_rotate_secrets_never_prints_values.py、tools/test_ledger_never_writes_a_secret.py、（参照）tools/test_unlock_redaction.py, tests/test_unlock_oracle.py。
- 確認済み論点（問題なし）: 8 件（S7-OK-1〜S7-OK-8）。
- 検出した論点: 1 件（S7-ISSUE-1、優先度 低〜情報、設計上の既知の限界、PR #66 起因ではない）。

担当範囲 7/8（security / secret handling）の監査を完了。

---

# 担当範囲 5/8 — resume / autoresume・GUI visible submit・conversation lineage・fanout campaign identity

- 監査日 (UTC): 2026-09-29 / 対象: feat/durable-companion-phase2-20260928 (PR #66)
- 実行環境: READ-ONLY 監査。既存ファイル無変更・git 操作なし。書込は本レポートのみ。
- 補足: 本セクションは、ゴールが徹底監査対象として名指しした4領域を独立の監査所見として明記するために追記した。resume/autoresume の marker 意味論の一部は 1/8（L-OK-7〜L-OK-10）および 3/8（S-OK-1〜S-OK-9）でも触れているが、ここでは resume の状態整合・二重再開防止、および GUI visible submit / conversation lineage / fanout campaign identity を正面から扱う。

## サマリ

4領域とも実装は堅牢で、致命的欠陥・承認ブロッカーは検出されなかった。resume/autoresume は3系統（ブリッジ auto-resume・LOCAL_LOOP controller 再起動・DB seq/fencing 整合）で状態整合と二重再開防止を明示設計。GUI visible submit は応答非依存の送信レシート要求で空送信・二重送信・偽失敗を封鎖。conversation lineage は最新セッション選択と誤提示対策で担保。fanout campaign identity は submission_id/jid 由来の冪等鍵で二重投入を防止。検出した独立論点は 0 件。観察を2件記録する。

## 確認済み（問題なしと判断した領域）

### R5-OK-1 resume/autoresume の3系統設計と二重再開防止
resume 系は3系統に分かれ、いずれも中断後の状態整合と二重再開防止を明示設計する。(1) ブリッジ起動時 auto-resume（bridge/copilot_bridge.py）は `should_autoresume(sess, fresh_flag)` を純粋関数で判定し、`--fresh` 最優先→前セッション無し→会話未添付でいずれも fresh 起動。crash 後（mode=="working"）は自動継続せず mode="interrupted" にし `GET /goal?resume=1` の明示操作を要求して無人での二重進行を防ぐ。(2) LOCAL_LOOP controller 再起動（relay/local_loop_controller.py）は job_lock→marker→snapshot→browser の固定順序で、ジョブロックはカーネルロックで排他（同一 job 二重起動不可）、marker は owner_pid ガード付き、正常終了/WAITING_RUNTIME/DONE では marker 除去、予期せぬ例外のみ marker 残置（真のクラッシュだけ自動再開）、runtime 再開は明示 `--resume-runtime` のみ。(3) DB レベルの seq/fencing 整合（relay/local_job_store.py）は claim_turn で `expected_seq != current_seq` を SEQ_MISMATCH 拒否し、`_validate_lease` が全 mutating 操作の入口で lease_id 一致・fencing 一致・未失効の3点を検証するため、再開後に古いプロセスが遅れて出した commit は FENCE_MISMATCH で確実に弾かれる。**根拠**: bridge/copilot_bridge.py（should_autoresume）, relay/local_loop_controller.py（起動順序・marker owner_pid ガード）, relay/local_job_store.py（claim_turn / _validate_lease）。1/8 の L-OK-1・L-OK-7、3/8 の S-OK-1〜S-OK-4 と整合。

### R5-OK-2 conversation lineage（最新セッション選択と誤提示対策）
ブリッジ auto-resume は意図的に「最新セッション（latest_session）」を渡す設計で、過去に latest_attached を使い2日前の会話を「再開」と誤提示した不具合の対策コメントが残る。会話の系譜（lineage）は最新の実会話に束縛され、古い添付を再開と取り違えない。**根拠**: bridge/copilot_bridge.py（latest_session 選択・誤提示対策コメント）。

### R5-OK-3 GUI visible submit（応答非依存レシートで空送信・二重送信・偽失敗を封鎖）
copilot_autopilot_relay.py の send 経路は、composer が空になっても新規会話では応答非依存レシートを要求する。fresh 会話では `/conversation/` URL または `_is_generating()` を SUBMIT_ACK_WAIT_S 内に確認できなければ `RuntimeError("composer cleared without ... acknowledgement")` を送出し、SPA の editor model 装着前に insertText が DOM に入り送信値が空のまま Send が arm する空送信を検知して失敗にする。Send が arm せず composer も空の場合は Enter を押さず次試行へ fall through（空送信の別経路封鎖）。JP long-goal は逐次 type() でなく1回の Input.insertText で IME 横取りを回避し、挿入後は最大8回 re-insert で着床検証。多重送信防止は固定800ms1回でなく最大12s ポーリングで空化を待ち、短窓による偽 failure+retry 二重送信を除去、新 answer block 出現を強い成功シグナルに採用。世代ゲート（前ターン生成中は GenerationInProgress で予算非消費リスケジュール）・tab 死（ConversationClosed）・offline（NetworkUnavailable）を区別し、3試行尽きたら RuntimeError。診断 snapshot は全て try/except で send() の制御フロー・retry・timing・例外を変えない。**根拠**: copilot_autopilot_relay.py（send / SUBMIT_ACK_WAIT_S / insertText / ポーリング）。判定: 欠陥なし。

### R5-OK-4 fanout campaign identity（submission_id/jid 由来の冪等鍵で二重投入防止）
fanout（複数 worker への一斉配信）の campaign identity は、goals_from_command の submission_id 由来 jid と `_append_goals_ledger` の resume-key 重複排除により冪等化される。同一コマンドのクラッシュ再生（同 jid）は no-op、意図的な同一テキスト別 jid は別タスクとして識別され、campaign の goal が二重投入されない。`return_new=True` で新規 admitted のみを memory(add_box) に投入する。**根拠**: relay/fleet_runner.py:1849-1888, 3863-3867, 3924-3928（4/8 の C-OK-6 と同一機構を fanout identity 観点で再確認）。

## 観察（実装欠陥ではない）

- R5-NOTE-1（優先度: 低）: GUI visible submit の空送信検知・多重送信防止は Playwright ベースの DOM 挙動に依存し、CI の hermetic suite では実ブラウザ経路の回帰が直接検証されない。実装は健全。
- R5-NOTE-2（優先度: 低）: conversation lineage の latest_session 選択の誤提示回帰に対する直接的な単体テストは確認範囲で明示できなかった。誤提示対策コメントは残るが回帰テスト追加を推奨。実装欠陥ではない。

## 監査で実施したこと（範囲 5/8 の取得件数）

- 読解した主要実装: bridge/copilot_bridge.py（should_autoresume / latest_session）、relay/local_loop_controller.py（起動順序・marker）、relay/local_job_store.py（claim_turn / _validate_lease）、copilot_autopilot_relay.py（send / 送信レシート）、relay/fleet_runner.py（submission_id/jid 冪等）。
- 確認済み論点（問題なし）: 4 件（R5-OK-1〜R5-OK-4）。
- 検出した独立論点: 0 件。観察: 2 件（R5-NOTE-1, R5-NOTE-2）。

担当範囲 5/8（resume/autoresume・GUI visible submit・conversation lineage・fanout campaign identity）の監査を完了。

---

# 担当範囲 6/8 — supervisor restart / port ownership の再取得経路（サブタスク4・6の未取得範囲を監査所見として補完）

- 監査日 (UTC): 2026-09-29 / 対象: feat/durable-companion-phase2-20260928 (PR #66)
- 実行環境: READ-ONLY 監査。既存ファイル無変更・git 操作なし。書込は本レポートのみ。
- 補足: 分割実行のサブタスク 4 ・ 6 は当初 REFUSED でレポートに所見が入らなかったため、本セクションで supervisor restart / port ownership の再取得経路を現行コード・テストから監査し、「問題の無い領域も確認済みとして明記」する。

## サマリ

実装は堅牢で、致命的欠陥・承認ブロッカーは検出されなかった。supervisor 再起動時の port 再取得は fail-closed verdict ・foreign holder 放置・2連続 idle 判定・兄弟 checkout 誤認防止で設計され、二重起動は Global mutex と per-job lock で抑止される。検出した独立論点は 0 件（3/8 の S-ISSUE-1（port ownership テストが Linux CI で skip）が本範囲にも適用されるため参照）。

## 確認済み（問題なしと判断した領域）

### R6-OK-1 port ownership verdict の fail-closed（再取得時に奪わない）
supervisor 再起動時の bridge port 所有判定（Get-BridgePortVerdict）は判定不能・想定外時に「所有していない/奪わない」側へ倒す。foreign holder（別プロセスが port を保持）は kill せず放置し、勝手な port 奪取をしない。3/8 の S-OK-6 と同一実装を restart 再取得観点で再確認。**根拠**: scripts/supervisor.ps1（Get-BridgePortVerdict）, scripts/test_supervisor_port_owner.py。

### R6-OK-2 server 生存判定の多要素検証と $null 耐性
Test-ServerUp 相当は TCP 接続可否だけでなく応答 body と pid を突き合わせて自分の server か foreign holder かを区別し、Get-ProcessVerdict は $null（プロセス消失）でも停止せず判定を継続する。restart 直後の瞬間的無応答で誤った port 奪取をしない。**根拠**: scripts/supervisor.ps1（Test-ServerUp / Get-ProcessVerdict）, scripts/test_supervisor_port_owner.py。

### R6-OK-3 stale server の 2 連続 idle 判定
Invoke-StaleServerCycle 相当は単発の idle 観測で stale と断ぜず 2 連続 idle を要求してから再取得サイクルへ進む。**根拠**: scripts/supervisor.ps1（Invoke-StaleServerCycle）。3/8 S-OK-8 と整合。

### R6-OK-4 restart 時の停止スコープ最小と「計画的終了」ラベル
Start-Server の kill 対象は (a) ours の port holder、(b) このチェックアウトの main.py 全プロセス、(c) supervisor が起動した prev proc のみ。$prevWasAlive を cleanup 前にスナップショットし、生存中を強制停止する場合のみ planned reason を付与する。**根拠**: scripts/supervisor.ps1（Start-Server 1076-1155）。3/8 S-OK-4・S-OK-5 と整合。

### R6-OK-5 兄弟 checkout 誤認防止と auto-resume の二重起動抑止
別 checkout（兄弟ディレクトリ）の同名プロセスを自分の server と誤認せず、Invoke-FleetAutoResume は Global mutex 相当で冪等化し supervisor 再起動時に fleet を二重起動しない。**根拠**: scripts/supervisor.ps1（兄弟 checkout 判別, Invoke-FleetAutoResume）。3/8 S-OK-9 と整合。

## 検出した論点

### R6-ISSUE-1（参照）port ownership テストが Linux CI で skip（優先度: 低）
3/8 の S-ISSUE-1 と同一。scripts/test_supervisor_port_owner.py は skipif(os.name != 'nt') で Linux CI では全 skip され、restart / port 再取得ロジックの回帰が CI 上で実行検証されない。実装健全、承認ブロッカーではない。推奨修正は 3/8 S-ISSUE-1 に記載済み。

## 監査で実施したこと（範囲 6/8 の取得件数）

- 読解した主要実装: scripts/supervisor.ps1（Get-BridgePortVerdict / Test-ServerUp / Get-ProcessVerdict / Invoke-StaleServerCycle / Start-Server / Invoke-FleetAutoResume / 兄弟 checkout 判別）。
- 読解した既存テスト: scripts/test_supervisor_port_owner.py。
- 確認済み論点（問題なし）: 5 件（R6-OK-1〜R6-OK-5）。
- 検出した独立論点: 0 件（R6-ISSUE-1 は 3/8 S-ISSUE-1 の参照）。

サブタスク 4 ・ 6 の未取得範囲を含む担当範囲 6/8 の監査を完了。

---

## 追補 (2026-09-29, 会話 2/2 = commands.d): 実行検証記録

担当範囲「commands.d の claim→apply→commit クラッシュ耐性・重複防止」の READ-ONLY 再監査。前記 4/8 セクションの静的所見を、当該会話で pytest を実行して裏付けた。既存ファイル・git 状態は無変更。

### V-1 実行したテストと結果 (unlock 済みセッション)
- `relay/test_command_claim_commit.py` … **26 passed** (8.61s, RC 0)。
- `relay/test_adopt_pending_command.py` + `relay/test_the_command_channel_obeys_only_what_it_should.py` + `relay/test_a_ledger_that_cannot_say_when_cannot_be_joined.py` … **116 passed** (13.89s, RC 0)。
- 合計 **142 passed / 0 failed**。警告は opentelemetry の DeprecationWarning と、ライブ supervisor が独立に書く `.fleet/tool_events.jsonl` 等の LIVE-STATE CANARY（非失敗、テスト起因ではない）のみ。

### V-2 再確認した設計上の不変条件 (file/line 根拠)
- claim→apply→commit を 1 件ずつ処理しキュー全体を先取りしない: `_drain_commands` は `claim_next_command` を用い、コミット成立まで次を claim しない。commit stall 時、後続は live pid が所有しない `.json` のまま残る。**根拠**: fleet_runner.py:3839-3868。
- commit point の非再実行性とレシート回復: `commit_command_claim` は rename (`.applied`/`.rejected`/`.read`) を非再実行コミット点とし、ack 書込失敗時は tombstone を残して `False`(=commit のみ再試行)を返す。既に committed 存在時は rename をスキップし tombstone から継続。**根拠**: fleet_runner.py:2936-2993。
- PID 再利用に対する所有権判定: `_claim_owner_is_live` は pid 生存かつ birth_token 一致時のみ所有権を認め、birth_token 不明時は他者作業を奪わないため保守的に True。**根拠**: fleet_runner.py:2734-2744。
- 二重適用防止(冪等性): crash-replay 時も jid は `sha1(submission_id + "\0" + idx)` から決定的に導出され同一。`_append_goals_ledger` は同一 jid の再投入を idempotent 化し、ライブ経路は新規に台帳へ入った goal だけを enqueue する(回復済みバッチは no-op)。legacy `commands.json` は固定名のため `_sid=""` として idempotency identity に用いない。**根拠**: fleet_runner.py:3104-3108, 1849-1888, 3090-3091。
- ledger 追記の耐久性: `_append_goals_ledger` は台帳が missing/corrupt の場合 raise し、ライブ apply は `raise_on_error=True` 経由で apply 失敗→restore に倒す(不明状態を上書きしない)。**根拠**: fleet_runner.py:1860-1888, 3877-3882。

### V-3 4/8 セクション記載の論点の扱い
- 4/8 の C-ISSUE(part-way 失敗時に `steer`/`reunlock` 副作用が再 apply で重複配信し得る、優先度 中〜低)は本実行検証でも変化なし。ledger goal は jid 冪等で保護されるが steer/reunlock は台帳外の副作用のため、既存テストは重複配信を捕捉しない。承認ブロッカーではないが回帰テスト追加を推奨(4/8 セクション記載どおり)。

### V-4 取得件数 (会話 2/2 = commands.d)
- 実行したテストファイル: 4 件 / 合計 142 passed・0 failed。
- 実行検証で裏付けた不変条件: 5 件(V-2)。
- 新規の独立ブロッカー論点: 0 件(既知の C-ISSUE 1 件は 4/8 セクションに既載)。

commands.d claim→apply→commit のクラッシュ耐性・重複防止レビューを完了。致命的欠陥・承認ブロッカーなし。


---

# 2026-09-29 follow-up resolution

PR #66 was merged to `main` as `b235e01`. The merge head `16c8c29` completed CI, CodeQL, Secret scan, Workflow lint, PowerShell lint, Windows build, and install-path checks successfully.

## C-ISSUE-1 — resolved

The audit correctly identified that `steer` / `reunlock` are message-bearing side effects and therefore are not safe to replay after a part-way command failure.

The command validator now treats each of those as its own retry unit:
- `steer` may be combined only with `ack`;
- `reunlock` may be combined only with `ack`;
- mixing either with close/settings/pause/stop/the other message-bearing control is rejected before any effect is applied.

This preserves the existing claim→apply→commit model while preventing a restored claim from sending the same user/unlock turn twice. The existing `add_goal` isolation rule remains unchanged.

Regression coverage was added in `relay/test_the_command_channel_obeys_only_what_it_should.py`, including acceptance of `ack` metadata and rejection of every mixed control form used by the audit scenario.

## L-ISSUE-1 — resolved by explicit coverage

No store implementation change was needed. `_validate_lease` already rejects a current `lease_id` paired with a stale `fencing_token` as `FENCE_MISMATCH`.

A parameterized regression now exercises that exact condition independently through:
- `heartbeat`
- `commit_turn`
- `abort_turn`
- `read_job_context`

Each case asserts `JobStoreError.code == "FENCE_MISMATCH"`, then verifies the same current lease remains usable with the correct fencing token.

## S-ISSUE-1 — already resolved on current main

The audit noted that `scripts/test_supervisor_port_owner.py` is skipped on Linux. Current `.github/workflows/ci.yml` already includes that file in the blocking `Windows-only tests` job, so the Windows/PowerShell port-ownership path is exercised by CI rather than being green-by-skip.

The PR #66 Windows CI containing that registration completed successfully.

## Validation

Focused follow-up suite:
- command-channel / claim-commit / steer delivery / local job store: **179 passed**
- command schema file alone: **104 passed**
- claim-commit + steer delivery: **50 passed**
- local job store: **25 passed**

The warnings observed were the repository's live-state canaries detecting writes from the concurrently running production supervisor/Fleet on this machine; they were explicitly non-failing and not attributed to the test code.


# 2026-09-29 follow-up resolution 2

## S-NOTE-1 — resolved by direct mutex coverage

The audit note was partly stale by the time of follow-up: `scripts/test_supervisor_port_owner.py` already exercised the parent-command-line ownership rule with a real venv launcher/base-interpreter pair and directly asserted that foreign holders are neither killed nor treated as ours.

The remaining gap was the supervisor Global Mutex `createdNew` loser branch. A Windows regression now extracts the production guard block itself, holds a unique named mutex, and verifies that a second acquisition takes the `createdNew=False` branch, writes the existing-supervisor log message, and returns before the sentinel. The same extracted guard is then run with a fresh name and must continue. This closes the only uncaptured subpart of S-NOTE-1 without changing supervisor runtime behavior.

## R5-NOTE-2 — resolved by explicit latest-session lineage coverage

Startup candidate selection is now factored through `startup_resume_candidate()`, which returns exactly `SessionStore.latest_session()`. A fake-store regression exposes a tempting `latest_attached()` method that raises if touched, proving startup does not search backward for a merely resumable older row. A second store-level regression persists an older attached session plus a newer unattached session and verifies that `latest_session()` returns the newer unattached row. A source contract pins the startup path to the tested helper. The authoritative behavior remains unchanged: `should_autoresume()` starts fresh when the newest row has no conversation instead of reopening an older one.

## CI-ISSUE-1 — resolved

The stale `.github/workflows/ci.yml` comment now says `nine files`, matching `scripts/check_ci_test_manifest.py` and the live manifest audit (`9 explicit exception(s)`). No CI execution logic changed.

## Remaining observations / accepted limits

- R5-NOTE-1 remains an environment-level observation: Playwright DOM submission behavior is not fully reproducible in the hermetic Linux suite. Existing logic is covered by pure/seam tests and live use, but this is not claimed as a browser-E2E proof.
- SEC-ISSUE-1 remains intentionally documented: XFF/IP identity is still caller-supplied in this deployment, while the default-on unlock token requirement prevents IP-only authorization in current operation. No claim is made that the IP oracle itself is fixed.
- S7-ISSUE-1 remains an architectural boundary: a same-user process with arbitrary code execution is outside what same-user file/tool gates can cryptographically exclude. The repository continues to describe this as narrowing/defence-in-depth, not closure.


# 2026-09-30 live supervisor follow-up

## Planned-restart transition path initialization -- resolved

A live residue (`.tmp.<supervisor_pid>` at repository root) exposed a runtime-only initialization-order bug that the earlier source-presence tests did not catch. `ServerTransitionPath` was computed from `$FleetDir` near the top of `scripts/supervisor.ps1`, but `$FleetDir` itself was not assigned until roughly 1,200 lines later. The long-running supervisor therefore attempted to publish a planned-restart marker through an empty path; `Write-ServerTransition` wrote a root `.tmp.<pid>` file and its fail-open catch intentionally hid the telemetry failure while allowing the stale-code restart itself to proceed.

Resolution: `ServerTransitionPath` is now derived directly from `$Root/.fleet`, where `$Root` is initialized before the transition helper is defined. A regression test asserts the initialization order and forbids the early path assignment from depending on `$FleetDir`. This preserves the intended rule that health telemetry can never block a restart while making the yellow planned-restart signal actually publishable in the live supervisor.
