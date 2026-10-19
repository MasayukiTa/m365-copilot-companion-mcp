// CopilotChat.Settings.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Text;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Documents;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using System.Web.Script.Serialization;

partial class ChatWindow
{
    //: WHERE THE SETTINGS LIVE, in both places, new first.
    //:
    //: %APPDATA% is redirected for a process running inside an MSIX package, so the same
    //: absolute path resolved to the operator's file from one context and to a private copy
    //: from another. On 2026-09-16 the panel showed 1 GB while every fleet coordinator
    //: reserved 4 GB, for a month, and neither side could see the other's file. The
    //: repository is the one directory every context agrees about.
    //:
    //: READS take the new location when it exists and the old one otherwise, so a machine
    //: mid-migration keeps working and one that never migrates behaves exactly as before.
    //: WRITES always go to the new location. Kept in step with tools/settings_path.py --
    //: test_the_settings_path_is_the_same_in_every_language pins the two together.
    static string SettingsFileNew
    {
        get { return Path.Combine(RepoRootForSettings(), ".config", "settings.txt"); }
    }

    static string SettingsFileOld
    {
        get
        {
            string app = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            if (string.IsNullOrEmpty(app))
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    ".copilot-bridge", "settings.txt");
            return Path.Combine(app, "copilot-bridge", "settings.txt");
        }
    }

    static string SettingsFile
    {
        get
        {
            try { if (File.Exists(SettingsFileNew)) return SettingsFileNew; } catch (Exception) { }
            try { if (File.Exists(SettingsFileOld)) return SettingsFileOld; } catch (Exception) { }
            return SettingsFileNew;
        }
    }

    static string SettingsFileForWrite
    {
        get
        {
            try { Directory.CreateDirectory(Path.GetDirectoryName(SettingsFileNew)); }
            catch (Exception) { }
            return SettingsFileNew;
        }
    }

    //: The repository root as seen from the running executable: ui\ sits directly under it.
    static string RepoRootForSettings()
    {
        try
        {
            string exe = System.Reflection.Assembly.GetExecutingAssembly().Location;
            string dir = Path.GetDirectoryName(exe);
            DirectoryInfo d = new DirectoryInfo(dir);
            while (d != null && !Directory.Exists(Path.Combine(d.FullName, ".fleet")))
                d = d.Parent;
            if (d != null) return d.FullName;
        }
        catch (Exception) { }
        return Directory.GetCurrentDirectory();
    }


    string T(string k)
    {
        bool ja = _lang == 0;
        if (k == "newchat") return ja ? "新しいチャット" : "New chat";
        if (k == "newchat_btn") return ja ? "＋   新しいチャット" : "＋   New chat";
        if (k == "send") return ja ? "送信" : "Send";
        // (theme/lang: iconized in Wave 2 — kept without emoji as tooltips live under tip_theme/tip_lang)
        if (k == "theme") return ja ? "テーマ (ダーク/ライト)" : "Theme (dark/light)";
        if (k == "lang") return ja ? "English へ" : "日本語へ";
        if (k == "rename") return ja ? "名前を変更" : "Rename";
        if (k == "delete") return ja ? "削除" : "Delete";
        if (k == "generating") return ja ? "生成中" : "Generating";
        if (k == "cancel") return ja ? "キャンセル" : "Cancel";
        if (k == "copy") return ja ? "コピー" : "Copy";
        if (k == "stop") return ja ? "停止" : "Stop";
        if (k == "ui_auto") return ja ? "自動" : "Auto";
        if (k == "fleet_queued") return ja ? "並列実行が満杯のため待機列に追加しました（空き枠で実行）。先頭に ! を付けると強制優先。" : "Fleet is full — queued (runs when a slot frees). Prefix ! to force priority.";
        if (k == "fleet_forced") return ja ? "強制優先で待機列の先頭に追加しました。" : "Forced to the front of the queue.";
        if (k == "router_q") return ja ? "これは調査向きの依頼です。researcher で深掘りしますか？" : "This looks like research. Run it on the researcher?";
        if (k == "router_research") return ja ? "researcher で深掘り" : "Deep research";
        if (k == "router_normal") return ja ? "そのまま送信" : "Send as-is";
        if (k == "attach") return ja ? "ファイル/画像を添付（画像は Ctrl+V で貼り付けも可）" : "Attach a file/image (or paste an image with Ctrl+V)";
        if (k == "attach_btn") return ja ? "＋ 添付" : "+ Attach";
        if (k == "attach_fail") return ja ? "添付に失敗:" : "Attach failed:";
        if (k == "open_cockpit") return ja ? "並列実行を開く" : "Open parallel execution";
        if (k == "loadingconv") return ja ? "会話を読み込み中…" : "Loading conversation…";
        if (k == "fleetview") return ja ? "並列タスクの会話" : "Parallel task";
        if (k == "fleetview_note") return ja ? "並列タスクの会話を表示中。ここに入力すると、この会話に割り込めます。" : "Viewing a parallel-task conversation. Type here to steer it.";
        if (k == "del_head") return ja ? "を削除 — 方法を選んでください" : " — choose how to delete";
        if (k == "m1t") return ja ? "このアプリからのみ削除" : "Delete from this app only";
        if (k == "m1s") return ja ? "Copilot 側の会話は残す（最も安全）" : "Keeps the Copilot conversation (safest)";
        if (k == "m2t") return ja ? "Copilot で開いて手動削除" : "Open in Copilot to delete manually";
        if (k == "m2s") return ja ? "その会話を Copilot で開く。1クリックで削除" : "Opens it in Copilot; delete it there in one click";
        if (k == "m3t") return ja ? "Copilot 会話も自動削除" : "Also auto-delete the Copilot conversation";
        if (k == "m3s") return ja ? "Copilot 側の会話も実際に消えます（失敗時は開いて手動）" : "Actually removes it on the Copilot side too (opens it for manual delete if that fails)";
        if (k == "del_note") return (ja ? "選んだ方法が次回の既定になります（現在: モード " : "Your choice becomes the default (current: mode ") + _deleteMode + "）";
        if (k == "t_local") return ja ? "ローカルから削除しました（Copilot 側は残しています）。" : "Deleted locally (kept on the Copilot side).";
        if (k == "t_open") return ja ? "Copilot で開きました。Copilot 上で会話を削除してください。" : "Opened in Copilot. Delete the conversation there.";
        if (k == "t_nourl") return ja ? "Copilot 会話 URL 不明のため、ローカルのみ削除しました。" : "No Copilot URL; deleted locally only.";
        if (k == "t_auto_ok") return ja ? "Copilot 会話も自動削除しました。" : "Auto-deleted the Copilot conversation too.";
        if (k == "t_auto_fail") return ja ? "自動削除はできませんでした。Copilot で開きます（手動で削除してください）。" : "Auto-delete failed. Opening in Copilot (delete it manually).";
        if (k == "manage_btn") return ja ? "会話を整理" : "Manage";
        if (k == "manage_title") return ja ? "会話の一括削除" : "Bulk delete conversations";
        if (k == "show_all") return ja ? "すべて表示（自分の会話も含む）" : "Show all (incl. your own)";
        if (k == "period") return ja ? "期間" : "Period";
        if (k == "period_all") return ja ? "全期間" : "All";
        if (k == "period_24h") return ja ? "過去24時間" : "Last 24h";
        if (k == "period_7d") return ja ? "過去7日" : "Last 7d";
        if (k == "period_30d") return ja ? "過去30日" : "Last 30d";
        if (k == "running_note") return ja ? "走行中：Copilot側の削除は停止します（ローカル一覧からのみ削除）。" : "A run is live: Copilot-side delete is disabled (local list only).";
        if (k == "select_all") return ja ? "全選択" : "Select all";
        if (k == "clear_all") return ja ? "全解除" : "Clear";
        if (k == "fetch_copilot") return ja ? "Copilot側から一覧取得" : "Fetch from Copilot";
        if (k == "del_selected") return ja ? "選択した会話を削除" : "Delete selected";
        if (k == "close") return ja ? "閉じる" : "Close";
        if (k == "untitled") return ja ? "(無題)" : "(untitled)";
        // ── empty state (fresh window / after New chat) ──────────────────────────
        if (k == "empty_title") return ja ? "何でも頼んでください — ローカルPCも操作できます" : "Ask me anything — I can operate this PC too";
        if (k == "empty_slash") return ja ? "「/」でコマンド一覧" : "Type \"/\" for the command list";
        if (k == "empty_s1") return ja ? "デスクトップのファイルを整理する計画を立てて" : "Plan how to organize the files on my Desktop";
        if (k == "empty_s2") return ja ? "このPCの空き容量を調べて大きいフォルダを一覧して" : "Check this PC's free space and list the largest folders";
        if (k == "empty_s3") return ja ? "Excelファイルを読んで要約して" : "Read an Excel file and summarize it";
        // ── status dot state labels (tooltip) ────────────────────────────────────
        if (k == "dot_idle")     return ja ? "接続済み・待機中" : "Connected · idle";
        if (k == "dot_busy")     return ja ? "生成中"           : "Generating";
        if (k == "dot_offline")  return ja ? "ブリッジ未接続"   : "Bridge unreachable";
        if (k == "dot_signin")   return ja ? "サインイン切れの可能性" : "Possible sign-in / refusal";
        if (k == "signin_banner") return ja ? "Copilotが応答を拒否しています。サインイン切れ/接続切れの可能性 — Fleet Cockpitの健康表示を確認してください" : "Copilot is refusing to respond. Sign-in or connection may have expired — check the health view in Fleet Cockpit.";
        if (k == "signin_open")   return ja ? "Fleet Cockpit を開く" : "Open Fleet Cockpit";
        // ── send-target pinning / reachability fallback errors (nothing was sent) ────
        if (k == "send_wrong_page")  return ja ? "送信先の会話に接続できませんでした — 送信は行われていません" : "Could not connect to the target conversation — nothing was sent.";
        if (k == "send_unknown_conv") return ja ? "この会話の送信先を特定できません。会話を開き直してください。" : "Can't identify where to send this — please reopen the conversation.";
        // ── fleet conversations: the message goes to the fleet, not to the bridge page ──
        if (k == "fleet_steer_sent")  return ja ? "実行中のワーカーに追加指示を渡しました。次のターンから反映されます。" : "Handed to the running worker -- it takes effect on its next turn.";
        if (k == "fleet_goal_empty") return ja ? "/goal のあとに内容がありません。新しいゴールの本文を書いてください。" : "/goal was given with no text. Write the new goal after it.";
        if (k == "fleet_follow_sent") return ja ? "この会話の続きとして、新しいワーカーに引き継ぎました（同じ会話を継続します）。" : "Queued as a follow-up: a new worker will continue this same conversation.";
        if (k == "fleet_follow_idle") return ja ? "フリートが起動していないため、次の走行で拾われます（同じ会話の続きとして投入済み）。" : "No fleet is running, so this waits for the next one -- queued as a continuation of this conversation.";
        if (k == "fleet_no_goal")     return ja ? "この会話を識別するゴール本文が記録されていないため、続きを投入できません。" : "This conversation has no recorded goal text to identify it, so it can't be continued.";
        if (k == "fleet_send_failed") return ja ? "フリートへの受け渡しに失敗しました。送信は行われていません。" : "Could not hand this to the fleet -- nothing was sent.";
        if (k == "not_saved") return ja ? "この会話は保存されていません: " : "This conversation was not saved: ";
        if (k == "send_offline") return ja ? "ブリッジに接続できません。送信していません。" : "Can't reach the bridge. Nothing was sent.";
        if (k == "retry_start_stack") return ja ? "スタックを起動して再試行" : "Start the stack and retry";
        if (k == "bridge_auth_problem") return ja ? "ブリッジが要求を受け付けませんでした（認証またはバージョンの不一致）。" : "The bridge refused the request (authentication or version mismatch).";
        if (k == "reload_transcript") return ja ? "再読み込み" : "Reload";
        // ── sidebar section / action labels ──────────────────────────────────────
        if (k == "sec_pinned")   return ja ? "ピン留め"   : "Pinned";
        if (k == "sec_today")    return ja ? "最近"        : "Recent";
        if (k == "sec_fleet")    return ja ? "フリート"    : "Fleet runs";
        if (k == "sec_archived") return ja ? "アーカイブ"  : "Archived";
        if (k == "pin")          return ja ? "ピン留め"    : "Pin";
        if (k == "unpin")        return ja ? "ピン解除"    : "Unpin";
        if (k == "archive")      return ja ? "アーカイブ"  : "Archive";
        if (k == "unarchive")    return ja ? "アーカイブ解除" : "Unarchive";
        // ── Wave 2: header dot tooltip, iconized footer tooltips, list scannability ──
        if (k == "dot_click_tip") return ja ? "状態の詳細は Fleet Cockpit へ" : "Open Fleet Cockpit for connection details";
        if (k == "tip_cockpit")   return ja ? "並列実行を開く" : "Open parallel execution";
        if (k == "tip_lang")      return ja ? "English へ切り替え" : "Switch to Japanese";
        if (k == "tip_theme")     return _dark ? (ja ? "ライトテーマへ" : "Switch to light theme") : (ja ? "ダークテーマへ" : "Switch to dark theme");
        if (k == "rename_link")   return ja ? "名前変更" : "Rename";
        if (k == "show_more")     return ja ? ("+" + "{0}" + " 件を表示") : ("+{0} more");
        if (k == "older_more")    return ja ? "さらに古い会話を読み込む（残り {0} 件）…" : "Load older conversations ({0} left)…";
        return k;
    }

    // Keep the chat in language/theme sync with the cockpit: both share settings.txt, so when the
    // other window toggles, re-read and re-apply here too (lang -> re-label all chrome + conv list).
    void CheckSettings()
    {
        try
        {
            if (!File.Exists(SettingsFile)) return;
            long m = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
            if (m == _settingsMtime) return;
            _settingsMtime = m;
            int l0 = _lang;
            int cap0 = _sectionCap;
            double s0 = _uiScale;
            bool a0 = _uiAuto; double t0 = _scaleTarget;
            LoadSettings();                              // re-reads lang/dark/ui_scale/target (+ApplyTheme)
            if (_lang != l0) { UpdateChrome(); RefreshConvList(); RerenderActiveConversation(); }
            else if (_sectionCap != cap0) RefreshConvList();   // the cockpit changed the sidebar cap
            // Cockpit changed the shared zoom -> mirror it silently. In AUTO recompute for THIS monitor
            // (the per-monitor effective scale, not the other window's); in MANUAL push the shared number.
            if (_uiAuto)
            {
                if (!a0 || System.Math.Abs(t0 - _scaleTarget) > 0.001
                        || System.Math.Abs(s0 - EffectiveAutoScale(CurrentMonitorScale())) > 0.001)
                    ApplyAutoScale(false);
            }
            else if (a0 || _uiScale != s0) ApplyScale(false);
        }
        catch { }
    }

    // ── delete-mode banner (3 choices, like Claude Code's modes) ────────────────
    void LoadSettings()
    {
        try
        {
            if (!File.Exists(SettingsFile)) return;
            foreach (var ln in File.ReadAllLines(SettingsFile))
            {
                int v;
                if (ln.StartsWith("deletemode=") && int.TryParse(ln.Substring(11).Trim(), out v)) _deleteMode = v;
                else if (ln.StartsWith("lang=") && int.TryParse(ln.Substring(5).Trim(), out v)) _lang = v;
                else if (ln.StartsWith("dark=")) _dark = ln.Substring(5).Trim() != "0";
                else if (ln.StartsWith("sidebar_collapsed=")) _sidebarCollapsed = ln.Substring(18).Trim() == "1";
                else if (ln.StartsWith("sidebar_section_cap=") && int.TryParse(ln.Substring(20).Trim(), out v))
                    _sectionCap = System.Math.Max(0, System.Math.Min(500, v));
                else if (ln.StartsWith("last_open_conv=")) _lastOpenId = ln.Substring(15).Trim();
                else if (ln.StartsWith("ui_scale="))
                {
                    string sv = ln.Substring(9).Trim();
                    if (sv.Equals("auto", StringComparison.OrdinalIgnoreCase))
                    { _uiAuto = true; _uiScaleLoaded = true; }
                    else
                    {
                        double d;
                        if (double.TryParse(sv, System.Globalization.NumberStyles.Float,
                                            System.Globalization.CultureInfo.InvariantCulture, out d))
                        { _uiAuto = false; _uiScale = ClampScale(d); _uiScaleLoaded = true; }
                    }
                }
                else if (ln.StartsWith("ui_scale_target="))
                {
                    double ut;
                    if (double.TryParse(ln.Substring(16).Trim(), System.Globalization.NumberStyles.Float,
                                        System.Globalization.CultureInfo.InvariantCulture, out ut))
                    { _scaleTarget = System.Math.Max(0.8, System.Math.Min(3.0, ut)); _scaleTargetLoaded = true; }
                }
            }
            ApplyTheme();     // _dark may have changed -> re-apply (shared with the cockpit)
        }
        catch { }
    }
    // Shared with the cockpit -> preserve the 'dark' key and write all three.
    void SaveSettings()
    {
        // settings.txt is SHARED with the cockpit, which stores many more keys (autoscale, maxtabs,
        // disk/ram floor, effort, approval, autoscale_per_tab_mb, ...). A full overwrite here would
        // wipe all of them -> every fleet setting resets on the next launch. So preserve every other
        // line and update only our three. No BOM (a BOM breaks the python-side reader, cf. .env rule).
        try
        {
            var want = new Dictionary<string, string> {
                { "deletemode", _deleteMode.ToString() },
                { "lang", _lang.ToString() },
                { "dark", _dark ? "1" : "0" },
                { "sidebar_collapsed", _sidebarCollapsed ? "1" : "0" },
                { "last_open_conv", _conv != null ? (_conv.Id ?? "") : "" },
                // ui_scale holds the literal "auto" in auto mode, else the fixed number (manual).
                { "ui_scale", _uiAuto ? "auto" : _uiScale.ToString("0.00", System.Globalization.CultureInfo.InvariantCulture) },
                { "ui_scale_target", _scaleTarget.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture) },
            };
            var lines = new List<string>();
            var seen = new HashSet<string>();
            if (File.Exists(SettingsFile))
                foreach (string ln in File.ReadAllLines(SettingsFile))
                {
                    int eq = ln.IndexOf('=');
                    string k = eq > 0 ? ln.Substring(0, eq) : null;
                    if (k != null && want.ContainsKey(k)) { lines.Add(k + "=" + want[k]); seen.Add(k); }
                    else lines.Add(ln);
                }
            foreach (var kv in want) if (!seen.Contains(kv.Key)) lines.Add(kv.Key + "=" + kv.Value);
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsFileForWrite));
            File.WriteAllText(SettingsFileForWrite, string.Join("\n", lines.ToArray()) + "\n", new UTF8Encoding(false));
        }
        catch { }
    }
}
