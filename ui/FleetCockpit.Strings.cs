// FleetCockpit.Strings.cs -- the T()/L() translation tables and status labels.
// Part of `partial class CockpitWindow` (the other parts: ui/FleetCockpit.cs and ui/FleetCockpit.*.cs).
// Moved verbatim out of ui/FleetCockpit.cs: no code was changed. Every part is listed on the
// Build "FleetCockpit" line of ui/rebuild_ui.ps1, which is the one list of what is compiled.
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;
using System.Windows.Data;
using System.Globalization;
using System.Web.Script.Serialization;
using System.Collections.ObjectModel;
using System.Runtime.CompilerServices;

partial class CockpitWindow
{

    string T(string k)
    {
        bool ja = _lang == 0;
        if (k == "title") return ja ? "並列" : "Parallel";
        if (k == "done") return ja ? "完了" : "done";
        if (k == "running") return ja ? "実行中" : "Running";
        if (k == "elapsed") return ja ? "経過" : "elapsed";
        if (k == "goals") return ja ? "ゴール" : "goals";
        if (k == "concurrent") return ja ? "同時" : "concurrent";
        if (k == "freeram") return ja ? "空きRAM" : "free RAM";
        if (k == "freed") return ja ? "完了で即解放" : "freed on finish";
        if (k == "turn") return ja ? "ターン" : "turn";
        if (k == "release") return ja ? "解放" : "release";
        if (k == "released") return ja ? "解放済" : "released";
        if (k == "maxtabs") return ja ? "最大タブ" : "Max tabs";
        if (k == "autoscale") return ja ? "RAM自動調整" : "Auto (RAM)";
        if (k == "auto_on") return ja ? "ON" : "ON";
        if (k == "auto_off") return ja ? "OFF" : "OFF";
        if (k == "def_tabs") return ja ? "開始(デフォルト)" : "Start";
        if (k == "max_tabs2") return ja ? "上限" : "Max";
        if (k == "idle") return ja ? "未実行 — python -m relay.fleet_runner で並列実行を開始するとここに表示されます。"
                                   : "Not running — start with python -m relay.fleet_runner to see goals here.";
        if (k == "stale") return ja ? "更新が止まっています（フリート停止？）" : "no updates (fleet stopped?)";
        if (k == "stale_wait") return ja ? "更新待ち" : "Waiting for update";
        if (k == "applies_next") return ja ? "次回起動から適用" : "applies next run";
        if (k == "start") return ja ? "並列実行を開始" : "Start parallel run";
        if (k == "goalhint") return ja ? "1行に1ゴール（複数可）・Ctrl+Enter で開始" : "One goal per line · Ctrl+Enter to start";
        if (k == "folder") return ja ? "自律コーディング (フォルダ)" : "Autonomous coding (folder)";
        // Feature B: view-filter toolbar (4-tab spec)
        if (k == "flt_all") return ja ? "すべて" : "All";
        if (k == "flt_active") return ja ? "進行中" : "In progress";   // ready + running: NOT only "running"
        if (k == "flt_needs") return ja ? "承認待ち" : "Needs input";
        if (k == "flt_done") return ja ? "完了" : "Done";
        if (k == "flt_intr") return ja ? "中断" : "Interrupted";
        // legacy key kept for safety (no longer rendered)
        // Feature C: retry
        if (k == "retry") return ja ? "再試行" : "Retry";
        if (k == "retry_all") return ja ? "停止を一括再試行" : "Retry all stopped";
        if (k == "retry_note") return ja ? "停止中のため、このゴール用にフリートを再起動しました" : "No run live — relaunched a fleet for this goal";
        if (k == "retry_capped") return ja ? "再試行上限に達したゴールを除外" : "at retry cap, skipped";
        if (k == "autoretry") return ja ? "自動再試行" : "Auto-retry";
        if (k == "cap") return ja ? "上限" : "cap";
        if (k == "to_history") return ja ? "履歴へ" : "To history";
        if (k == "all_to_history") return ja ? "すべて履歴へ" : "All to history";
        if (k == "eta") return ja ? "完了予測" : "ETA";
        if (k == "eta_in") return ja ? "あと" : "in";
        if (k == "eta_calc") return ja ? "計測中…" : "estimating…";
        if (k == "rate") return ja ? "件/時" : "/h";
        // Effort selector + fleet-wide pause/stop (NEW)
        if (k == "effort") return ja ? "推論" : "Reasoning";
        if (k == "effort_policy") return ja ? "推論方針" : "Effort policy";
        if (k == "approval") return ja ? "承認" : "Approval";
        if (k == "run_mode") return ja ? "実行方式" : "Run mode";
        if (k == "pause") return ja ? "一時停止" : "Pause";
        if (k == "resume") return ja ? "再開" : "Resume";
        if (k == "stopall") return ja ? "全停止" : "Stop all";
        if (k == "steer_dead") return ja ? "走行が停止中のため割り込めません（再開後にどうぞ）" : "No run live — can't steer (resume the fleet first)";
        // Feature 1: collapsed-card steer affordance (placeholder watermark + send ack toast).
        if (k == "steer_collapsed_placeholder") return ja ? "割り込み指示…" : "Steer…";
        if (k == "steer_collapsed_ack") return ja ? "送信" : "Sent";
        // ── P0 Health strip (infra state) + Fix button + INFRA_STUCK / agent badge ──────
        if (k == "hs_server") return ja ? "サーバ" : "Server";
        if (k == "hs_tunnel") return ja ? "トンネル" : "Tunnel";
        if (k == "hs_edge") return ja ? "Edge" : "Edge";
        if (k == "hs_signin") return ja ? "サインイン" : "Sign-in";
        if (k == "hs_agent") return ja ? "エージェント" : "Agent";
        if (k == "hs_fix") return ja ? "直す" : "Fix";
        if (k == "hs_fixing_button") return ja ? "修復中…" : "Fixing…";
        if (k == "hs_fix_hint") return ja ? "検出された不具合を直す" : "Fix the detected problem";
        if (k == "hs_queue_stale") return ja ? "未着手の投入済みタスクが {0} 件、最長 {1} 続いています" : "{0} submitted task(s) still unstarted; oldest {1}";
        if (k == "hs_ok") return ja ? "正常" : "OK";
        if (k == "hs_down") return ja ? "応答なし" : "down";
        if (k == "hs_unknown") return ja ? "未設定/不明" : "unknown";
        if (k == "hs_checking") return ja ? "確認中…" : "checking…";
        if (k == "hs_lastcheck") return ja ? "最終確認: " : "last check: ";
        if (k == "hs_level_red") return ja ? "【赤：故障】" : "[RED: broken]";
        if (k == "hs_level_yellow") return ja ? "【黄：注意・未確認】" : "[YELLOW: caution / unverified]";
        if (k == "hs_never") return ja ? "未確認" : "not checked yet";
        if (k == "hs_srv_detail_ok") return ja ? "ローカルサーバは応答しています (127.0.0.1:8000)" : "Local server responding (127.0.0.1:8000)";
        if (k == "hs_srv_detail_bad") return ja ? "ローカルサーバが応答しません。start_all.bat を実行してください。" : "Local server not responding. Run start_all.bat.";
        // Amber, not red: the process IS up and answering. It is telling us its callers are
        // being rejected, which /health has always reported and this dot never read.
        if (k == "hs_srv_detail_auth") return ja ? "サーバは応答しているが、直近10分に認証失敗が出ています。MCP_API_KEY の不一致を疑ってください。件数:" : "Server is responding, but reporting auth failures in the last 10 minutes. Suspect an MCP_API_KEY mismatch. Count:";
        if (k == "hs_tun_detail_ok") return ja ? "トンネル経由でサーバに到達できます" : "Server reachable through the tunnel";
        if (k == "hs_tun_detail_bad") return ja ? "トンネルからサーバに到達できません" : "Server not reachable through the tunnel";
        if (k == "hs_tun_detail_notused") return ja ? "コネクタなし経路のためトンネルは使いません" : "Not used: the connector-less route needs no tunnel";
        if (k == "hs_tun_detail_none") return ja ? "MCP_TUNNEL_URL が .env に未設定です" : "MCP_TUNNEL_URL is not set in .env";
        if (k == "hs_fix_edge_navigate") return ja ? "エージェントのページを開いています..." : "opening the agent page...";
        if (k == "hs_fix_edge_still") return ja ? "ページを開いても準備完了になりません" : "navigated, but the page is still not ready";
        if (k == "hs_edge_detail_notabs") return ja ? "ブラウザは応答しています。この走行はソケット経路でタブを使わないため、タブが無いのは正常です。" : "The browser is responding. This run uses the socket route and drives no tabs, so having none is expected.";
        // A SEPARATE SENTENCE FOR A SEPARATE FACT. The message above asserts a socket-route
        // run, and it was shown on an idle machine where no run existed at all.
        if (k == "hs_edge_detail_norun") return ja ? "ブラウザは応答しています。走行が無いので、タブについては何も確認していません。" : "The browser is responding. No run is active, so nothing has been checked about tabs.";
        if (k == "hs_signin_old_ok") return ja ? "サインイン記録は有効期限内ですが、取得が古く現在の状態を保証しません。再取得までは未確認です。" : "The sign-in record is unexpired, but it is old and does not attest to the session right now. Unverified until the next capture.";
        if (k == "hs_agent_other_surface") return ja ? "エージェント紐付けの直接の証拠がありません。別サーフェスの直近取得にgpt_idがあるだけで、フリート自身の紐付けは未確認です。" : "No direct evidence that THIS surface is bound to an agent -- only a gpt_id from the last capture on ANOTHER surface. The fleet own binding is unverified.";
        if (k == "hs_srv_detail_stale") return ja ? "サーバは応答していますが、起動時のコードのままでチェックアウトが先に進んでいます。再起動しないと修正は反映されません。起動時HEAD:" : "The server is responding but is still running the code it started on; the checkout has moved past it. Fixes are not live until it restarts. Started at HEAD:";
        // THE SAME FACT, NOT PRESENTED AS SOMETHING TO DO. Shown on a GREEN dot while the
        // supervisor still has its chance to cycle the server; the line above is for when
        // it has had that chance and the server is still stale.
        if (k == "hs_srv_detail_stale_recent") return ja ? "サーバは応答しています。チェックアウトが先に進んでいますが、フリートがアイドルになれば自動で入れ替わります。人の操作は不要です。" : "The server is responding. The checkout has moved past it, and it will be cycled automatically once the fleet is idle. Nothing for you to do.";
        if (k == "hs_tun_detail_other") return ja ? "トンネルは応答していますが、このマシンで動いているサーバとは別のプロセスに繋がっています。pid:" : "The tunnel is responding, but it reaches a DIFFERENT process than the server running on this machine. pid:";
        if (k == "hs_tool_detail_fleet_down") return ja ? "ブリッジ側はツールを呼べていますが、フリート側のツール呼び出しが連続失敗しています。作業を実行する経路はこちらです。" : "The bridge can call tools, but the FLEET path is failing repeatedly. That is the path that does the work.";
        if (k == "hs_tool_detail_bridge_only_down") return ja ? "落ちているのはブリッジのチャット経路だけで、フリートのツール呼び出しは成功しています。ツール全体が不通ではありません。" : "Only the bridge chat path is down; fleet tool calls are succeeding. This is not a total tool outage.";
        if (k == "hs_tool_detail_never") return ja ? "自己診断がまだ一度も結果を書いていない（ブリッジ未起動か診断側の不具合）" : "the self-probe has never written a result (bridge not started, or the probe is broken)";
        if (k == "hs_signin_old") return ja ? "サインイン失敗の記録が古く、現在の状態を表していない可能性" : "the sign-in failure on record is old and may not describe the present";
        if (k == "hs_edge_detail_blank") return ja ? "ブラウザは動作中だがエージェントのページが開かれていない" : "browser up, but no agent page open";
        if (k == "hs_edge_detail_ok") return ja ? "コンパニオン Edge が稼働中 (:9222)" : "Companion Edge running (:9222)";
        if (k == "hs_edge_detail_bad") return ja ? "コンパニオン Edge に接続できません (:9222)" : "Companion Edge not reachable (:9222)";
        if (k == "hs_signin_ok") return ja ? "M365 にサインイン済み（ログイン画面なし）" : "Signed in to M365 (no login wall)";
        if (k == "hs_signin_bad") return ja ? "サインインが必要です（ログイン画面を検出）" : "Sign-in required (login wall detected)";
        if (k == "hs_agent_ok") return ja ? "専用エージェントに接続中" : "Bound to the configured agent";
        if (k == "hs_agent_warn") return ja ? "M365のタブはありますが、設定されたエージェントではありません。既定Copilotに落ちている可能性があります（コネクタ無し・テナント接地無しで、それらしく答えます）。" : "An M365 tab is open, but it is not the configured agent -- possibly the default Copilot, which answers fluently with no connectors and no tenant grounding.";
        // WAS TAB-ERA TEXT. This dot stopped reading tabs when the socket route landed, so
        // under that route there is never a chat tab and the one red message on the agent
        // dot sent the reader looking for something whose absence is normal. The branch
        // that still reaches this is: route open, no cached binding, no gpt_id anywhere.
        if (k == "hs_agent_bad") return ja ? "実行中ですが、このサーフェスがエージェントに紐付いている証拠がありません（テンプレート未取得または期限切れ）" : "Run is active, but there is no evidence this surface is bound to an agent (no cached template, or it has expired)";
        if (k == "hs_agent_unknown_live") return ja ? "実行中ですが取得記録がまだありません。未確認であって失敗ではありません。" : "Run is active but no capture has been recorded yet. Unverified, not failed.";
        if (k == "hs_agent_route_unknown") return ja ? "ソケット経路の記録が読めず、経路が使えるか判定できません。故障ではなく未確認です。" : "The socket route record could not be read, so whether the route is usable is unknown. Unverified, not failed.";
        // hs_agent_gray no longer means "Edge is down": under the socket route the agent dot
        // is grey when no run is in flight, which is the ordinary idle state.
        if (k == "hs_agent_gray") return ja ? "走行なし" : "No run in flight";
        if (k == "hs_agent_canned") return ja ? "（直近の返答が定型の無回答）" : " (the last reply was the canned non-answer)";
        // No tab state exists (Copilot never runs in a tab). A shut route HOLDS goals.
        if (k == "hs_agent_route_closed_held") return ja ? "ソケット経路が使えず、{n}件のゴールが待機中（修復を再試行中・タブは開きません）: {why}" : "Socket route unavailable: {n} goal(s) held while repair retries (no tab is opened): {why}";
        if (k == "hs_agent_route_closed_idle") return ja ? "ソケット経路は閉鎖中（修復を再試行中）。待機中のゴールはなく、実行中のワーカーは送信を続けています。" : "Socket route is shut (repair retrying). No goal is held; running workers keep sending.";
        if (k == "hs_agent_token_wait") return ja ? "トークン待ち — ゴールを保留中（{s}秒、タブは開かず取得を再試行中）" : "Waiting for token: goals held ({s}s); capture retrying, no tab is opened";
        if (k == "hs_signin_gray") return ja ? "捕捉の記録なし（走行なし）" : "No capture on record (no run)";
        // TWO OF THE THREE BRANCHES THAT USED hs_signin_gray DO have a record; it is
        // merely old, or the token has run out. Saying "no capture on record" there is a
        // statement a reader can check and find false, which costs more than saying less.
        if (k == "hs_signin_gray_old") return ja ? "取得記録はありますが古く、走行も無いため現在の状態は未確認です。" : "There is a capture on record, but it is old and no run is active, so the present state is unverified.";
        if (k == "hs_signin_gray_expired") return ja ? "記録上のトークンは期限切れですが、走行が無いため必要とされていません。" : "The token on record has expired, but no run is asking for one.";
        if (k == "hs_signin_unknown_live") return ja ? "走行中だが捕捉の記録がない" : "A run is live and no capture is on record";
        if (k == "hs_signin_failed_other") return ja ? "直近の捕捉が失敗（サインイン以外の理由）" : "The last capture failed for a reason other than sign-in";
        if (k == "hs_signin_stale") return ja ? "トークンが期限切れ（走行中）" : "The token has expired while a run is live";
        if (k == "hs_fixing") return ja ? "修復中… " : "Fixing… ";
        if (k == "hs_fix_signin") return ja ? "サインイン用に Edge を開いています…" : "Opening Edge for sign-in…";
        if (k == "hs_fix_signin_toast") return ja ? "開いたEdgeでサインインしてください。完了すると自動で緑になります。" : "Sign in on the Edge that opened; it turns green automatically when done.";
        if (k == "hs_fix_edge") return ja ? "Edge を再起動しています…" : "Relaunching Edge…";
        if (k == "hs_fix_agent") return ja ? "コネクタを再接続しています…" : "Reconnecting the connector…";
        if (k == "hs_fix_agent_ok") return ja ? "再接続に成功しました" : "Reconnect succeeded";
        if (k == "hs_fix_agent_fail") return ja ? "再接続に失敗（手動確認が必要）" : "Reconnect failed (needs manual check)";
        if (k == "hs_fix_server") return ja ? "start_all.bat を実行してください" : "Please run start_all.bat";
        if (k == "hs_fix_stack") return ja ? "サーバ/トンネルを起動しています(最大2分)" : "Starting server/tunnel (up to ~2 min)";
        if (k == "hs_fix_done") return ja ? "完了" : "done";
        if (k == "hs_fix_err") return ja ? "修復でエラー" : "fix error";
        if (k == "hs_fix_manual_needed") return ja ? "手動での対応が必要です" : "Manual step needed";
        // 2026-09-24 startup-loop fix: see StartupGate / AutoFixBudget in
        // ui/SelfImproveDashboard.cs for the mechanism these three strings surface.
        if (k == "autofix_start_all_running") return ja ? "起動処理が進行中です" : "Startup is already in progress";
        if (k == "autofix_exhausted") return ja
            ? "自動修復を繰り返しましたが直りませんでした。しばらく待つか、「直す」ボタンで再試行してください。"
            : "Automatic repair kept failing and has stopped retrying for now. Wait a bit, or use the Fix button to try again.";
        if (k == "hs_fix_repair_unreadable") return ja
            ? "自動修復の結果を読み取れませんでした。doctor.bat を実行して原因を確認してください。"
            : "Could not read the automatic repair result. Run doctor.bat to see what is wrong.";
        if (k == "infra_wait") return ja ? "インフラ待ち" : "Infra wait";
        if (k == "infra_retry") return ja ? "再投入" : "Re-queue";
        if (k == "badge_default_copilot") return ja ? "既定Copilot" : "default Copilot";
        // ── P1/P2 UX: artifacts, history groups/search/auto-archive, resume ──────────
        if (k == "artifacts") return ja ? "成果物" : "Artifacts";
        if (k == "copy_result") return ja ? "結果をコピー" : "Copy result";
        if (k == "copy_result_done") return ja ? "結果をコピーしました" : "Result copied";
        if (k == "copy_result_fail") return ja ? "結果をコピーできませんでした" : "Could not copy result";
        if (k == "reveal_artifacts") return ja ? "成果物を表示" : "Show artifacts";
        if (k == "reveal_artifacts_done") return ja ? "成果物をExplorerで表示しました" : "Opened artifacts in Explorer";
        if (k == "reveal_artifacts_fail") return ja ? "成果物を表示できませんでした" : "Could not open artifacts";
        if (k == "rerun_same") return ja ? "同じ条件でもう一度実行" : "Run again with the same goal";
        if (k == "rerun_started") return ja ? "同じ条件で再実行を開始しました" : "Started the same goal again";
        // ── running/waiting/stopped card menu (same kebab as completed cards) ──────────
        if (k == "stop_task") return ja ? "止める" : "Stop";
        if (k == "why_waiting") return ja ? "進まない理由を表示" : "Why it's waiting";
        if (k == "why_waiting_title") return ja ? "進まない理由" : "Why it's waiting";
        if (k == "why_waiting_none") return ja ? "待ちの理由は報告されていません。" : "No waiting reason was reported.";
        if (k == "make_priority") return ja ? "最優先にする" : "Make top priority";
        if (k == "clear_priority") return ja ? "優先を解除" : "Clear priority";
        if (k == "priority_pending") return ja ? "優先の仕組みは準備中です（priority_boost）" : "Priority boosting is not wired up yet (priority_boost)";
        if (k == "path_missing") return ja ? "見つかりません" : "Not found";
        if (k == "hist_today") return ja ? "今日" : "Today";
        if (k == "hist_yesterday") return ja ? "昨日" : "Yesterday";
        if (k == "hist_earlier") return ja ? "その他" : "Earlier";
        if (k == "hist_search") return ja ? "履歴を検索…" : "Search history…";
        if (k == "autoarchive") return ja ? "完了を自動で履歴へ" : "Auto-archive on finish";
        // Priority boost ("make this top priority") feature strings.
        if (k == "boost_setting") return ja ? "最優先の操作を表示" : "Show top-priority control";
        if (k == "boost_set") return ja ? "最優先にする" : "Make top priority";
        if (k == "boost_clear") return ja ? "最優先を解除" : "Clear top priority";
        if (k == "boost_set_done") return ja ? "最優先に指定しました" : "Marked top priority";
        if (k == "boost_clear_done") return ja ? "最優先を解除しました" : "Top priority cleared";
        if (k == "boost_mark") return ja ? "最優先" : "Top priority";
        if (k == "boost_count") return ja ? "優先中" : "Boosted";
        if (k == "boost_too_many") return ja ? "同時実行枠を超えています（これ以上は優先が効きません）" : "exceeds concurrent slots (further boosts have no effect)";
        if (k == "boost_disabled") return ja ? "最優先機能は設定でオフです" : "Priority boost is turned off in settings";
        if (k == "set_archive_section") return ja ? "自動アーカイブ" : "Auto-archive";
        // Capacity-wait banner (admission gate) + force-start
        // Settings panel (gear popup) -- consolidates the scattered toolbar controls
        if (k == "settings") return ja ? "設定" : "Settings";
        if (k == "set_tabs_section") return ja ? "並列タブ" : "Parallel tabs";
        // Header chip "タブ X/Y". Y is the LIVE admission ceiling (shrinks/grows with free RAM);
        // X is real browser tabs open right now. A shrink is soft -- it stops new tabs from
        // opening, it does not close ones already running -- so X can sit above Y for a while as
        // the fleet drains down to the new ceiling, and one worker mid fan-out (research +
        // refuter side-pages) alone can be up to 3 of X. Neither is a bug; the tooltip says so
        // instead of leaving "3/1" looking like a broken cap.
        if (k == "tabs_chip_hint") return ja
            ? "今開いているタブ数 / 上限（空きRAMで自動増減）。上限を下げても今動いている分は閉じません。次に空くまで新規だけ止めます。1ワーカーが調査・反論用の子タブを開くと、それだけで数字が上限を超えて見えることがあります。"
            : "Open tabs now / the live ceiling (auto-adjusts with free RAM). Lowering the ceiling doesn't close tabs already running -- it only stops new ones until one frees up. One worker fanning out to research/refuter side-tabs can push the count above the ceiling by itself.";
        if (k == "set_retry_section") return ja ? "自動再試行" : "Auto-retry";
        if (k == "set_retention_section") return ja ? "会話の保持" : "Conversation retention";
        if (k == "set_rate_section") return ja ? "レート上限" : "Rate ceiling";
        if (k == "rate_rpm") return ja ? "毎分の上限" : "Per minute";
        // ONE LINE, BECAUSE THE PANEL IS A LIST OF CONTROLS AND NOT A MANUAL. This was five
        // sentences -- what the ceiling does, what the bar is drawn against, where the default
        // comes from, when to lower it, what 0 means. Together with the retention note below it
        // made the panel taller than the display, and the section at the bottom (詳細設定) was
        // pushed off the screen entirely. The operator reported that section as missing.
        // What survives is what changes a decision: the number's meaning and how to turn it off.
        if (k == "rate_note") return ja
            ? "直近1分がこの本数に達したら投入を止め、下がったら再開。0で無効。"
            : "Admission pauses while the last minute is at this number, and resumes when it drops. 0 disables it.";
        if (k == "set_fleetret_section") return ja ? "作業ディレクトリの保持" : "Working directory retention";
        if (k == "fleet_log_days") return ja ? "ログ(日)" : "Logs (days)";
        if (k == "fleet_store_days") return ja ? "実行記録(日)" : "Run records (days)";
        if (k == "set_sidebar_section") return ja ? "チャットのサイドバー" : "Chat sidebar";
        if (k == "sidebar_section_cap") return ja ? "各節の表示件数(0=すべて)" : "Rows per section (0 = all)";
        if (k == "sidebar_cap_all") return ja ? "すべて" : "All";
        if (k == "fleet_scratch_days") return ja ? "作業ファイル(日)" : "Scratch files (days)";
        if (k == "fleet_compress_hours") return ja ? "圧縮まで(時間)" : "Compress after (h)";
        if (k == "fleet_ret_note") return ja
            ? "終わった走行のログのみ。会話は消えません。新しい分は圧縮して残します。"
            : "Finished runs' logs only -- conversations are not touched. Newer ones are compressed, not deleted.";
        if (k == "ret_days") return ja ? "保持日数" : "Keep for (days)";
        if (k == "ret_keep") return ja ? "消さない" : "keep all";
        if (k == "ret_mb") return ja ? "上限サイズ (MB)" : "Size cap (MB)";
        if (k == "set_capacity_section") return ja ? "容量ガード" : "Capacity guard";
        if (k == "disk_floor") return ja ? "実行下限ディスク (GB)" : "Disk floor (GB)";
        if (k == "disk_floor_hint") return ja ? "空きディスクがこの値を下回るとタブ開放を待機します。" : "Pauses opening tabs when free disk drops below this.";
        if (k == "ram_floor") return ja ? "確保する空きRAM (MB)" : "RAM floor (MB)";
        // FALLBACK: on-demand re-unlock. Automatic recovery already exists (relay_fleet
        // injects the unlock password into a worker's first turn, and a heuristic retries
        // it when a reply looks like a lock refusal) but both can miss -- see
        // relay/fleet_runner.py's apply_reunlock docstring for the incident this answers:
        // twice in one day a worker was refused for lock, nothing recovered it, and the
        // run carried on regardless. The route is visible here; the password never is --
        // it is read on the coordinator's machine from its own .env and never appears in
        // the command file this button writes.
        if (k == "set_approval_section") return ja ? "操作承認" : "Approval policy";
        if (k == "set_approval_hint") return ja
            ? "自動=禁止パターンは即拒否・それ以外は通す（推奨）／バイパス=ゲートを掛けない／毎回確認=初見の種類ごとに人に訊く"
            : "Auto = refuse prohibited patterns outright, pass the rest (recommended) / Bypass = no gate / Confirm = ask a person about every first-seen class";
        if (k == "set_approval_note") return ja
            ? "承認ダイアログと同じ設定です。ダイアログは承認待ちのときしか開かないので、落ち着いて見直せる場所にも置いてあります。次の判定から効きます。"
            : "The same setting the approval dialog carries. That dialog only opens while an approval is being demanded, so the choice lives here too. Takes effect from the next decision.";
        // The re-unlock control's four strings were removed with it. They described a text box
        // for typing which worker to rescue -- a question the records can answer and a person
        // cannot. relay/fleet_runner.py sweep_unclaimed_refusals does the rescuing now.
        if (k == "ui_scale_section") return ja ? "表示サイズ" : "UI scale";
        if (k == "ui_scale") return ja ? "表示サイズ" : "UI scale";
        if (k == "ui_scale_hint") return ja ? "Ctrl+ホイールや Ctrl +/− でも変更できます（Ctrl+0 で自動）。" : "Also change with Ctrl+wheel or Ctrl +/− (Ctrl+0 = auto).";
        if (k == "ui_auto") return ja ? "自動" : "Auto";
        if (k == "ui_auto_on") return ja ? "自動: オン" : "Auto: on";
        if (k == "ui_auto_off") return ja ? "自動: オフ" : "Auto: off";
        if (k == "force_start") return ja ? "今すぐ開始" : "Start now";
        if (k == "floor_restore") return ja ? "容量制限を戻す" : "Restore limit";
        if (k == "floor_off") return ja ? "容量制限を一時解除しています" : "Capacity limit paused";
        // FIX B: Stop button's optimistic "stopping" state (distinct from the steady-state tooltip).
        if (k == "stop_stopping") return ja ? "停止中…" : "Stopping…";
        // FIX C: explicit verb -- distinct from the archive ("履歴へ"/"to_history") and per-card
        // release ("解放"/"release") dismiss verbs, so the three no longer read as ambiguous synonyms.
        if (k == "clear_history") return ja ? "履歴を空にする" : "Clear history";
        // FIX D: same lightweight toast wording for both the autoscale-ON and autoscale-OFF apply paths.
        if (k == "maxtabs_toast_prefix") return ja ? "最大タブ →" : "Max tabs ->";
        // ── 6th health dot "Tool": bridge self-probe (.fleet/tool_probe.json), see the class-field
        // comment near HEALTH_DOT_COUNT for what this axis covers and why it's separate from Agent.
        if (k == "hs_tool") return ja ? "ツール" : "Tool";
        if (k == "hs_tool_detail_ok") return ja ? "ツール呼び出し確認 OK" : "tool call confirmed OK";
        if (k == "hs_tool_detail_consent") return ja ? "consent待ち（再接続で解消可）" : "consent pending (reconnect can clear this)";
        if (k == "hs_tool_detail_down") return ja ? "応答なし(再接続が必要)" : "no response (reconnect needed)";
        if (k == "hs_tool_detail_no_tool") return ja ? "チャットは応答あり／ツール未接続の可能性" : "chat is answering; tool may not be attached";
        if (k == "hs_tool_detail_stale") return ja ? "自己診断が実行されていません" : "self-probe hasn't run recently";
        if (k == "hs_tool_detail_none") return ja ? "自己診断は未設定(機能無効)" : "self-probe not configured (feature off)";
        if (k == "hs_tool_detail_checking") return ja ? "ツール接続を確認中…" : "checking tool connection…";
        // Bridge-targeted Fix tier (RunFix priority 5) + the always-available manual button.
        if (k == "hs_fix_bridge") return ja ? "チャットのブリッジを再接続しています…" : "Reconnecting the chat bridge…";
        if (k == "hs_fix_bridge_ok") return ja ? "ブリッジの再接続に成功しました" : "Bridge reconnect succeeded";
        if (k == "hs_fix_bridge_fail") return ja ? "ブリッジの再接続に失敗（手動確認が必要）" : "Bridge reconnect failed (needs manual check)";
        if (k == "set_chat_section") return ja ? "チャット" : "Chat";
        if (k == "reconnect_chat") return ja ? "チャット再接続" : "Reconnect chat";
        if (k == "reconnect_chat_hint") return ja ? "対話チャット(ブリッジ :9223)のMCP接続を再接続します" : "Reconnect the interactive chat bridge's MCP connection (:9223)";
        if (k == "reconnect_chat_toast_start") return ja ? "チャット再接続中…" : "Reconnecting chat…";
        if (k == "reconnect_chat_toast_ok") return ja ? "チャット再接続に成功しました" : "Chat reconnect succeeded";
        if (k == "reconnect_chat_toast_fail") return ja ? "チャット再接続に失敗しました（手動確認が必要）" : "Chat reconnect failed (needs manual check)";
        if (k == "reconnect_chat_toast_dead") return ja
            ? "エージェントが読み込まれませんでした。次を実行してください: powershell -File scripts\\start_bridge.ps1 -HardReset -Keepalive"
            : "Agent did not load. Run: powershell -File scripts\\start_bridge.ps1 -HardReset -Keepalive";
        return k;
    }
    // Inline bilingual helper for one-off strings that don't warrant a T() dictionary entry
    // (e.g. the 詳細設定/Advanced panel). Same signature/behaviour as ApprovalPromptWindow's L()
    // elsewhere in this file -- kept as a small per-class duplicate rather than a shared static
    // because both classes' _lang fields are independent instance state.
    string L(string ja, string en) { return _lang == 0 ? ja : en; }
    string StatusLabel(string s)
    {
        bool ja = _lang == 0;
        if (s == "waiting") return ja ? "実行中" : "Running";
        if (s == "done") return ja ? "完了" : "Done";
        if (s == "stuck") return ja ? "停滞" : "Stuck";
        if (s == "maxturns") return ja ? "上限" : "Max turns";
        if (s == "error") return ja ? "エラー" : "Error";
        if (s == "cancelled") return ja ? "停止" : "Stopped";
        if (s == "interrupted") return ja ? "中断" : "Interrupted";
        if (s == "pending") return ja ? "待機列" : "Queued";
        if (s == "ready") return ja ? "準備" : "Ready";
        return s;
    }
}
