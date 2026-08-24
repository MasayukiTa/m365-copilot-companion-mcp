// SelfImproveDashboard.cs -- native Windows (WPF) read-only view for the SELF-IMPROVEMENT
// controller (the "surpass" feature, M365_HARDENING_AND_UX Tier 2).
//
// relay/selfimprove/dashboard.py folds the self-improvement ledgers (genome Archive, burned
// registry, SWE grade stream, per-A/B selfimprove_report_*.json) into ONE dashboard_state dict
// and, via write_json(), drops it as .fleet/selfimprove_dashboard.json -- the self-improvement
// analogue of .fleet/status.json. THIS window tails that JSON (~1s) and renders the scorecard,
// the A/B history, the burned ledger, the pass@1 trend, and the archive summary. It is strictly
// READ-ONLY: there is no control here that writes any file (unlike FleetCockpit's release/steer).
//
//   [ python -m relay.selfimprove.dashboard (write_json) ] --(selfimprove_dashboard.json)--> [ this ]
//
// Theme/cards/glyphs match FleetCockpit.cs exactly: the sibling app slate palette, single orange
// accent #ea580c, Material Symbols rendered as vector geometry from ui/assets/material_glyphs.json
// (NO emoji), soft-band card borders + faint tint (never a harsh fill), pill badges. It follows
// the same shared settings.txt theme/lang so toggling the chat retints this too.
//
// Build: this file is compiled INTO FleetCockpit.exe alongside FleetCockpit.cs (same csc.exe,
// legacy .NET Framework 4.x / C# 5). Add it to the file list in ui\build_cockpit.bat, e.g.:
//     ... "%~dp0FleetCockpit.cs" "%~dp0SelfImproveDashboard.cs"
// It declares NO entry point (no Main) and lives in the global namespace, exactly like
// CockpitWindow, so it drops straight into the existing single-exe build.
//
// ============================================================================================
// INTEGRATION HOOK (the ONE line a maintainer adds to FleetCockpit.cs -- do NOT edit it here):
//
//   In CockpitWindow.BuildChrome(), in the `ctrls` header cluster (next to where _mainBtn is
//   created and added around line 411-414), insert:
//
//       var _siBtn = IconButton("account_tree", 18); _siBtn.ToolTip = _lang == 0 ? "自己改善ダッシュボード" : "Self-improvement dashboard"; _siBtn.Click += delegate { new SelfImproveDashboardWindow().Show(); }; ctrls.Children.Add(_siBtn);
//
//   That single statement opens this window. SelfImproveDashboardWindow is in the global
//   namespace (same as CockpitWindow), so no using/namespace change is needed. It resolves the
//   JSON path itself (../.fleet/selfimprove_dashboard.json relative to the exe), so no argument
//   is required.
// ============================================================================================
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using System.Web.Script.Serialization;

class SelfImproveDashboardWindow : Window
{
    public const string WindowTitle = "Self-Improvement";

    static Color C(string hex) { return (Color)ColorConverter.ConvertFromString(hex); }

    // theme-dependent brushes (Theme.cs is the single source of truth)
    Brush Bg, CardBg, Border, Fg, Muted, QuoteBg, BtnBg;
    Brush Accent;
    static readonly Brush White = new SolidColorBrush(C("#ffffff"));

    bool _dark = true;
    int _lang = 0;             // 0 = Japanese, 1 = English
    long _settingsMtime = 0;

    // FIX 1: regeneration throttle
    DateTime _lastRegen = DateTime.MinValue;

    static readonly string SettingsFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "copilot-bridge", "settings.txt");

    readonly string _jsonPath;
    DispatcherTimer _timer;
    string _lastSig = "";
    JavaScriptSerializer _js = new JavaScriptSerializer();
    double _upm = 960;
    Dictionary<string, string> _glyphs = new Dictionary<string, string>();

    ContentControl _iconHost;
    TextBlock _header, _sub, _freshnessLine;
    Button _themeBtn, _langBtn, _refreshBtn;
    Border _divider1, _divider2;   // vertical dividers between icon buttons
    StackPanel _body;
    ScrollViewer _sv;
    Border _headBar;

    public SelfImproveDashboardWindow() : this(null) { }

    public SelfImproveDashboardWindow(string path)
    {
        _jsonPath = ResolvePath(path);
        LoadGlyphs();
        LoadSettings();
        ApplyThemeBrushes();
        // Named once and matched by name: FleetCockpit's --authority guard raises THIS window
        // rather than opening a second one, and it identifies it by this exact title.
        Title = WindowTitle;
        Width = 920; Height = 740;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        BuildChrome();
        _timer = new DispatcherTimer();
        _timer.Interval = TimeSpan.FromMilliseconds(1000);
        _timer.Tick += new EventHandler(OnTick);
        _timer.Start();
        // FIX 1a: regenerate the feed on window open so data is never stale
        RegenerateFeed();
        OnTick(null, null);
    }

    static string ResolvePath(string path)
    {
        if (!string.IsNullOrEmpty(path)) return path;
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;
        return Path.GetFullPath(Path.Combine(exeDir, "..", ".fleet", "selfimprove_dashboard.json"));
    }

    // FIX 1a: fire-and-forget feed regeneration via `python -m relay.selfimprove.dashboard --write`
    // Resolves venv python from the repo root (exeDir is <repo>/ui/).
    // Guard: ignores the call if a regen was started within the last 1500ms.
    void RegenerateFeed()
    {
        try
        {
            if ((DateTime.Now - _lastRegen).TotalMilliseconds < 1500) return;
            _lastRegen = DateTime.Now;

            string exeDir   = AppDomain.CurrentDomain.BaseDirectory;
            string repoRoot = Path.GetFullPath(Path.Combine(exeDir, ".."));
            string venvPy   = Path.Combine(repoRoot, ".venv", "Scripts", "python.exe");
            string pyExe    = File.Exists(venvPy) ? venvPy : "python";

            var psi = new ProcessStartInfo();
            psi.FileName               = pyExe;
            psi.Arguments              = "-m relay.selfimprove.dashboard --write";
            psi.WorkingDirectory       = repoRoot;
            psi.UseShellExecute        = false;
            psi.CreateNoWindow         = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError  = true;

            var proc = new Process();
            proc.StartInfo = psi;
            proc.Start();
            // drain output async so the process doesn't block on a full buffer
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            // do NOT WaitForExit -- fire-and-forget; the 1s tail timer picks up the new file
        }
        catch (Exception) { }
    }

    // ── i18n ────────────────────────────────────────────────────────────────────
    string T(string k)
    {
        bool ja = _lang == 0;
        // window header
        if (k == "win_title")  return ja ? "自己改善 / Self-Improvement" : "Self-Improvement / 自己改善";
        if (k == "win_sub")    return ja
            ? "エージェントが自分の解決スキャフォルドをどう改善しているか（実タスクの完了率・A/Bテスト・採用履歴）"
            : "How the agent is improving its own solving scaffold — real-task completion, A/B tests, and what it kept.";

        // authority section -- what changed what the system may become
        if (k == "auth_sec") return ja ? "権限の履歴" : "Authority";
        if (k == "auth_exp") return ja
            ? "この系が「何になれるか」を変えた行為の記録。台帳は追記専用でハッシュ連鎖しているが、"
              + "何も許可しないし、止めもしない。actor は自己申告で検証されていない。"
            : "Acts that changed what this system may become. The ledger is append-only and "
              + "chained; it authorises nothing and prevents nothing, and actor is self-reported.";
        if (k == "auth_intact")   return ja ? "凍結セット照合" : "Frozen set";
        if (k == "auth_ok")       return ja ? "一致" : "matches";
        if (k == "auth_broken")   return ja ? "不一致" : "differs";
        if (k == "auth_anchor")   return ja ? "アンカー" : "Anchor";
        if (k == "auth_chain")    return ja ? "台帳の連結" : "Ledger links";
        if (k == "auth_chain_ok") return ja ? "連続" : "contiguous";
        if (k == "auth_verified_here") return ja
            ? "この2つはこの画面が自分で計算している（python の自己申告ではない）。"
              + "ただし各レコードの内容ハッシュまでは再計算していない — 連結と通し番号のみ。"
            : "Both computed by this window, not reported by python. Record CONTENTS are not "
              + "re-hashed here -- only the links and the sequence.";
        if (k == "auth_rate")     return ja ? "直近7日の再署名" : "Re-signings, last 7d";
        if (k == "auth_rate_hi")  return ja ? "平常より多い" : "above the usual";
        if (k == "auth_none")     return ja ? "記録なし" : "no records";
        if (k == "auth_revoke")   return ja ? "直前の再署名を取り消す" : "Revoke the last re-signing";
        if (k == "auth_revoke_q") return ja
            ? "直前の再署名を取り消します。\n\n戻すのは【承認】であって【コード】ではありません。"
              + "ファイルは変わったままなので、直後に凍結セットは不一致になり、走行は止まります。"
              + "それが狙いです。\n\n戻り先: "
            : "Withdraw the last re-signing.\n\nThis undoes the APPROVAL, not the code. The files "
              + "stay as they are, so the frozen set will differ immediately afterwards and runs "
              + "will stop. That is the intent.\n\nRestoring: ";
        if (k == "auth_revoke_t") return ja ? "最終確認" : "Final confirmation";
        if (k == "auth_revoke_ok") return ja
            ? "取り消しました。凍結セットが不一致になっているのは正常です。"
            : "Revoked. The frozen set differing now is the expected state.";
        if (k == "auth_revoke_no") return ja ? "取り消せませんでした" : "Could not revoke";
        if (k == "auth_nothing")  return ja ? "取り消せる再署名がありません" : "No re-signing to withdraw";

        // no-data friendly message
        if (k == "nodata_title") return ja ? "まだデータがありません" : "No data yet";
        if (k == "nodata_body")  return ja
            ? "自己改善ループが走ると、ここに指標が表示されます。\n\npython -m relay.selfimprove.dashboard を実行するとデータが生成されます。"
            : "Metrics will appear here once the self-improvement loop runs.\n\nRun: python -m relay.selfimprove.dashboard";

        // section labels (shown in caps)
        if (k == "usage_sec")     return ja ? "実利用" : "Live usage";
        if (k == "scorecard_sec") return ja ? "スコアカード" : "Scorecard";
        if (k == "ab_sec")        return ja ? "A/B 履歴" : "A/B history";
        if (k == "burned_sec")    return ja ? "Burned 台帳" : "Burned ledger";
        if (k == "trend_sec")     return ja ? "Pass@1 推移" : "Pass@1 trend";
        if (k == "archive_sec")   return ja ? "ゲノム" : "Genomes";
        if (k == "pending_sec")   return ja ? "判断待ち" : "Awaiting a decision";
        if (k == "ev_rebless")    return ja ? "再署名" : "Re-signed";
        if (k == "ev_mismatch")   return ja ? "無承認の変更を検知" : "Unapproved change detected";
        if (k == "ev_revoke")     return ja ? "承認を取り消し" : "Approval withdrawn";
        if (k == "ev_apply")      return ja ? "ゲノム適用" : "Genome applied";
        if (k == "ev_revert")     return ja ? "ゲノム撤回" : "Genome reverted";
        if (k == "ev_branch_new") return ja ? "枝を命名" : "Branch named";
        if (k == "ev_branch_del") return ja ? "枝を削除" : "Branch deleted";
        if (k == "ev_unresolved") return ja ? "未解決" : "unresolved";
        if (k == "ev_closed_by")  return ja ? "検知 {0} → {1}後に再署名" : "detected {0}, re-signed {1} later";
        if (k == "ev_testrecords") return ja
            ? "テスト実行が本番台帳に書いたレコード {0} 件（一時ディレクトリのみを触るもの）"
            : "{0} records written into the live ledger by test runs (they touch only temp dirs)";
        if (k == "ev_by")         return ja ? "実行" : "by";
        if (k == "ev_summary")    return ja ? "要約" : "summary";
        if (k == "ev_churn")      return ja ? "再署名が集中しているファイル" : "Files re-signed most";
        if (k == "ev_summary_tip") return ja
            ? "モデルによる要約。台帳の記録ではない — 記録された原文はクリックで表示。"
            : "A model's summary, not the ledger's record -- click to show the recorded text.";
        if (k == "pending_exp")   return ja
            ? "恒久委任の外にある変更の提案。委任は「何を進化させてよいか」を定義するファイル自体には及ばないので、ここに溜まる。実行するには、あなたの指示をそのまま authorization に入れる。"
            : "Proposed changes outside the standing delegation. It does not extend to the files that define what may be evolved, so those queue here; running one takes your own words as its authorization.";
        if (k == "pending_copy")  return ja ? "コマンドをコピー" : "Copy command";
        if (k == "pd_approve")    return ja ? "承認する" : "Approve";
        if (k == "pd_a1")         return ja
            ? "承認する。この提案のまま実施してよい。"
            : "Approved. Carry it out as proposed.";
        if (k == "pd_a2")         return ja
            ? "承認する。実施したら結果を報告すること。"
            : "Approved. Report back once it is done.";
        if (k == "pd_r1")         return ja
            ? "却下する。この変更は不要。"
            : "Rejected. This change is not wanted.";
        if (k == "pd_r2")         return ja
            ? "却下する。今はやらない（後で見直す）。"
            : "Rejected for now; revisit later.";
        if (k == "pd_own")        return ja ? "自分で書く" : "Write my own";
        if (k == "pd_recorded")   return ja
            ? "選んだ文がそのまま台帳に記録されます。"
            : "The phrase you pick is what goes into the ledger, word for word.";
        if (k == "pd_reject")     return ja ? "却下する" : "Reject";
        if (k == "pd_approved")   return ja ? "承認済み" : "approved";
        if (k == "pd_waiting")    return ja ? "エージェントの実行待ち" : "waiting on the agent";
        if (k == "pd_ask_t")      return ja ? "この提案を承認する" : "Approve this proposal";
        if (k == "pd_ask")        return ja
            ? "承認の理由を、あなたの言葉で書いてください。ここに書いた文はそのまま台帳に記録され、"
              + "この変更の根拠になります。要約も言い換えもされません。"
            : "Say why, in your own words. What you write is recorded verbatim in the ledger as "
              + "the authorisation for this change -- not summarised, not paraphrased.";
        if (k == "pd_ask_reject") return ja
            ? "却下の理由（任意）。書いておくと、同じ提案が次に出たときに読めます。"
            : "Why not (optional). Writing it means the next time this comes up, it can be read.";
        if (k == "pd_reject_t")   return ja ? "この提案を却下する" : "Reject this proposal";
        if (k == "pd_need_words") return ja
            ? "空のままでは承認できません。あとから誰も引用できない承認は、承認ではありません。"
            : "An empty authorisation is refused: an approval nobody can quote afterwards is not one.";
        if (k == "pd_failed")     return ja ? "記録できませんでした" : "Could not record it";
        if (k == "pd_ok")         return ja ? "OK" : "OK";
        if (k == "pd_cancel")     return ja ? "やめる" : "Cancel";
        if (k == "pending_copied") return ja ? "コピーしました" : "Copied";
        if (k == "auth_detail")   return ja ? "記録と取り消し" : "Records and undo";

        // section explanations (one-liner below the label)
        if (k == "usage_exp")  return ja
            ? "あなたの実際のタスクから算出。ベンチマーク不要 — 普段の完了率と傾向を示します。"
            : "From your real runs, no benchmark — shows day-to-day completion rate and trend.";
        if (k == "scorecard_exp") return ja
            ? "最新世代の解決性能のスナップショット。A/B テストの結果と採用されたスキャフォルドの数。"
            : "Snapshot of the latest generation's solving performance, the latest A/B result, and how many scaffolds have been adopted.";
        if (k == "ab_exp")     return ja
            ? "新しいスキャフォルド案を旧版と比較した結果（新しい順）。採用=緑、示唆的=黄、却下=赤。"
            : "Results of testing a new scaffold idea against the current one, newest first. Keep=green, suggestive=yellow, rejected=red.";
        if (k == "burned_exp") return ja
            ? "評価に使われたため再利用できない問題の台帳。理由別の内訳。"
            : "Problems that can no longer be reused for evaluation because they were consumed during testing — broken down by reason.";
        if (k == "trend_exp")  return ja
            ? "各改善サイクル後の pass@1 の推移（新しい順・最大24件）。 薄い行は再測定で置き換えられた測定。"
            : "Pass@1 after each improvement cycle, newest first — up to 24 points shown. Dimmed rows were replaced by a re-measurement.";
        if (k == "archive_exp") return ja
            ? "採用されたゲノム（解決スクリプトの変種）とその測定値。QDセルは問題タイプごとのスロット数。"
            : "Adopted genomes (scaffold variants) and what each measured. QD cells = slots by problem type.";

        // metric labels
        if (k == "u_completion")  return ja ? "完了率" : "Completion";
        if (k == "u_recent")      return ja ? "直近完了率" : "Recent";
        if (k == "u_turns")       return ja ? "中央ターン数" : "Median turns";
        if (k == "u_tasks")       return ja ? "タスク数" : "Tasks";
        if (k == "u_trend")       return ja ? "完了率の推移（古い→新しい）" : "Completion trend (old → new)";
        if (k == "u_persona")     return ja ? "自我/助言の混入率（一般利用）" : "Persona/advice leak rate (live usage)";
        if (k == "u_persona_exp") return ja
            ? "ベンチ不要・実際の利用から自動計測。出力に上から目線の助言/講釈/自我が混じった割合（低いほど良い）。"
            : "No benchmark — measured automatically from real usage. Share of outputs leaking coaching/lecturing/ego (lower is better).";
        if (k == "u_persona_pre") return ja ? "計測前" : "Not yet measured";
        if (k == "u_persona_n")   return ja ? "件採点" : "scored";
        if (k == "u_persona_eg")  return ja ? "検出例" : "Flagged examples";
        if (k == "latest_pass")   return ja ? "最新 pass@1" : "Latest pass@1";
        if (k == "latest_ab")     return ja ? "最新 A/B" : "Latest A/B";
        if (k == "burned_total")  return ja ? "Burned 合計" : "Burned total";
        if (k == "archive_count") return ja ? "採用ゲノム数" : "Archive count";
        if (k == "qd_cells")      return ja ? "QD セル" : "QD cells";
        if (k == "genomes")       return ja ? "ゲノム" : "Genomes";
        if (k == "g_id")          return ja ? "ゲノム" : "Genome";
        if (k == "g_pass")        return ja ? "pass@1" : "pass@1";
        if (k == "g_gate")        return ja ? "ゲート判定" : "Gate";
        if (k == "g_desc")        return ja ? "特性" : "Descriptors";
        if (k == "g_parent")      return ja ? "親" : "Parent";
        if (k == "g_best")        return ja ? "最良 pass@1" : "Best pass@1";
        if (k == "superseded")    return ja ? "(再測定で置換)" : "(replaced by a re-measurement)";
        if (k == "measured_n")    return ja ? "測定回数" : "Measurements";
        if (k == "g_root")        return ja ? "\u2014 (起点)" : "\u2014 (root)";
        if (k == "keep")          return ja ? "採用" : "Keep";
        if (k == "none")          return ja ? "なし" : "none";
        if (k == "total")         return ja ? "合計" : "Total";
        if (k == "by_reason")     return ja ? "理由別内訳" : "By reason";
        return k;
    }

    // ── Material Symbols glyphs (vector, no emoji) ───────────────────────────────
    void LoadGlyphs()
    {
        try
        {
            string p = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "assets", "material_glyphs.json");
            if (!File.Exists(p)) return;
            var o = (Dictionary<string, object>)_js.DeserializeObject(File.ReadAllText(p, Encoding.UTF8));
            if (o.ContainsKey("unitsPerEm")) _upm = Convert.ToDouble(o["unitsPerEm"]);
            var g = (Dictionary<string, object>)o["glyphs"];
            foreach (KeyValuePair<string, object> kv in g) _glyphs[kv.Key] = kv.Value.ToString();
        }
        catch (Exception) { }
    }
    UIElement MakeIcon(string name, double size, Brush fill)
    {
        if (!_glyphs.ContainsKey(name))
        {
            var ph = new Border(); ph.Width = size; ph.Height = size; return ph;
        }
        var path = new System.Windows.Shapes.Path();
        Geometry geo = Geometry.Parse(_glyphs[name]).Clone();
        double s = size / _upm;
        geo.Transform = new MatrixTransform(s, 0, 0, -s, 0, s * _upm);
        path.Data = geo; path.Fill = fill; path.Stretch = Stretch.None;
        path.Width = size; path.Height = size;
        path.HorizontalAlignment = HorizontalAlignment.Center;
        path.VerticalAlignment = VerticalAlignment.Center;
        return path;
    }

    // ── settings (shared with the chat / cockpit) ─────────────────────────────────
    void LoadSettings()
    {
        try
        {
            if (!File.Exists(SettingsFile)) return;
            foreach (string ln in File.ReadAllLines(SettingsFile))
            {
                int v;
                if (ln.StartsWith("dark=")) _dark = ln.Substring(5).Trim() != "0";
                else if (ln.StartsWith("lang=") && int.TryParse(ln.Substring(5).Trim(), out v)) _lang = v;
            }
            _settingsMtime = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
        }
        catch (Exception) { }
    }
    void SaveKey(string key, string val)
    {
        try
        {
            var lines = new List<string>(); bool found = false;
            if (File.Exists(SettingsFile))
                foreach (string ln in File.ReadAllLines(SettingsFile))
                {
                    if (ln.StartsWith(key + "=")) { lines.Add(key + "=" + val); found = true; }
                    else lines.Add(ln);
                }
            if (!found) lines.Add(key + "=" + val);
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsFile));
            File.WriteAllText(SettingsFile, string.Join("\n", lines.ToArray()) + "\n", new UTF8Encoding(false));
            _settingsMtime = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
        }
        catch (Exception) { }
    }

    // ── theme ────────────────────────────────────────────────────────────────────
    void ApplyThemeBrushes()
    {
        Bg      = Theme.Br(Theme.Bg(_dark));
        CardBg  = Theme.Br(Theme.Surface(_dark));
        Border  = Theme.Br(Theme.Border(_dark));
        Fg      = Theme.Br(Theme.Text(_dark));
        Muted   = Theme.Br(Theme.Muted(_dark));
        QuoteBg = Theme.Br(Theme.SurfaceSubtle(_dark));
        BtnBg   = Theme.Br(Theme.SurfaceSubtle(_dark));
        Accent  = Theme.Br(Theme.Accent(_dark));
    }
    Color BgColor()   { return Theme.Col(Theme.Bg(_dark)); }
    Color CardColor() { return Theme.Col(Theme.Surface(_dark)); }
    static Color Mix(Color a, Color b, double t)
    {
        return Color.FromRgb((byte)(a.R * t + b.R * (1 - t)),
                             (byte)(a.G * t + b.G * (1 - t)),
                             (byte)(a.B * t + b.B * (1 - t)));
    }

    static Color StatusColorFor(string ck, bool dark)
    {
        if (ck == "good") return Theme.Col(Theme.Success(dark));
        if (ck == "warn") return Theme.Col(Theme.Warning(dark));
        if (ck == "bad")  return Theme.Col(Theme.Danger(dark));
        return Theme.Col(Theme.Muted(dark));
    }
    string VerdictKey(object keep, string verdict)
    {
        if (AsBool(keep)) return "good";
        if (!string.IsNullOrEmpty(verdict) && verdict.ToLower() == "suggestive") return "warn";
        return "bad";
    }

    // ── chrome ───────────────────────────────────────────────────────────────────
    void BuildChrome()
    {
        var root = new DockPanel();

        // ---- header bar ----
        _headBar = new Border();
        _headBar.Padding = new Thickness(26, 18, 18, 14);
        DockPanel.SetDock(_headBar, Dock.Top);

        var headGrid = new Grid();
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        headGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });  // title row
        headGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });  // subtitle row
        headGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });  // separator

        // controls (top-right): refresh | lang | theme
        var ctrls = new StackPanel();
        ctrls.Orientation = Orientation.Horizontal;
        ctrls.VerticalAlignment = VerticalAlignment.Top;

        // Refresh button (no matching glyph in 8-glyph set; use Unicode ⟳ TextBlock)
        _refreshBtn = RefreshButton();
        _refreshBtn.ToolTip = _lang == 0 ? "更新" : "Refresh";
        _refreshBtn.Click += delegate { RegenerateFeed(); };
        ctrls.Children.Add(_refreshBtn);

        _langBtn = IconButton("translate", 18);
        _langBtn.ToolTip = "日本語 / English";
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveKey("lang", _lang.ToString()); PaintChrome(); ForceRender(); };
        ctrls.Children.Add(_langBtn);

        _themeBtn = IconButton(_dark ? "light_mode" : "dark_mode", 18);
        _themeBtn.ToolTip = "テーマ (ダーク/ライト)";
        _themeBtn.Click += delegate { _dark = !_dark; SaveKey("dark", _dark ? "1" : "0"); ApplyThemeBrushes(); PaintChrome(); ForceRender(); };
        ctrls.Children.Add(_themeBtn);

        Grid.SetColumn(ctrls, 1); Grid.SetRow(ctrls, 0);
        headGrid.Children.Add(ctrls);

        // title row: icon + text
        var titleRow = new DockPanel { LastChildFill = true };
        titleRow.VerticalAlignment = VerticalAlignment.Center;
        titleRow.Margin = new Thickness(0, 0, 12, 0);
        _iconHost = new ContentControl();
        _iconHost.VerticalAlignment = VerticalAlignment.Center;
        _iconHost.Margin = new Thickness(0, 0, 10, 0);
        DockPanel.SetDock(_iconHost, Dock.Left);
        titleRow.Children.Add(_iconHost);
        _header = new TextBlock();
        _header.FontSize = 20;
        _header.FontWeight = FontWeights.SemiBold;
        _header.VerticalAlignment = VerticalAlignment.Center;
        _header.TextTrimming = TextTrimming.CharacterEllipsis;
        _header.TextWrapping = TextWrapping.NoWrap;
        titleRow.Children.Add(_header);
        Grid.SetColumn(titleRow, 0); Grid.SetRow(titleRow, 0);
        headGrid.Children.Add(titleRow);

        // subtitle (one-line explanation of what this window IS)
        _sub = new TextBlock();
        _sub.FontSize = 12.5;
        _sub.Margin = new Thickness(36, 5, 12, 0);
        _sub.TextWrapping = TextWrapping.Wrap;
        Grid.SetColumn(_sub, 0); Grid.SetColumnSpan(_sub, 2); Grid.SetRow(_sub, 1);
        headGrid.Children.Add(_sub);

        // FIX 1c: freshness line (row 1 col 1, right-aligned, under the controls)
        _freshnessLine = new TextBlock();
        _freshnessLine.FontSize = 11;
        _freshnessLine.Margin = new Thickness(0, 5, 0, 0);
        _freshnessLine.TextAlignment = TextAlignment.Right;
        _freshnessLine.VerticalAlignment = VerticalAlignment.Top;
        Grid.SetColumn(_freshnessLine, 1); Grid.SetRow(_freshnessLine, 1);
        headGrid.Children.Add(_freshnessLine);

        // separator line below the header
        var sep = new Border();
        sep.Height = 1;
        sep.Margin = new Thickness(0, 12, 0, 0);
        sep.BorderThickness = new Thickness(0);
        Grid.SetColumn(sep, 0); Grid.SetColumnSpan(sep, 2); Grid.SetRow(sep, 2);
        headGrid.Children.Add(sep);

        _headBar.Child = headGrid;
        root.Children.Add(_headBar);

        // ---- scrolling body ----
        _sv = new ScrollViewer();
        _sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _sv.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        _sv.Padding = new Thickness(20, 12, 20, 28);
        _body = new StackPanel();
        _sv.Background = Bg; _body.Background = Bg;
        _sv.Content = _body;
        root.Children.Add(_sv);

        Content = root;
        PaintChrome();
    }

    Button IconButton(string glyph, double size)
    {
        var b = new Button();
        b.Width = 36; b.Height = 30;
        b.Cursor = Cursors.Hand;
        b.BorderThickness = new Thickness(1);
        b.Margin = new Thickness(2, 0, 2, 0);
        b.Padding = new Thickness(5, 3, 5, 3);
        b.Content = MakeIcon(glyph, size, Fg);
        b.Tag = glyph;
        // FIX 2b: hover affordance -- faint background tint on mouse enter/leave
        b.MouseEnter += delegate(object s, MouseEventArgs e)
        {
            var btn = (Button)s;
            btn.Background = Theme.Br(Theme.Hover(_dark));
        };
        b.MouseLeave += delegate(object s, MouseEventArgs e)
        {
            var btn = (Button)s;
            btn.Background = BtnBg;
        };
        return b;
    }

    // FIX 1b: Refresh button using Unicode ⟳ (no refresh/sync glyph in the 8-glyph set)
    Button RefreshButton()
    {
        var b = new Button();
        b.Width = 36; b.Height = 30;
        b.Cursor = Cursors.Hand;
        b.BorderThickness = new Thickness(1);
        b.Margin = new Thickness(2, 0, 2, 0);
        b.Padding = new Thickness(5, 3, 5, 3);
        var t = new TextBlock();
        t.Text = "⟳";   // ⟳  CLOCKWISE GAPPED CIRCLE ARROW
        t.FontSize = 16;
        t.VerticalAlignment = VerticalAlignment.Center;
        t.HorizontalAlignment = HorizontalAlignment.Center;
        b.Content = t;
        b.Tag = "_refresh";
        b.MouseEnter += delegate(object s, MouseEventArgs e)
        {
            var btn = (Button)s;
            btn.Background = Theme.Br(Theme.Hover(_dark));
        };
        b.MouseLeave += delegate(object s, MouseEventArgs e)
        {
            var btn = (Button)s;
            btn.Background = BtnBg;
        };
        return b;
    }

    // FIX 2a: thin vertical divider between icon buttons
    Border MakeVDivider()
    {
        var d = new Border();
        d.Width = 1;
        d.Height = 17;
        d.VerticalAlignment = VerticalAlignment.Center;
        d.Margin = new Thickness(6, 0, 6, 0);
        d.Background = Muted;   // visible divider (user: make the line darker, not the faint Border)
        return d;
    }

    void PaintChrome()
    {
        Background = Bg;
        _headBar.Background = Bg;
        if (_sv != null)   _sv.Background   = Bg;
        if (_body != null) _body.Background = Bg;
        _header.Foreground = Fg;
        _header.Text = T("win_title");
        _sub.Foreground = Muted;
        _sub.Text = T("win_sub");
        _iconHost.Content = MakeIcon("account_tree", 24, Fg);

        // FIX 2c: repaint all icon buttons + dividers
        foreach (Button b in new Button[] { _themeBtn, _langBtn, _refreshBtn })
        {
            if (b != null)
            {
                b.Background  = BtnBg;
                b.Foreground  = Fg;
                b.BorderBrush = Border;
            }
        }
        if (_themeBtn   != null) _themeBtn.Content  = MakeIcon(_dark ? "light_mode" : "dark_mode", 18, Fg);
        if (_langBtn    != null) _langBtn.Content   = MakeIcon("translate", 18, Fg);
        // _refreshBtn content is a TextBlock; repaint its Foreground
        if (_refreshBtn != null)
        {
            _refreshBtn.ToolTip = _lang == 0 ? "更新" : "Refresh";
            var tb = _refreshBtn.Content as TextBlock;
            if (tb != null) tb.Foreground = Fg;
        }
        // repaint dividers with current border brush
        if (_divider1 != null) _divider1.Background = Muted;
        if (_divider2 != null) _divider2.Background = Muted;

        // FIX 1c: freshness line is updated on each tick; just set colour here
        if (_freshnessLine != null) _freshnessLine.Foreground = Muted;
    }

    void ForceRender() { _lastSig = ""; OnTick(null, null); }

    // ── poll loop ─────────────────────────────────────────────────────────────────
    void OnTick(object sender, EventArgs e)
    {
        try
        {
            if (File.Exists(SettingsFile))
            {
                long m = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
                if (m != _settingsMtime)
                {
                    bool d0 = _dark; int l0 = _lang;
                    LoadSettings();
                    if (d0 != _dark) { ApplyThemeBrushes(); PaintChrome(); _lastSig = ""; }
                    else if (l0 != _lang) { PaintChrome(); _lastSig = ""; }
                }
            }
        }
        catch (Exception) { }

        // FIX 1c: update freshness line every tick regardless of state change
        UpdateFreshnessLine();

        Dictionary<string, object> state = ReadState();
        if (state == null)
        {
            if (_lastSig != "NODATA" + (_dark ? "D" : "L") + _lang)
            {
                RenderNoData();
                _lastSig = "NODATA" + (_dark ? "D" : "L") + _lang;
            }
            return;
        }

        string sig = Sig(state);
        if (sig == _lastSig) return;
        _lastSig = sig;
        Render(state);
    }

    // FIX 1c: compute freshness from File.GetLastWriteTime(_jsonPath) and display as
    // "更新: 10:48 (たった今 / N分前)"  /  "Updated 10:48 (just now / N min ago)"
    void UpdateFreshnessLine()
    {
        if (_freshnessLine == null) return;
        try
        {
            if (!File.Exists(_jsonPath)) { _freshnessLine.Text = ""; return; }
            DateTime mtime = File.GetLastWriteTime(_jsonPath);
            double mins = (DateTime.Now - mtime).TotalMinutes;
            string timeStr = mtime.ToString("HH:mm");
            string ago;
            bool ja = _lang == 0;
            if (mins < 1.0)
                ago = ja ? "たった今" : "just now";
            else if (mins < 60.0)
                ago = ja ? ((int)mins).ToString() + "分前" : ((int)mins).ToString() + " min ago";
            else
            {
                int h = (int)(mins / 60);
                ago = ja ? h.ToString() + "時間前" : h.ToString() + " hr ago";
            }
            _freshnessLine.Text = (ja ? "更新: " : "Updated ") + timeStr + " (" + ago + ")";
        }
        catch (Exception) { _freshnessLine.Text = ""; }
    }

    Dictionary<string, object> ReadState()
    {
        try
        {
            if (!File.Exists(_jsonPath)) return null;
            string text;
            using (var fs = new FileStream(_jsonPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fs, Encoding.UTF8))
                text = sr.ReadToEnd();
            if (string.IsNullOrEmpty(text)) return null;
            return _js.DeserializeObject(text) as Dictionary<string, object>;
        }
        catch (Exception) { return null; }
    }

    // ── JSON-safe accessors ────────────────────────────────────────────────────────
    static string S(Dictionary<string, object> d, string k)
    { if (d != null && d.ContainsKey(k) && d[k] != null) return d[k].ToString(); return ""; }
    static int I(Dictionary<string, object> d, string k)
    { try { if (d != null && d.ContainsKey(k) && d[k] != null) return Convert.ToInt32(d[k]); } catch (Exception) { } return 0; }
    static Dictionary<string, object> Obj(Dictionary<string, object> d, string k)
    { if (d != null && d.ContainsKey(k)) return d[k] as Dictionary<string, object>; return null; }
    static object[] Arr(Dictionary<string, object> d, string k)
    { if (d != null && d.ContainsKey(k)) return d[k] as object[]; return null; }
    static bool AsBool(object o)
    { try { return o != null && Convert.ToBoolean(o); } catch (Exception) { return false; } }

    static string Num(object o, string fmt)
    {
        if (o == null) return "n/a";
        try { return Convert.ToDouble(o).ToString(fmt, System.Globalization.CultureInfo.InvariantCulture); }
        catch (Exception) { return "n/a"; }
    }
    static string Pp(object o)
    {
        if (o == null) return "n/a";
        try { return Convert.ToDouble(o).ToString("+0.0;-0.0", System.Globalization.CultureInfo.InvariantCulture) + "pp"; }
        catch (Exception) { return "n/a"; }
    }
    static string Pct(Dictionary<string, object> d, string k)
    {
        if (d == null || !d.ContainsKey(k) || d[k] == null) return "—";
        try { return (Convert.ToDouble(d[k]) * 100.0).ToString("0.0") + "%"; } catch (Exception) { return "—"; }
    }

    string Sig(Dictionary<string, object> state)
    {
        var sb = new StringBuilder();
        sb.Append(_dark ? "D" : "L").Append(_lang).Append('|');
        var sum = Obj(state, "summary");
        if (sum != null)
        {
            sb.Append(S(sum, "latest_pass_at_1")).Append('|');
            sb.Append(S(sum, "burned_total")).Append('|').Append(S(sum, "archive_count")).Append('|');
            var ab = Obj(sum, "latest_ab");
            if (ab != null) sb.Append(S(ab, "net_pp")).Append(S(ab, "p")).Append(S(ab, "verdict")).Append(S(ab, "keep"));
        }
        object[] hist = Arr(state, "ab_history");
        sb.Append('|').Append(hist != null ? hist.Length : 0);
        object[] pt = Arr(state, "pass1_trend");
        sb.Append('|').Append(pt != null ? pt.Length : 0);
        return sb.ToString();
    }

    // ── rendering ─────────────────────────────────────────────────────────────────

    // Empty / no-data state: friendly message in a single centered card, not a broken panel.
    void RenderNoData()
    {
        Background = Bg; _headBar.Background = Bg;
        _body.Children.Clear();

        // a calm, centred placeholder card
        var card = new Border();
        card.BorderThickness = new Thickness(1);
        card.CornerRadius    = new CornerRadius(10);
        card.Padding         = new Thickness(32, 28, 32, 28);
        card.Margin          = new Thickness(0, 24, 0, 0);
        card.BorderBrush     = Border;
        card.Background      = CardBg;
        card.HorizontalAlignment = HorizontalAlignment.Center;
        card.MaxWidth        = 560;

        var col = new StackPanel();
        col.HorizontalAlignment = HorizontalAlignment.Center;

        // icon  (use a neutral "info" look)
        var iconWrap = new ContentControl();
        iconWrap.Content = MakeIcon("auto_awesome", 36, Muted);
        iconWrap.HorizontalAlignment = HorizontalAlignment.Center;
        iconWrap.Margin = new Thickness(0, 0, 0, 14);
        col.Children.Add(iconWrap);

        var heading = new TextBlock();
        heading.Text = T("nodata_title");
        heading.Foreground = Fg;
        heading.FontSize = 15;
        heading.FontWeight = FontWeights.SemiBold;
        heading.TextAlignment = TextAlignment.Center;
        heading.Margin = new Thickness(0, 0, 0, 8);
        col.Children.Add(heading);

        var body = new TextBlock();
        body.Text = T("nodata_body");
        body.Foreground = Muted;
        body.FontSize = 12.5;
        body.TextWrapping = TextWrapping.Wrap;
        body.TextAlignment = TextAlignment.Center;
        body.LineHeight = 20;
        col.Children.Add(body);

        card.Child = col;
        _body.Children.Add(card);
    }

    void Render(Dictionary<string, object> state)
    {
        // ORDER IS AN ARGUMENT ABOUT WHAT MATTERS, and this one used to open on the audit
        // log. The archive is what the loop is FOR -- the solvers it has actually adopted --
        // and it sat last, as two integers, while the record of permission changes took the
        // first screen every time. The integrity checks still have to be reachable without
        // hunting, which is why they collapse to one line rather than moving out of sight,
        // and why they expand themselves when a check fails.
        _body.Children.Clear();
        var pending = BuildPending(state);
        if (pending != null) _body.Children.Add(pending);
        _body.Children.Add(BuildArchive(state));
        _body.Children.Add(BuildScorecard(state));
        _body.Children.Add(BuildAbHistory(state));
        _body.Children.Add(BuildPassTrend(state));
        _body.Children.Add(BuildUsage(state));
        _body.Children.Add(BuildBurnedLedger(state));
        _body.Children.Add(BuildAuthority());
    }

    // ── AUTHORITY ───────────────────────────────────────────────────────────────
    // What changed what this system may become, and the one way back.
    //
    // READ STRAIGHT FROM THE LEDGER AND THE BASELINE, not from the dashboard json. The json
    // is written by the same python that performs these acts, and a component reporting that
    // its own record is fine is self-report, not monitoring -- the moment worth watching is
    // exactly the moment that component is misbehaving. The two checks below are computed
    // here, by different code, from the files themselves.
    //
    // AGREEMENT WITH PYTHON WAS MEASURED, 2026-08-20, not assumed. Two implementations of one
    // rule is a false-alarm generator unless they are actually compared: this logic was run
    // standalone against the live repository and reported exactly what frozen_intact() did --
    // the same single differing file, and the same anchor verdict. Re-check it after touching
    // either side; a monitor that disagrees with the thing it monitors is worse than none.
    //
    // WHAT IS NOT CHECKED HERE, so the green does not claim more than it is: the per-record
    // content hashes are not recomputed. Reproducing python's canonical JSON in C# means
    // matching float formatting and non-ASCII escaping exactly, and getting that subtly wrong
    // produces a monitor that cries "tampered" at an untouched ledger -- worse than a stated
    // limit. Links and sequence are checked; contents are python's `verify()` to confirm.

    static string LedgerPath()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                            ".selfimprove_ledger.jsonl");
    }

    static string AnchorPath()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                            ".selfimprove_frozen_anchor");
    }

    static string RepoRoot()
    {
        return Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
    }

    // sha256 with CRLF folded to LF -- the same normalisation python uses, and the reason it
    // does: a Windows checkout materialises these files with CRLF and the raw bytes would
    // differ from a baseline taken anywhere else.
    static string Sha256Lf(string path)
    {
        try
        {
            byte[] raw = File.ReadAllBytes(path);
            var outb = new List<byte>(raw.Length);
            for (int i = 0; i < raw.Length; i++)
            {
                if (raw[i] == 0x0d && i + 1 < raw.Length && raw[i + 1] == 0x0a) continue;
                outb.Add(raw[i]);
            }
            using (var sha = System.Security.Cryptography.SHA256.Create())
            {
                byte[] h = sha.ComputeHash(outb.ToArray());
                var sb = new StringBuilder();
                foreach (byte b in h) sb.Append(b.ToString("x2"));
                return sb.ToString();
            }
        }
        catch (Exception) { return null; }
    }

    List<Dictionary<string, object>> ReadLedger()
    {
        var rows = new List<Dictionary<string, object>>();
        try
        {
            string p = LedgerPath();
            if (!File.Exists(p)) return rows;
            foreach (string line in File.ReadAllLines(p, Encoding.UTF8))
            {
                string t = line.Trim();
                if (t.Length == 0) continue;
                try { rows.Add((Dictionary<string, object>)_js.DeserializeObject(t)); }
                catch (Exception) { }
            }
        }
        catch (Exception) { }
        return rows;
    }

    // links + sequence only. See the note above the section.
    static bool LedgerLinksHold(List<Dictionary<string, object>> rows, out string problem)
    {
        problem = null;
        string prev = null;
        for (int i = 0; i < rows.Count; i++)
        {
            object seq, ph, h;
            rows[i].TryGetValue("seq", out seq);
            rows[i].TryGetValue("prev_hash", out ph);
            rows[i].TryGetValue("hash", out h);
            if (seq == null || Convert.ToInt32(seq) != i)
            { problem = "seq " + (seq == null ? "?" : seq.ToString()) + " != " + i; return false; }
            string phs = ph == null ? null : ph.ToString();
            if (phs != prev) { problem = "link breaks at seq " + i; return false; }
            prev = h == null ? null : h.ToString();
        }
        return true;
    }

    // the frozen set, recomputed here from the baseline json and the files on disk
    bool FrozenMatches(out int checkedCount, out List<string> differing, out bool anchorOk)
    {
        checkedCount = 0; differing = new List<string>(); anchorOk = false;
        try
        {
            string root = RepoRoot();
            string bp = Path.Combine(root, "relay", "selfimprove", "frozen_baseline.json");
            if (!File.Exists(bp)) { differing.Add("NO_BASELINE"); return false; }
            var doc = (Dictionary<string, object>)_js.DeserializeObject(
                File.ReadAllText(bp, Encoding.UTF8));
            object sumsObj; doc.TryGetValue("checksums", out sumsObj);
            var sums = sumsObj as Dictionary<string, object>;
            if (sums == null) { differing.Add("NO_CHECKSUMS"); return false; }
            foreach (var kv in sums)
            {
                checkedCount++;
                string actual = Sha256Lf(Path.Combine(root, kv.Key.Replace('/', Path.DirectorySeparatorChar)));
                if (actual == null || actual != Convert.ToString(kv.Value)) differing.Add(kv.Key);
            }
            string anchorFile = AnchorPath();
            if (File.Exists(anchorFile))
                anchorOk = File.ReadAllText(anchorFile, Encoding.UTF8).Trim() == Sha256Lf(bp);
        }
        catch (Exception) { differing.Add("UNREADABLE"); }
        return differing.Count == 0;
    }

    UIElement BuildAuthority()
    {
        var card = SectionCard("auth_sec", "auth_exp");
        var col  = (StackPanel)card.Child;

        var rows = ReadLedger();
        int nChecked; List<string> differing; bool anchorOk;
        bool intact = FrozenMatches(out nChecked, out differing, out anchorOk);
        string chainProblem; bool linksOk = LedgerLinksHold(rows, out chainProblem);

        // -- rate first: it belongs in the same one-line verdict as the three checks, so it
        //    has to be computed before the header is built. A bare number is not monitoring;
        //    it needs a usual level to sit against.
        double now = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;
        int last7 = 0; int prior = 0;
        foreach (var r0 in rows)
        {
            object ev0, ts0;
            r0.TryGetValue("event", out ev0); r0.TryGetValue("ts", out ts0);
            if (ev0 == null || Convert.ToString(ev0) != "rebless" || ts0 == null) continue;
            double age = now - Convert.ToDouble(ts0);
            if (age <= 7 * 86400) last7++; else if (age <= 35 * 86400) prior++;
        }
        double usual = prior / 4.0;
        bool hot = last7 > Math.Max(2.0, usual * 3.0);
        string rateTxt = T("auth_rate") + ": " + last7
                       + (usual > 0 ? "  (~" + usual.ToString("0.#") + ")" : "");

        // -- current state, always visible. An event list alone buries the one fact that
        //    decides whether anything else on this screen can be trusted.
        //
        //    SETTLED AS ONE LINE WHEN IT IS SETTLED. Four saturated badges announcing that
        //    nothing is wrong is an instruction to read four things in order to learn
        //    nothing, and it spends the reader's attention precisely where there is nothing
        //    to spend it on. A chip is now reserved for a check that FAILED; everything
        //    holding collapses into one muted line. Nothing is hidden by this -- every count
        //    and every verdict is still on screen, at the weight the news deserves.
        var okBits = new List<string>();
        var head = new WrapPanel(); head.Margin = new Thickness(0, 10, 0, 0);

        if (intact) okBits.Add(T("auth_intact") + ": " + T("auth_ok") + " (" + nChecked + ")");
        else head.Children.Add(Pill(T("auth_intact") + ": " + T("auth_broken")
                                    + " (" + nChecked + ")", "bad"));

        if (anchorOk) okBits.Add(T("auth_anchor") + ": " + T("auth_ok"));
        else head.Children.Add(Pill(T("auth_anchor") + ": " + T("auth_broken"), "bad"));

        if (linksOk) okBits.Add(T("auth_chain") + ": " + T("auth_chain_ok"));
        else head.Children.Add(Pill(T("auth_chain") + ": " + chainProblem, "bad"));

        if (hot) head.Children.Add(Pill(rateTxt + "  \u2014  " + T("auth_rate_hi"), "warn"));
        else okBits.Add(rateTxt);

        // EVERYTHING PASSING MEANS ONE LINE. The detail pane below carries the records, the
        // disclosure of what this check does not cover, and the way back; it opens on click,
        // and it opens BY ITSELF the moment any check fails, so the state that needs reading
        // is never the state you have to go looking for. What stays visible when it is shut
        // is the whole verdict -- three checks and the re-signing count -- so collapsing
        // withholds detail, never news.
        bool allWell = intact && anchorOk && linksOk && !hot;
        var detail = new StackPanel();
        detail.Visibility = allWell ? Visibility.Collapsed : Visibility.Visible;

        if (head.Children.Count > 0) col.Children.Add(head);
        if (okBits.Count > 0)
        {
            var okLine = new TextBlock();
            okLine.Text = (head.Children.Count == 0 ? "\u2713  " : "")
                        + string.Join("    \u00b7    ", okBits.ToArray());
            okLine.Foreground = Muted; okLine.FontSize = Theme.FsMeta;
            okLine.TextWrapping = TextWrapping.Wrap;
            okLine.Margin = new Thickness(0, head.Children.Count > 0 ? 8 : 10, 0, 0);
            col.Children.Add(okLine);
        }

        var toggle = new TextBlock();
        toggle.Text = (allWell ? "\u25b8  " : "\u25be  ") + T("auth_detail");
        toggle.Foreground = Accent; toggle.FontSize = 11.5;
        toggle.Margin = new Thickness(0, 8, 0, 0);
        toggle.Cursor = System.Windows.Input.Cursors.Hand;
        var detailRef = detail; var toggleRef = toggle;
        toggle.MouseLeftButtonUp += delegate
        {
            bool open = detailRef.Visibility != Visibility.Visible;
            detailRef.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
            toggleRef.Text = (open ? "\u25be  " : "\u25b8  ") + T("auth_detail");
        };
        col.Children.Add(toggle);
        col.Children.Add(detail);

        // WHICH FILES ARE ABSORBING THE RATE. A count alone says the ledger is growing; it
        // does not say what to do about it. Repeated re-signings of one file are the actual
        // signal -- either the workflow keeps touching something that should not be frozen,
        // or one change is being approved in pieces -- and that is the question a reader has
        // once they see 27 in a week. Computed here from the same rows as the count above.
        var churn = new Dictionary<string, int>();
        foreach (var r1 in rows)
        {
            object e1, t1;
            r1.TryGetValue("event", out e1); r1.TryGetValue("ts", out t1);
            if (Convert.ToString(e1) != "rebless" || t1 == null) continue;
            try { if (now - Convert.ToDouble(t1) > 7 * 86400) continue; }
            catch (Exception) { continue; }
            foreach (var f in ChangedPaths(r1))
            {
                string b = f;
                int sl = b.LastIndexOfAny(new char[] { '/', '\\' });
                if (sl >= 0) b = b.Substring(sl + 1);
                churn[b] = (churn.ContainsKey(b) ? churn[b] : 0) + 1;
            }
        }
        if (churn.Count > 0)
        {
            var names = new List<string>(churn.Keys);
            names.Sort(delegate (string x, string y) { return churn[y].CompareTo(churn[x]); });
            var bits = new List<string>();
            for (int n = 0; n < names.Count && n < 5; n++)
                bits.Add(names[n] + " \u00d7" + churn[names[n]]);
            col.Children.Add(MuteRow(T("ev_churn") + ": " + string.Join("  \u00b7  ", bits.ToArray())));
        }

        if (!intact && differing.Count > 0)
            detail.Children.Add(MuteRow(string.Join(", ", differing.ToArray())));
        detail.Children.Add(MuteRow(T("auth_verified_here")));

        // -- the events themselves, newest first. Previously one run-on line concatenated the
        //    event kind, the actor and the touched files with no separator, and dropped the
        //    reason to muted underneath -- so the four different kinds of thing inside a
        //    record were typographically the same thing, and the record's actual content was
        //    the faintest part of it. Now each record is a rail-marked block: what happened
        //    and when on the first line, WHY in body text on the second, where underneath.
        // WHICH MISMATCH DID A RE-SIGNING CLOSE. Chronologically the drift is detected first
        // and the operator re-signs after, so the pair is (mismatch, the rebless that follows
        // it). Read off the screen instead -- where the list runs newest first -- the pair
        // looks reversed, and collapsing "the mismatch after a rebless" would hide the ones
        // nothing has answered yet. Those are the entire reason this ledger exists.
        //
        // Only collapsed when both name the same files. A mismatch that detected something
        // other than what was then approved is the anomaly a reader should see first.
        var closedBy = new Dictionary<int, int>();
        if (linksOk)
        {
            for (int i = 0; i + 1 < rows.Count; i++)
            {
                object a, b;
                rows[i].TryGetValue("event", out a);
                rows[i + 1].TryGetValue("event", out b);
                if (Convert.ToString(a) != "baseline_mismatch") continue;
                if (Convert.ToString(b) != "rebless") continue;
                if (!SameTargets(rows[i], rows[i + 1])) continue;
                closedBy[i] = i + 1;
            }
        }

        var summaries = LoadSummaries();
        int testRecords = 0;
        if (linksOk) foreach (var r0 in rows) if (IsTestRecord(r0)) testRecords++;

        int shown = 0;
        for (int i = rows.Count - 1; i >= 0 && shown < 8; i--)
        {
            object ev; rows[i].TryGetValue("event", out ev);
            string kind = Convert.ToString(ev);
            if (kind == "genesis") continue;
            if (closedBy.ContainsKey(i)) continue;          // shown on the re-signing that closed it
            if (linksOk && IsTestRecord(rows[i])) continue; // counted once, below

            object actor, reason, auth, ts;
            rows[i].TryGetValue("actor_claimed", out actor);
            rows[i].TryGetValue("reason", out reason);
            rows[i].TryGetValue("authorization", out auth);
            rows[i].TryGetValue("ts", out ts);
            var paths = ChangedPaths(rows[i]);

            // Any mismatch reaching this point was not collapsed, so nothing closed it --
            // EXCEPT when the chain check failed, where no pairing was computed at all. Calling
            // all 24 of them unresolved there would be a false alarm produced by the very
            // condition that already tells the reader not to trust this section.
            bool unresolved = linksOk && kind == "baseline_mismatch";

            var body = new StackPanel();

            // line 1 -- the headline, computed: what happened, to what, when
            var g = new Grid();
            var c0 = new ColumnDefinition(); c0.Width = new GridLength(1, GridUnitType.Star);
            var c1 = new ColumnDefinition(); c1.Width = GridLength.Auto;
            g.ColumnDefinitions.Add(c0); g.ColumnDefinitions.Add(c1);

            string title = EventVerb(kind);
            string targets = BaseNames(paths);
            if (targets.Length > 0) title = title + ": " + targets;
            if (unresolved) title = title + "  \u2014  " + T("ev_unresolved");

            var titleTb = ClipLine(title, unresolved ? KindBrush("baseline_mismatch") : Fg,
                                   13.0, false);
            titleTb.FontWeight = FontWeights.SemiBold;
            Grid.SetColumn(titleTb, 0); g.Children.Add(titleTb);

            if (ts != null)
            {
                var when = new TextBlock();
                when.Text = AgoText(Convert.ToDouble(ts), now);
                when.Foreground = Theme.Br(Theme.Faint(_dark));
                when.FontSize = 11;
                when.VerticalAlignment = VerticalAlignment.Center;
                when.Margin = new Thickness(12, 0, 0, 0);
                when.ToolTip = new DateTime(1970, 1, 1).AddSeconds(Convert.ToDouble(ts))
                                   .ToLocalTime().ToString("yyyy-MM-dd HH:mm");
                Grid.SetColumn(when, 1); g.Children.Add(when);
            }
            body.Children.Add(g);

            // The mismatch this re-signing answered, folded in. The gap between detection and
            // approval is information the two separate rows did not carry.
            int mism = -1;
            foreach (var kv in closedBy) if (kv.Value == i) { mism = kv.Key; break; }
            if (mism >= 0)
            {
                object mts; rows[mism].TryGetValue("ts", out mts);
                string det = "?", gap = "";
                try
                {
                    double mv = Convert.ToDouble(mts);
                    det = new DateTime(1970, 1, 1).AddSeconds(mv).ToLocalTime().ToString("HH:mm");
                    gap = GapText(mv, Convert.ToDouble(ts));
                }
                catch (Exception) { }
                var closed = new TextBlock();
                closed.Text = string.Format(T("ev_closed_by"), det, gap);
                closed.Foreground = Theme.Br(Theme.Faint(_dark));
                closed.FontSize = 11;
                closed.Margin = new Thickness(0, 3, 0, 0);
                body.Children.Add(closed);
            }

            // line 2 -- WHY. The summary when there is one, and the recorded words underneath
            // it, collapsed: showing both at once restores exactly the length this section was
            // just cut down from, and the summary is only trustworthy if the original is one
            // click away rather than gone.
            string sum = SummaryFor(rows[i], summaries);
            var reasonTb = ClipLine(Convert.ToString(reason), sum.Length > 0 ? Muted : Fg,
                                    Theme.FsMeta, false);
            reasonTb.Margin = new Thickness(0, sum.Length > 0 ? 3 : 4, 0, 0);

            if (sum.Length > 0)
            {
                var sumRow = new StackPanel();
                sumRow.Orientation = Orientation.Horizontal;
                sumRow.Margin = new Thickness(0, 4, 0, 0);
                var tag = new TextBlock();
                tag.Text = T("ev_summary");
                tag.Foreground = Theme.Br(Theme.Faint(_dark));
                tag.FontSize = 10.5;
                tag.VerticalAlignment = VerticalAlignment.Center;
                tag.Margin = new Thickness(0, 0, 8, 0);
                sumRow.Children.Add(tag);
                var sumTb = new TextBlock();
                sumTb.Text = sum;
                sumTb.Foreground = Fg; sumTb.FontSize = Theme.FsMeta;
                sumTb.TextWrapping = TextWrapping.Wrap;
                sumRow.Children.Add(sumTb);
                sumRow.ToolTip = T("ev_summary_tip");
                body.Children.Add(sumRow);
                reasonTb.Visibility = Visibility.Collapsed;   // one click away, never gone
            }
            body.Children.Add(reasonTb);

            // line 3 -- where, in full, and who claimed to act. The actor used to be the
            // headline; it takes two values across the whole ledger.
            TextBlock scopeTb = null;
            if (paths.Count > 0)
            {
                string tail = string.Join(", ", paths.ToArray());
                string who = Convert.ToString(actor);
                if (who.Length > 0) tail = tail + "    " + T("ev_by") + " " + who;
                scopeTb = ClipLine(tail, Theme.Br(Theme.Faint(_dark)), Theme.FsLog, true);
                scopeTb.Margin = new Thickness(0, 3, 0, 0);
                body.Children.Add(scopeTb);
            }

            // line 4 -- the operator's own words. Verbatim, and kept in the quote form this
            // file already reserves for verbatim material.
            if (auth != null && Convert.ToString(auth) != "self-initiated")
            {
                var q = new Border();
                q.Background = QuoteBg;
                q.CornerRadius = new CornerRadius(Theme.RadSmall);
                q.Padding = new Thickness(8, 4, 8, 4);
                q.Margin = new Thickness(0, 6, 0, 0);
                q.HorizontalAlignment = HorizontalAlignment.Left;
                var qt = new TextBlock();
                qt.Text = "\u201c" + Convert.ToString(auth) + "\u201d";
                qt.Foreground = Muted; qt.FontSize = Theme.FsMeta;
                qt.TextWrapping = TextWrapping.Wrap; qt.MaxWidth = 620;
                q.Child = qt;
                body.Children.Add(q);
            }

            var rec = new Border();
            rec.BorderThickness = new Thickness(0, 0, 0, 1);
            rec.BorderBrush = new SolidColorBrush(Mix(Theme.Col(Theme.Faint(_dark)), CardColor(), 0.22));
            rec.Padding = new Thickness(0, 0, 0, 12);
            rec.Margin  = new Thickness(0, 12, 0, 0);
            rec.Background = Brushes.Transparent;
            rec.Cursor = System.Windows.Input.Cursors.Hand;
            rec.Child = body;
            var rTb = reasonTb; var sTb = scopeTb; var tTb = titleTb;
            rec.MouseLeftButtonUp += delegate
            {
                bool open = rTb.Visibility != Visibility.Visible
                         || rTb.TextWrapping == TextWrapping.NoWrap;
                rTb.Visibility = Visibility.Visible;
                SetClip(rTb, open); SetClip(sTb, open); SetClip(tTb, open);
            };
            detail.Children.Add(rec);
            shown++;
        }

        // Counted, never deleted: they are real rows in an append-only file. What they are not
        // is events in this repository's life, and 20 of 78 of them crowded out the ones that
        // were. Closing the route that writes them belongs upstream, not here.
        if (testRecords > 0)
            detail.Children.Add(MuteRow(string.Format(T("ev_testrecords"), testRecords)));

        if (shown == 0) detail.Children.Add(MuteRow(T("auth_none")));

        // -- the way back. One step, repeatable. Jumping to an arbitrary earlier point would
        //    be CHOOSING a state to install, and choosing is what promotion is; withdrawing
        //    the last act is the only move that is purely an undo.
        var btn = new Button();
        btn.Content = T("auth_revoke");
        btn.Padding = new Thickness(16, 6, 16, 6);
        btn.Margin  = new Thickness(0, 14, 0, 0);
        btn.HorizontalAlignment = HorizontalAlignment.Left;
        btn.Click += delegate { RevokeLastRebless(); };
        detail.Children.Add(btn);
        return card;
    }

    void RevokeLastRebless()
    {
        // the landing point, named before the click is confirmed
        string landing = null;
        var rows = ReadLedger();
        for (int i = rows.Count - 1; i >= 0; i--)
        {
            object ev; rows[i].TryGetValue("event", out ev);
            if (Convert.ToString(ev) != "rebless") continue;
            object reason, seq;
            rows[i].TryGetValue("reason", out reason); rows[i].TryGetValue("seq", out seq);
            landing = "seq=" + Convert.ToString(seq) + "  " + Convert.ToString(reason);
            break;
        }
        if (landing == null)
        {
            MessageBox.Show(this, T("auth_nothing"), T("auth_revoke_t"),
                            MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        // The same shape the cockpit uses for a high-impact action. Not a third dialect of
        // confirmation -- one system, one way of asking.
        if (MessageBox.Show(this, T("auth_revoke_q") + landing, T("auth_revoke_t"),
                            MessageBoxButton.YesNo, MessageBoxImage.Warning,
                            MessageBoxResult.No) != MessageBoxResult.Yes) return;

        string root   = RepoRoot();
        string venvPy = Path.Combine(root, ".venv", "Scripts", "python.exe");
        string pyExe  = File.Exists(venvPy) ? venvPy : "python";
        var psi = new ProcessStartInfo();
        psi.FileName  = pyExe;
        psi.Arguments = "-m relay.selfimprove.frozen --revoke --reason "
                      + "\"withdrawn from the self-improvement dashboard\"";
        psi.WorkingDirectory       = root;
        psi.UseShellExecute        = false;
        psi.CreateNoWindow         = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError  = true;
        try
        {
            var proc = new Process(); proc.StartInfo = psi; proc.Start();
            // WAITED FOR, unlike the feed regeneration. A fire-and-forget revoke that died
            // would leave no trace at all: the ledger records successes, so a failed attempt
            // is invisible unless this window keeps the exit code and the stderr.
            string so = proc.StandardOutput.ReadToEnd();
            string se = proc.StandardError.ReadToEnd();
            proc.WaitForExit(60000);
            if (proc.ExitCode == 0)
                MessageBox.Show(this, T("auth_revoke_ok") + "\n\n" + so, T("auth_revoke_t"),
                                MessageBoxButton.OK, MessageBoxImage.Information);
            else
                MessageBox.Show(this, T("auth_revoke_no") + " (exit " + proc.ExitCode + ")\n\n"
                                + so + "\n" + se, T("auth_revoke_t"),
                                MessageBoxButton.OK, MessageBoxImage.Error);
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, T("auth_revoke_no") + "\n\n" + ex.Message,
                            T("auth_revoke_t"), MessageBoxButton.OK, MessageBoxImage.Error);
        }
        ForceRender();
    }

    // ── (0) LIVE USAGE — general-user lens ───────────────────────────────────────
    // Real-run completion / turns / trend, no benchmark.
    UIElement BuildUsage(Dictionary<string, object> state)
    {
        var u    = Obj(state, "usage");
        var card = SectionCard("usage_sec", "usage_exp");
        var col  = (StackPanel)card.Child;

        if (u == null || I(u, "n_tasks") == 0)
        {
            col.Children.Add(MuteRow(T("none")));
            return card;
        }

        // metrics row
        var grid = new Grid(); grid.Margin = new Thickness(0, 10, 0, 0);
        for (int i = 0; i < 4; i++)
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        var goodBrush = new SolidColorBrush(StatusColorFor("good", _dark));
        grid.Children.Add(MetricCell(T("u_completion"), Pct(u, "completion_rate"),  goodBrush, 0));
        grid.Children.Add(MetricCell(T("u_recent"),     Pct(u, "recent_completion_rate"), Fg, 1));
        grid.Children.Add(MetricCell(T("u_turns"),
            Num(u.ContainsKey("median_turns") ? u["median_turns"] : null, "0.#"), Fg, 2));
        grid.Children.Add(MetricCell(T("u_tasks"), I(u, "n_tasks").ToString(), Fg, 3));
        col.Children.Add(grid);

        // status mix (counts by outcome)
        var mix = Obj(u, "status_mix");
        if (mix != null && mix.Count > 0)
        {
            var parts = new List<string>();
            foreach (KeyValuePair<string, object> kv in mix)
                parts.Add(kv.Key + ": " + kv.Value);
            var mixLine = new TextBlock();
            mixLine.Text = string.Join("    ·    ", parts.ToArray());
            mixLine.Foreground = Muted;
            mixLine.FontSize = 12;
            mixLine.Margin = new Thickness(0, 10, 0, 0);
            mixLine.TextWrapping = TextWrapping.Wrap;
            col.Children.Add(mixLine);
        }

        // completion trend
        object[] tr = Arr(u, "trend");
        if (tr != null && tr.Length > 1)
        {
            var label = new TextBlock();
            label.Text = T("u_trend");
            label.Foreground = Muted;
            label.FontSize = 11;
            label.Margin = new Thickness(0, 12, 0, 3);
            col.Children.Add(label);

            var sb = new StringBuilder();
            for (int i = 0; i < tr.Length; i++)
            {
                double v = 0.0;
                try { if (tr[i] != null) v = Convert.ToDouble(tr[i]); } catch (Exception) { }
                if (i > 0) sb.Append("  →  ");
                sb.Append(((int)System.Math.Round(v * 100)).ToString() + "%");
            }
            var trendLine = new TextBlock();
            trendLine.Text = sb.ToString();
            trendLine.Foreground = Fg;
            trendLine.FontSize = 13;
            trendLine.FontWeight = FontWeights.SemiBold;
            col.Children.Add(trendLine);
        }

        // persona / advice leak rate — quality metric measured from real usage (no benchmark).
        BuildPersonaLeak(u, col);

        return card;
    }

    // persona_leak_rate : float|null (share of outputs leaking coaching/lecture/ego),
    // quality_scored : int (how many runs could be scored),
    // persona_flagged : [{key, signals[], excerpt}, ...] (leak examples, may be empty).
    // null leak rate => show "計測前" (no fabricated number). Flagged shown only when non-empty.
    void BuildPersonaLeak(Dictionary<string, object> u, StackPanel col)
    {
        // divider above the quality block to set it apart from the benchmark-free usage counts
        col.Children.Add(HRule());

        // label row: metric name + value (value coloured by whether any leak)
        var labelRow = new StackPanel();
        labelRow.Orientation = Orientation.Horizontal;
        labelRow.Margin = new Thickness(0, 8, 0, 0);

        var nameTb = new TextBlock();
        nameTb.Text = T("u_persona") + ": ";
        nameTb.Foreground = Muted; nameTb.FontSize = 12.5;
        nameTb.VerticalAlignment = VerticalAlignment.Center;
        labelRow.Children.Add(nameTb);

        object rateObj = u.ContainsKey("persona_leak_rate") ? u["persona_leak_rate"] : null;
        int scored = I(u, "quality_scored");

        var valTb = new TextBlock();
        valTb.FontSize = 15; valTb.FontWeight = FontWeights.SemiBold;
        valTb.VerticalAlignment = VerticalAlignment.Center;

        if (rateObj == null)
        {
            // null => not yet measured; do NOT invent a number
            valTb.Text = T("u_persona_pre");
            valTb.Foreground = Muted;
        }
        else
        {
            double rate = 0.0;
            bool ok = true;
            try { rate = Convert.ToDouble(rateObj); } catch (Exception) { ok = false; }
            if (!ok)
            {
                valTb.Text = T("u_persona_pre");
                valTb.Foreground = Muted;
            }
            else
            {
                valTb.Text = (rate * 100.0).ToString("0.0", System.Globalization.CultureInfo.InvariantCulture) + "%";
                // green when clean (0%), warning otherwise
                valTb.Foreground = new SolidColorBrush(StatusColorFor(rate <= 0.0 ? "good" : "warn", _dark));
            }
        }
        labelRow.Children.Add(valTb);

        if (scored > 0)
        {
            var nTb = new TextBlock();
            nTb.Text = "  (" + scored.ToString() + T("u_persona_n") + ")";
            nTb.Foreground = Muted; nTb.FontSize = 12;
            nTb.VerticalAlignment = VerticalAlignment.Center;
            labelRow.Children.Add(nTb);
        }
        col.Children.Add(labelRow);

        // helper line: stresses this number is from real usage, not a benchmark
        col.Children.Add(SectionExplanation(T("u_persona_exp")));

        // flagged excerpts (real leak examples) — only when non-empty
        object[] flagged = Arr(u, "persona_flagged");
        if (flagged != null && flagged.Length > 0)
        {
            var egLbl = new TextBlock();
            egLbl.Text = T("u_persona_eg");
            egLbl.Foreground = Muted; egLbl.FontSize = 11;
            egLbl.FontWeight = FontWeights.SemiBold;
            egLbl.Margin = new Thickness(0, 8, 0, 3);
            col.Children.Add(egLbl);

            int shown = 0;
            for (int i = 0; i < flagged.Length && shown < 3; i++)
            {
                var fr = flagged[i] as Dictionary<string, object>;
                if (fr == null) continue;
                string excerpt = S(fr, "excerpt");
                if (string.IsNullOrEmpty(excerpt)) continue;

                var quote = new Border();
                quote.Background      = QuoteBg;
                quote.CornerRadius    = new CornerRadius(6);
                // The tinted ground already says "this is quoted material"; the bar only made
                // it a sticky note.
                quote.Padding         = new Thickness(9, 5, 9, 5);
                quote.Margin          = new Thickness(0, 3, 0, 0);

                var qt = new TextBlock();
                qt.Text = excerpt;
                qt.Foreground = Muted; qt.FontSize = 11.5;
                qt.TextWrapping = TextWrapping.Wrap;
                quote.Child = qt;
                col.Children.Add(quote);
                shown++;
            }
        }
    }

    // ── (1) SCORECARD ────────────────────────────────────────────────────────────
    // Latest pass@1, latest A/B verdict, burned count, archive count.
    UIElement BuildScorecard(Dictionary<string, object> state)
    {
        var sum = Obj(state, "summary");
        var ab  = sum != null ? Obj(sum, "latest_ab") : null;

        string vk = ab != null ? VerdictKey(ab.ContainsKey("keep") ? ab["keep"] : null, S(ab, "verdict")) : "muted";
        Color  sc = StatusColorFor(vk, _dark);

        // scorecard uses a subtle tinted border to convey the latest verdict at a glance
        var card = new Border();
        card.BorderThickness = new Thickness(1.4);
        card.CornerRadius    = new CornerRadius(10);
        card.Padding         = new Thickness(18, 14, 16, 16);
        card.Margin          = new Thickness(0, 8, 0, 8);
        card.BorderBrush     = new SolidColorBrush(Mix(sc, BgColor(), 0.45));
        card.Background      = new SolidColorBrush(Mix(sc, CardColor(), 0.07));

        var col = new StackPanel();

        // section label + verdict chip on one row
        var topRow = new DockPanel { LastChildFill = false };
        topRow.Margin = new Thickness(0, 0, 0, 2);

        var sLabel = SectionLabel(T("scorecard_sec"));
        DockPanel.SetDock(sLabel, Dock.Left);
        topRow.Children.Add(sLabel);

        if (ab != null)
        {
            string vtext = string.IsNullOrEmpty(S(ab, "verdict")) ? "?" : S(ab, "verdict");
            var chip = Pill(vtext, vk);
            chip.Margin = new Thickness(10, 0, 0, 0);
            DockPanel.SetDock(chip, Dock.Left);
            topRow.Children.Add(chip);
        }
        col.Children.Add(topRow);

        // explanation
        col.Children.Add(SectionExplanation(T("scorecard_exp")));

        // metrics row
        var grid = new Grid(); grid.Margin = new Thickness(0, 12, 0, 0);
        for (int i = 0; i < 4; i++)
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        string pass1 = sum != null ? Num(sum.ContainsKey("latest_pass_at_1") ? sum["latest_pass_at_1"] : null, "0.000") : "n/a";
        grid.Children.Add(MetricCell(T("latest_pass"), pass1, Fg, 0));

        string abVal;
        if (ab != null)
        {
            string verdict = string.IsNullOrEmpty(S(ab, "verdict")) ? "n/a" : S(ab, "verdict");
            abVal = verdict + "  " + Pp(ab.ContainsKey("net_pp") ? ab["net_pp"] : null)
                            + "  p=" + Num(ab.ContainsKey("p") ? ab["p"] : null, "0.000");
        }
        else
        {
            abVal = "n/a";
        }
        grid.Children.Add(MetricCell(T("latest_ab"), abVal, new SolidColorBrush(sc), 1));
        grid.Children.Add(MetricCell(T("burned_total"),  I(sum, "burned_total").ToString(),  Fg, 2));
        grid.Children.Add(MetricCell(T("archive_count"), I(sum, "archive_count").ToString(), Fg, 3));
        col.Children.Add(grid);

        card.Child = col;
        return card;
    }

    // ── (2) A/B HISTORY ──────────────────────────────────────────────────────────
    // Newest-first list: toggle name, sample size, net pp, p-value, verdict chip.
    UIElement BuildAbHistory(Dictionary<string, object> state)
    {
        var card = SectionCard("ab_sec", "ab_exp");
        var col  = (StackPanel)card.Child;
        object[] hist = Arr(state, "ab_history");

        if (hist == null || hist.Length == 0)
        {
            col.Children.Add(MuteRow(T("none")));
            return card;
        }

        // column header labels
        var hdr = new Grid(); hdr.Margin = new Thickness(0, 8, 0, 2);
        hdr.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(3, GridUnitType.Star) });
        hdr.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        hdr.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        hdr.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        hdr.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(80) });
        hdr.Children.Add(ColHeader(_lang == 0 ? "トグル" : "Toggle",        0));
        hdr.Children.Add(ColHeader("n",                                      1));
        hdr.Children.Add(ColHeader(_lang == 0 ? "差分" : "Net",             2));
        hdr.Children.Add(ColHeader("p-value",                                3));
        hdr.Children.Add(ColHeader(_lang == 0 ? "採否" : "Verdict",         4));
        col.Children.Add(hdr);
        col.Children.Add(HRule());

        // newest-first
        for (int i = hist.Length - 1; i >= 0; i--)
        {
            var r = hist[i] as Dictionary<string, object>;
            if (r == null) continue;
            string vk = VerdictKey(r.ContainsKey("keep") ? r["keep"] : null, S(r, "verdict"));
            Color  sc = StatusColorFor(vk, _dark);

            // No accent strip: the verdict is already stated twice in the row, as the chip at
            // the end and as the colour on net_pp. A third copy as a bar down the left edge
            // adds no information and is the look the operator has asked not to see.
            var strip = new Border();
            strip.Padding = new Thickness(0, 5, 0, 5);

            var row = new Grid();
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(3, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(80) });

            string toggle = string.IsNullOrEmpty(S(r, "toggle")) ? "?" : S(r, "toggle");
            string nVal   = r.ContainsKey("n") && r["n"] != null ? r["n"].ToString() : "?";
            string netVal = Pp(r.ContainsKey("net_pp") ? r["net_pp"] : null);
            string pVal   = Num(r.ContainsKey("p") ? r["p"] : null, "0.000");
            string vtext  = string.IsNullOrEmpty(S(r, "verdict")) ? "?" : S(r, "verdict");

            row.Children.Add(RowCell(toggle, Fg,   false, 0));
            row.Children.Add(RowCell(nVal,   Muted, false, 1));
            row.Children.Add(RowCell(netVal, new SolidColorBrush(sc), true, 2));
            row.Children.Add(RowCell(pVal,   Muted, false, 3));

            // verdict chip
            var chip = Pill(vtext, vk);
            chip.VerticalAlignment = VerticalAlignment.Center;
            chip.Margin = new Thickness(4, 0, 0, 0);
            Grid.SetColumn(chip, 4);
            row.Children.Add(chip);

            strip.Child = row;
            col.Children.Add(strip);
        }
        return card;
    }

    // ── (3) BURNED LEDGER ────────────────────────────────────────────────────────
    // Total count of burned problems + breakdown by reason (bar chart).
    UIElement BuildBurnedLedger(Dictionary<string, object> state)
    {
        var card = SectionCard("burned_sec", "burned_exp");
        var col  = (StackPanel)card.Child;
        var bl   = Obj(state, "burned_ledger");
        int total = I(bl, "total");

        // total count as a prominent metric
        var totalRow = new StackPanel(); totalRow.Orientation = Orientation.Horizontal;
        totalRow.Margin = new Thickness(0, 8, 0, 10);
        var tLabel = new TextBlock();
        tLabel.Text = T("burned_total") + ": ";
        tLabel.Foreground = Muted; tLabel.FontSize = 13;
        tLabel.VerticalAlignment = VerticalAlignment.Center;
        totalRow.Children.Add(tLabel);
        var tValue = new TextBlock();
        tValue.Text = total.ToString();
        tValue.Foreground = Fg; tValue.FontSize = 16; tValue.FontWeight = FontWeights.SemiBold;
        tValue.VerticalAlignment = VerticalAlignment.Center;
        totalRow.Children.Add(tValue);
        col.Children.Add(totalRow);

        var byReason = bl != null ? Obj(bl, "by_reason") : null;
        if (byReason == null || byReason.Count == 0)
        {
            col.Children.Add(MuteRow(T("none")));
            return card;
        }

        // sub-label
        var subLbl = new TextBlock();
        subLbl.Text = T("by_reason");
        subLbl.Foreground = Muted; subLbl.FontSize = 11;
        subLbl.FontWeight = FontWeights.SemiBold;
        subLbl.Margin = new Thickness(0, 0, 0, 4);
        col.Children.Add(subLbl);

        int max = 1;
        foreach (KeyValuePair<string, object> kv in byReason)
        { int v = 0; try { v = Convert.ToInt32(kv.Value); } catch (Exception) { } if (v > max) max = v; }

        foreach (KeyValuePair<string, object> kv in byReason)
        {
            int v = 0; try { v = Convert.ToInt32(kv.Value); } catch (Exception) { }
            col.Children.Add(BarRow(kv.Key, v, max, (double)v / max));
        }
        return card;
    }

    // ── (4) PASS@1 TREND ─────────────────────────────────────────────────────────
    // Timestamped bar list, newest at top, capped at 24 entries.
    UIElement BuildPassTrend(Dictionary<string, object> state)
    {
        var card = SectionCard("trend_sec", "trend_exp");
        var col  = (StackPanel)card.Child;
        object[] pt = Arr(state, "pass1_trend");

        if (pt == null || pt.Length == 0)
        {
            col.Children.Add(MuteRow(T("none")));
            return card;
        }

        int shown = 0;
        for (int i = pt.Length - 1; i >= 0 && shown < 24; i--, shown++)
        {
            var entry = pt[i] as Dictionary<string, object>;
            if (entry == null) continue;
            double pass = 0.0;
            try
            {
                if (entry.ContainsKey("pass_at_1") && entry["pass_at_1"] != null)
                    pass = Convert.ToDouble(entry["pass_at_1"]);
            }
            catch (Exception) { }
            if (pass < 0) pass = 0;
            if (pass > 1) pass = 1;
            string ts = entry.ContainsKey("ts") && entry["ts"] != null ? entry["ts"].ToString() : "?";

            // A SUPERSEDED POINT IS NOT A STEP IN A SERIES. The archive holds one genome
            // measured twice -- 0.34, then 0.50 after the first grade turned out to be a
            // host artifact -- and drawn plainly this series says the loop improved by 16
            // points. It did not; the same scaffold was measured again. Labelled and dimmed
            // rather than dropped, because the loop did produce that number and the record
            // of the correction is what makes it auditable.
            bool superseded = false;
            try
            {
                if (entry.ContainsKey("superseded") && entry["superseded"] != null)
                    superseded = Convert.ToBoolean(entry["superseded"]);
            }
            catch (Exception) { }

            // The recorded reason beats the generic label whenever there is one: "(replaced by
            // a re-measurement)" says a row was replaced, and the reader's next question is
            // always why.
            string note = entry.ContainsKey("note") && entry["note"] != null
                        ? Convert.ToString(entry["note"]) : "";
            string tag = note.Length > 0 ? note : T("superseded");
            var barRow = BarRow(superseded ? ts + "   " + tag : ts, -1, -1, pass,
                Num(entry.ContainsKey("pass_at_1") ? entry["pass_at_1"] : null, "0.000"));
            if (superseded)
            {
                var fe = barRow as FrameworkElement;
                if (fe != null) fe.Opacity = 0.45;
            }
            col.Children.Add(barRow);
        }
        return card;
    }

    // ── (5) ARCHIVE ──────────────────────────────────────────────────────────────
    // Genome count + QD cells.
    // Highest pass@1 across adopted genomes. The scorecard's "latest" answers a different
    // question -- what the last cycle scored -- and the two diverge exactly when a cycle
    // regresses, which is the case worth being able to see.
    string BestPass(Dictionary<string, object> arc)
    {
        object[] gs = arc != null ? Arr(arc, "genomes") : null;
        if (gs == null || gs.Length == 0) return "?";
        double best = double.MinValue;
        foreach (var o in gs)
        {
            var g = o as Dictionary<string, object>;
            if (g == null || !g.ContainsKey("pass_at_1") || g["pass_at_1"] == null) continue;
            try { double v = Convert.ToDouble(g["pass_at_1"]); if (v > best) best = v; }
            catch (Exception) { }
        }
        return best == double.MinValue ? "?" : best.ToString("0.###");
    }

    // THE OPERATOR'S OWN WORDS, TYPED BY THE OPERATOR. The ledger's contract is that an
    // authorisation is verbatim; a button that recorded "approved from the dashboard" would be
    // this window putting words in their mouth, which is the one thing the quote form exists to
    // prevent. MessageBox cannot take text, so this is the smallest window that can.
    // A CHOICE, NOT A COMPOSITION EXERCISE. The first version demanded typed words every
    // time, on the reasoning that an authorisation must be verbatim. The contract is narrower
    // than that: what it forbids is this window inventing a decision. A phrase the operator
    // picked is theirs -- it is shown, word for word, before the click -- and the record says
    // whether it was picked or written, so a reader knows the granularity rather than guessing.
    //
    // Typing stays, as one of the options, because "yes, but" is a real answer and a fixed
    // list cannot hold it. Demanding it for every routine yes is friction, and a decision
    // surface with friction is one that gets abandoned.
    //
    // Returns the chosen text and sets `kind`, or null if the operator closed it.
    string AskForDecision(string title, string[] choices, out string kind)
    {
        kind = "";
        var w = new Window();
        w.Title = title;
        w.Owner = this;
        w.WindowStartupLocation = WindowStartupLocation.CenterOwner;
        w.SizeToContent = SizeToContent.Height;
        w.Width = 580;
        w.ResizeMode = ResizeMode.NoResize;
        w.Background = Bg;

        var col = new StackPanel();
        col.Margin = new Thickness(20, 18, 20, 16);

        var note = new TextBlock();
        note.Text = T("pd_recorded");
        note.Foreground = Muted; note.FontSize = Theme.FsMeta;
        note.TextWrapping = TextWrapping.Wrap;
        col.Children.Add(note);

        var picked = new string[2];       // [0] text, [1] kind

        foreach (string choice in choices)
        {
            var b = new Button();
            b.Content = choice;
            b.HorizontalContentAlignment = HorizontalAlignment.Left;
            b.Padding = new Thickness(12, 8, 12, 8);
            b.Margin = new Thickness(0, 10, 0, 0);
            string theChoice = choice;
            b.Click += delegate
            {
                picked[0] = theChoice; picked[1] = "preset"; w.Close();
            };
            col.Children.Add(b);
        }

        var own = new TextBox();
        own.AcceptsReturn = true;
        own.TextWrapping = TextWrapping.Wrap;
        own.MinHeight = 56;
        own.Margin = new Thickness(0, 14, 0, 0);
        own.FontSize = Theme.FsMeta;
        col.Children.Add(own);

        var row = new StackPanel();
        row.Orientation = Orientation.Horizontal;
        row.HorizontalAlignment = HorizontalAlignment.Right;
        row.Margin = new Thickness(0, 10, 0, 0);
        var cancel = new Button();
        cancel.Content = T("pd_cancel");
        cancel.Padding = new Thickness(14, 5, 14, 5);
        cancel.Margin = new Thickness(0, 0, 8, 0);
        var ok = new Button();
        ok.Content = T("pd_own");
        ok.Padding = new Thickness(18, 5, 18, 5);
        row.Children.Add(cancel); row.Children.Add(ok);
        col.Children.Add(row);

        ok.Click += delegate
        {
            if (own.Text.Trim().Length == 0)
            {
                MessageBox.Show(w, T("pd_need_words"), title,
                                MessageBoxButton.OK, MessageBoxImage.Warning);
                return;
            }
            picked[0] = own.Text.Trim(); picked[1] = "typed"; w.Close();
        };
        cancel.Click += delegate { picked[0] = null; w.Close(); };

        w.Content = col;
        w.ShowDialog();
        kind = picked[1] ?? "";
        return picked[0];
    }

    // Recording a decision goes through the same module the queue is written by, so there is
    // one implementation of the on-disk shape. Waited for and reported: a decision that failed
    // to record silently is worse than no button, because the operator believes they answered.
    bool RecordDecision(string pid, string verb, string words, string kind)
    {
        try
        {
            string root   = RepoRoot();
            string venvPy = Path.Combine(root, ".venv", "Scripts", "python.exe");
            var psi = new ProcessStartInfo();
            psi.FileName  = File.Exists(venvPy) ? venvPy : "python";
            psi.Arguments = "-m relay.selfimprove.pending " + verb + " " + pid
                          + " --authorization \"" + (words ?? "").Replace("\"", "'") + "\""
                          + " --kind " + (string.IsNullOrEmpty(kind) ? "typed" : kind);
            psi.WorkingDirectory       = root;
            psi.UseShellExecute        = false;
            psi.CreateNoWindow         = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError  = true;
            var proc = new Process(); proc.StartInfo = psi; proc.Start();
            string so = proc.StandardOutput.ReadToEnd();
            string se = proc.StandardError.ReadToEnd();
            proc.WaitForExit(30000);
            if (proc.ExitCode != 0)
            {
                MessageBox.Show(this, T("pd_failed") + " (exit " + proc.ExitCode + ")\n\n"
                                + so + "\n" + se, T("pending_sec"),
                                MessageBoxButton.OK, MessageBoxImage.Error);
                return false;
            }
            return true;
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, T("pd_failed") + "\n\n" + ex.Message, T("pending_sec"),
                            MessageBoxButton.OK, MessageBoxImage.Error);
            return false;
        }
    }

    // AWAITING A DECISION. Shown first, and only when there is something -- an empty call to
    // action is noise, and a queue nobody sees is the reminder it was built to replace.
    //
    // This screen makes no claim to enforce anything. The queue runs in the same privilege
    // domain as the agent that fills it; what it buys is that a refused proposal survives the
    // turn and can be found without anyone remembering it exists.
    UIElement BuildPending(Dictionary<string, object> state)
    {
        object[] rows = Arr(state, "pending_decisions");
        if (rows == null || rows.Length == 0) return null;

        var card = SectionCard("pending_sec", "pending_exp");
        var col  = (StackPanel)card.Child;

        var head = new WrapPanel(); head.Margin = new Thickness(0, 10, 0, 0);
        head.Children.Add(Pill(rows.Length.ToString() + (_lang == 0 ? " 件" : " open"), "warn"));
        col.Children.Add(head);

        double now = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;
        foreach (var o in rows)
        {
            var r = o as Dictionary<string, object>;
            if (r == null) continue;

            var body = new StackPanel();

            var g = new Grid();
            var ca = new ColumnDefinition(); ca.Width = new GridLength(1, GridUnitType.Star);
            var cb = new ColumnDefinition(); cb.Width = GridLength.Auto;
            g.ColumnDefinitions.Add(ca); g.ColumnDefinitions.Add(cb);

            object files; r.TryGetValue("files", out files);
            var flist = new List<string>();
            var farr = files as object[];
            if (farr != null) foreach (var f in farr) flist.Add(Convert.ToString(f));
            string st = S(r, "status");
            string head0 = string.Join(", ", flist.ToArray());
            if (st == "approved") head0 = head0 + "   —   " + T("pd_approved")
                                        + " · " + T("pd_waiting");
            var filesTb = ClipLine(head0, Fg, Theme.FsMeta, true);
            filesTb.FontWeight = FontWeights.SemiBold;
            Grid.SetColumn(filesTb, 0); g.Children.Add(filesTb);

            object ts; r.TryGetValue("ts", out ts);
            if (ts != null)
            {
                var when = new TextBlock();
                when.Text = AgoText(Convert.ToDouble(ts), now);
                when.Foreground = Theme.Br(Theme.Faint(_dark));
                when.FontSize = 11;
                when.VerticalAlignment = VerticalAlignment.Center;
                when.Margin = new Thickness(12, 0, 0, 0);
                Grid.SetColumn(when, 1); g.Children.Add(when);
            }
            body.Children.Add(g);

            var reason = new TextBlock();
            reason.Text = S(r, "reason");
            reason.Foreground = Fg; reason.FontSize = Theme.FsMeta;
            reason.TextWrapping = TextWrapping.Wrap;
            reason.Margin = new Thickness(0, 4, 0, 0);
            body.Children.Add(reason);

            string detail = S(r, "detail");
            if (detail.Length > 0)
            {
                var det = new TextBlock();
                det.Text = detail;
                det.Foreground = Muted; det.FontSize = Theme.FsMeta;
                det.TextWrapping = TextWrapping.Wrap;
                det.Margin = new Thickness(0, 6, 0, 0);
                body.Children.Add(det);
            }

            string cmd = S(r, "command");
            string status = S(r, "status");
            string words = S(r, "authorization");
            string pid = S(r, "id");

            if (cmd.Length > 0)
            {
                // Once approved the placeholder is gone: the command carries the operator's
                // own words, so it can be run as it stands.
                string filled = words.Length > 0
                    ? cmd.Replace("<\u3042\u306a\u305f\u306e\u8a00\u8449>", words)
                         .Replace("<your words>", words)
                    : cmd;
                var cmdTb = ClipLine(filled, Theme.Br(Theme.Faint(_dark)), Theme.FsLog, true);
                cmdTb.Margin = new Thickness(0, 8, 0, 0);
                body.Children.Add(cmdTb);
            }

            if (status == "approved")
            {
                // The answer, kept in the form this file reserves for verbatim material, and
                // the entry stays on screen until the work is done. Removing it at the moment
                // of approval is what made approving feel identical to being ignored.
                var q = new Border();
                q.Background = QuoteBg;
                q.CornerRadius = new CornerRadius(Theme.RadSmall);
                q.Padding = new Thickness(8, 4, 8, 4);
                q.Margin = new Thickness(0, 8, 0, 0);
                q.HorizontalAlignment = HorizontalAlignment.Left;
                var qt = new TextBlock();
                qt.Text = "\u201c" + words + "\u201d";
                qt.Foreground = Muted; qt.FontSize = Theme.FsMeta;
                qt.TextWrapping = TextWrapping.Wrap; qt.MaxWidth = 620;
                q.Child = qt;
                body.Children.Add(q);
            }

            // -- the controls. A card that only offers "copy" states that a decision is
            //    waiting without offering anywhere to make it, which is the same shape as the
            //    notification that opened a text file of commands to paste.
            var actions = new StackPanel();
            actions.Orientation = Orientation.Horizontal;
            actions.Margin = new Thickness(0, 10, 0, 0);

            if (status != "approved")
            {
                var yes = new Button();
                yes.Content = T("pd_approve");
                yes.Padding = new Thickness(16, 5, 16, 5);
                yes.Margin  = new Thickness(0, 0, 8, 0);
                string theId = pid;
                yes.Click += delegate
                {
                    string kind;
                    string said = AskForDecision(T("pd_ask_t"),
                                                 new string[] { T("pd_a1"), T("pd_a2") },
                                                 out kind);
                    if (said == null) return;                    // closed, nothing recorded
                    if (RecordDecision(theId, "--approve", said, kind)) ForceRender();
                };
                actions.Children.Add(yes);

                var no = new Button();
                no.Content = T("pd_reject");
                no.Padding = new Thickness(16, 5, 16, 5);
                no.Margin  = new Thickness(0, 0, 8, 0);
                string theId2 = pid;
                no.Click += delegate
                {
                    string kind;
                    string said = AskForDecision(T("pd_reject_t"),
                                                 new string[] { T("pd_r1"), T("pd_r2") },
                                                 out kind);
                    if (said == null) return;
                    if (RecordDecision(theId2, "--drop", said, kind)) ForceRender();
                };
                actions.Children.Add(no);
            }

            if (cmd.Length > 0)
            {
                var copy = new Button();
                copy.Content = T("pending_copy");
                copy.Padding = new Thickness(12, 5, 12, 5);
                string theCmd = words.Length > 0
                    ? cmd.Replace("<\u3042\u306a\u305f\u306e\u8a00\u8449>", words)
                         .Replace("<your words>", words)
                    : cmd;
                var theBtn = copy;
                copy.Click += delegate
                {
                    try { Clipboard.SetText(theCmd); theBtn.Content = T("pending_copied"); }
                    catch (Exception) { }
                };
                actions.Children.Add(copy);
            }
            body.Children.Add(actions);

            var rec = new Border();
            rec.BorderThickness = new Thickness(0, 0, 0, 1);
            rec.BorderBrush = new SolidColorBrush(Mix(Theme.Col(Theme.Faint(_dark)), CardColor(), 0.22));
            rec.Padding = new Thickness(0, 0, 0, 12);
            rec.Margin  = new Thickness(0, 12, 0, 0);
            rec.Child = body;
            col.Children.Add(rec);
        }
        return card;
    }

    UIElement BuildArchive(Dictionary<string, object> state)
    {
        var card = SectionCard("archive_sec", "archive_exp");
        var col  = (StackPanel)card.Child;
        var arc  = Obj(state, "archive");
        var sum  = Obj(state, "summary");

        var grid = new Grid(); grid.Margin = new Thickness(0, 10, 0, 0);
        for (int c = 0; c < 3; c++)
        {
            var cd = new ColumnDefinition(); cd.Width = new GridLength(1, GridUnitType.Star);
            grid.ColumnDefinitions.Add(cd);
        }
        grid.Children.Add(MetricCell(T("genomes"),  I(arc, "count").ToString(),    Fg, 0));
        grid.Children.Add(MetricCell(T("qd_cells"), I(arc, "qd_cells").ToString(), Fg, 1));
        grid.Children.Add(MetricCell(T("g_best"), BestPass(arc), Fg, 2));
        col.Children.Add(grid);

        // THE GENOMES THEMSELVES. The feed has carried id, parent, pass@1, the gate verdict
        // and the behavioural descriptors for every adopted genome all along; the card showed
        // two integers derived from that list and threw the list away. Nothing here needed new
        // data -- only for the screen to stop discarding what it was already given.
        object[] gs = arc != null ? Arr(arc, "genomes") : null;
        if (gs == null || gs.Length == 0) { col.Children.Add(MuteRow(T("none"))); return card; }

        var table = new Grid(); table.Margin = new Thickness(0, 14, 0, 0);
        double[] w = { 1.1, 0.7, 1.0, 2.0, 1.0 };
        for (int c = 0; c < w.Length; c++)
        {
            var cd = new ColumnDefinition(); cd.Width = new GridLength(w[c], GridUnitType.Star);
            table.ColumnDefinitions.Add(cd);
        }
        var hr = new RowDefinition(); hr.Height = GridLength.Auto;
        table.RowDefinitions.Add(hr);
        string[] heads = { T("g_id"), T("g_pass"), T("g_gate"), T("g_desc"), T("g_parent") };
        for (int c = 0; c < heads.Length; c++)
        {
            var h = ColHeader(heads[c], c);
            Grid.SetRow((UIElement)h, 0);
            table.Children.Add((UIElement)h);
        }

        int shownG = 0;
        for (int i = gs.Length - 1; i >= 0 && shownG < 12; i--)
        {
            var g = gs[i] as Dictionary<string, object>;
            if (g == null) continue;
            var rd = new RowDefinition(); rd.Height = GridLength.Auto;
            table.RowDefinitions.Add(rd);
            int row = table.RowDefinitions.Count - 1;

            string gid    = S(g, "id");
            string gate   = S(g, "gate_verdict");
            object parent = g.ContainsKey("parent_id") ? g["parent_id"] : null;
            string ptxt   = parent == null ? T("g_root") : Convert.ToString(parent);

            var desc = Obj(g, "descriptors");
            var dparts = new List<string>();
            if (desc != null)
                foreach (var kv in desc)
                    dparts.Add(Convert.ToString(kv.Value));
            string dtxt = string.Join("  \u00b7  ", dparts.ToArray());

            int nMeas = 1;
            try
            {
                if (g.ContainsKey("measurements") && g["measurements"] != null)
                    nMeas = Convert.ToInt32(g["measurements"]);
            }
            catch (Exception) { }

            // Deduplicating the rows must not erase the fact that there were several. The id
            // is a content hash, so a repeat means the same scaffold was measured again --
            // which is exactly what the reader needs to know before reading the number.
            var idTb = RowCell(gid + (nMeas > 1 ? "  ×" + nMeas : ""), Fg, true, 0) as TextBlock;
            if (idTb != null)
            {
                idTb.FontFamily = new FontFamily(Theme.CodeFont);
                if (nMeas > 1) idTb.ToolTip = T("measured_n") + ": " + nMeas;
            }
            var cells = new UIElement[] {
                (UIElement)idTb,
                (UIElement)RowCell(Num(g.ContainsKey("pass_at_1") ? g["pass_at_1"] : null, "0.###"),
                                   Fg, true, 1),
                (UIElement)RowCell(gate, Muted, false, 2),
                (UIElement)RowCell(dtxt, Muted, false, 3),
                (UIElement)RowCell(ptxt, Theme.Br(Theme.Faint(_dark)), false, 4)
            };
            for (int c = 0; c < cells.Length; c++)
            {
                var fe = cells[c] as FrameworkElement;
                if (fe != null) fe.Margin = new Thickness(c == 0 ? 0 : 4, 6, 0, 0);
                Grid.SetRow(cells[c], row);
                table.Children.Add(cells[c]);
            }
            shownG++;
        }
        col.Children.Add(table);
        if (gs.Length > shownG)
            col.Children.Add(MuteRow("+" + (gs.Length - shownG) + (_lang == 0 ? " 件" : " more")));
        return card;
    }

    // ── widget helpers ────────────────────────────────────────────────────────────

    // Section card: returns a themed Border whose .Child is a StackPanel with the
    // section label + one-line explanation already prepended; callers append content rows.
    // titleKey and expKey are T() keys (not raw strings) so the card re-renders on lang toggle.
    Border SectionCard(string titleKey, string expKey)
    {
        var card = new Border();
        card.BorderThickness = new Thickness(1);
        card.CornerRadius    = new CornerRadius(10);
        card.Padding         = new Thickness(18, 14, 16, 16);
        card.Margin          = new Thickness(0, 0, 0, 12);
        card.BorderBrush     = Border;
        card.Background      = CardBg;

        var col = new StackPanel();

        // section label (small, semibold, caps)
        col.Children.Add(SectionLabel(T(titleKey)));

        // one-line explanation (muted, wraps)
        col.Children.Add(SectionExplanation(T(expKey)));

        card.Child = col;
        return card;
    }

    TextBlock SectionLabel(string text)
    {
        var t = new TextBlock();
        t.Text = text.ToUpper();
        t.Foreground  = Accent;
        t.FontSize    = 11;
        t.FontWeight  = FontWeights.SemiBold;
        t.Margin      = new Thickness(0, 0, 0, 0);
        return t;
    }

    TextBlock SectionExplanation(string text)
    {
        var t = new TextBlock();
        t.Text = text;
        t.Foreground   = Muted;
        t.FontSize     = 11.5;
        t.TextWrapping = TextWrapping.Wrap;
        t.Margin       = new Thickness(0, 3, 0, 0);
        return t;
    }

    // Metric cell: label above, value below, placed in a Grid column.
    UIElement MetricCell(string label, string value, Brush valueBrush, int colIdx)
    {
        var sp = new StackPanel();
        sp.Margin = new Thickness(0, 0, 12, 0);
        var l = new TextBlock();
        l.Text = label; l.Foreground = Muted; l.FontSize = 11;
        l.TextTrimming = TextTrimming.CharacterEllipsis;
        sp.Children.Add(l);
        var v = new TextBlock();
        v.Text = value; v.Foreground = valueBrush; v.FontSize = 15;
        v.FontWeight = FontWeights.SemiBold;
        v.TextWrapping = TextWrapping.Wrap;
        v.Margin = new Thickness(0, 3, 0, 0);
        sp.Children.Add(v);
        Grid.SetColumn(sp, colIdx);
        return sp;
    }

    // Plain muted row used for "none" placeholders.
    TextBlock MuteRow(string text)
    {
        var t = new TextBlock();
        t.Text = text; t.Foreground = Muted; t.FontSize = 12.5;
        t.Margin = new Thickness(0, 8, 0, 0);
        return t;
    }

    // Table column header (small, muted, semibold).
    UIElement ColHeader(string text, int col)
    {
        var t = new TextBlock();
        t.Text = text; t.Foreground = Muted; t.FontSize = 11;
        t.FontWeight = FontWeights.SemiBold;
        t.VerticalAlignment = VerticalAlignment.Center;
        t.Margin = new Thickness(col == 0 ? 0 : 4, 0, 0, 0);
        Grid.SetColumn(t, col);
        return t;
    }

    // Table data cell.
    UIElement RowCell(string text, Brush brush, bool semibold, int col)
    {
        var t = new TextBlock();
        t.Text = text; t.Foreground = brush; t.FontSize = 12.5;
        if (semibold) t.FontWeight = FontWeights.SemiBold;
        t.VerticalAlignment = VerticalAlignment.Center;
        t.TextTrimming = TextTrimming.CharacterEllipsis;
        t.Margin = new Thickness(col == 0 ? 0 : 4, 0, 0, 0);
        Grid.SetColumn(t, col);
        return t;
    }

    // Thin horizontal rule divider.
    UIElement HRule()
    {
        var b = new Border();
        b.Height = 1; b.Background = Border;
        b.Margin = new Thickness(0, 2, 0, 4);
        return b;
    }

    // Bar row: label | proportional bar | value. Used for burned ledger (counts) and pass@1 trend (ratio).
    UIElement BarRow(string label, int v, int max, double frac) { return BarRow(label, v, max, frac, null); }
    UIElement BarRow(string label, int v, int max, double frac, string valueText)
    {
        if (frac < 0) frac = 0;
        if (frac > 1) frac = 1;

        var grid = new Grid(); grid.Margin = new Thickness(0, 3, 0, 3);
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(160) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(64) });

        var lbl = new TextBlock();
        lbl.Text = label; lbl.Foreground = Muted; lbl.FontSize = 12;
        lbl.VerticalAlignment = VerticalAlignment.Center;
        lbl.TextTrimming = TextTrimming.CharacterEllipsis;
        Grid.SetColumn(lbl, 0); grid.Children.Add(lbl);

        // bar: a two-star Grid inside a pill-shaped track
        var track = new Border();
        track.Height = 8; track.CornerRadius = new CornerRadius(4);   // = height/2. 999 renders as a lens, not a bar.
        track.Background = QuoteBg;
        track.VerticalAlignment = VerticalAlignment.Center;
        track.Margin = new Thickness(0, 0, 10, 0);
        var bargrid = new Grid();
        bargrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(frac,       GridUnitType.Star) });
        bargrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1.0 - frac, GridUnitType.Star) });
        var fill = new Border();
        fill.Height = 8; fill.CornerRadius = new CornerRadius(4); fill.Background = Accent;
        Grid.SetColumn(fill, 0); bargrid.Children.Add(fill);
        track.Child = bargrid;
        Grid.SetColumn(track, 1); grid.Children.Add(track);

        var val = new TextBlock();
        val.Text = valueText != null ? valueText : v.ToString();
        val.Foreground = Fg; val.FontSize = 12; val.FontWeight = FontWeights.SemiBold;
        val.VerticalAlignment = VerticalAlignment.Center;
        val.TextAlignment = TextAlignment.Right;
        Grid.SetColumn(val, 2); grid.Children.Add(val);

        return grid;
    }

    // Status chip. Theme.cs states the design target in as many words -- status is "a thin
    // left rail + small chip (never a full-card fill)" -- and this was a saturated full fill
    // at radius 999, which is pixel-identical to the cockpit's RUNNING indicator. Three of
    // them in a row over a static integrity check therefore read as three things spinning.
    // Shape carries meaning whether or not that was intended.
    //
    // Now: soft tint, small radius, the status colour carried by the TEXT. The tint is mixed
    // against the card, so the chip never becomes a dark ground and the white-on-dark rule
    // has nothing to bind to. "muted" spends no colour at all -- a normal state should not
    // cost a chip, and if everything is normal the caller should not be building one.
    Border Pill(string text, string ck)
    {
        var b = new Border();
        b.CornerRadius = new CornerRadius(Theme.RadSmall);
        b.Padding      = new Thickness(8, 2, 8, 2);
        b.Margin       = new Thickness(0, 0, 6, 0);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = string.IsNullOrEmpty(text) ? "?" : text;
        t.FontSize = Theme.FsChip; t.FontWeight = FontWeights.SemiBold;
        if (ck == "good" || ck == "warn" || ck == "bad")
        {
            Color c = StatusColorFor(ck, _dark);
            b.Background      = new SolidColorBrush(Mix(c, CardColor(), 0.14));
            b.BorderBrush     = new SolidColorBrush(Mix(c, CardColor(), 0.45));
            b.BorderThickness = new Thickness(1);
            t.Foreground      = new SolidColorBrush(c);
        }
        else
        {
            t.Foreground = Muted;
            b.Padding    = new Thickness(0, 2, 8, 2);
        }
        b.Child = t;
        return b;
    }

    // "3日前" / "3 d ago" from a unix timestamp. The ledger has carried a ts on every record
    // from the beginning and the history displayed none of it: a list of acts with no times
    // is not a history, and it is the single largest reason the section could not be read.
    string AgoText(double ts, double now)
    {
        bool ja = _lang == 0;
        double sec = now - ts;
        if (sec < 90) return ja ? "たった今" : "just now";
        if (sec < 3600) return ((int)(sec / 60)).ToString() + (ja ? "分前" : " min ago");
        if (sec < 86400) return ((int)(sec / 3600)).ToString() + (ja ? "時間前" : " hr ago");
        return ((int)(sec / 86400)).ToString() + (ja ? "日前" : " d ago");
    }

    // A DERIVED SUMMARY, NOT THE RECORD. The reason a record carries is prose the agent typed
    // in whichever language it was working in, so toggling this window to English left those
    // lines in Japanese -- they are not interface text. Shortening and translating one needs a
    // model, and that is the only thing on this screen a model does.
    //
    // Read from a cache keyed by the record's own hash, so a summary cannot attach to a
    // different record and an edited record simply misses. Missing means the raw reason, which
    // is exactly what this window showed before summaries existed: every failure lands there.
    Dictionary<string, object> LoadSummaries()
    {
        try
        {
            string p = Path.Combine(RepoRoot(), ".fleet", "selfimprove", "record_summaries.json");
            if (!File.Exists(p)) return null;
            return _js.DeserializeObject(File.ReadAllText(p, Encoding.UTF8))
                   as Dictionary<string, object>;
        }
        catch (Exception) { return null; }
    }

    string SummaryFor(Dictionary<string, object> row, Dictionary<string, object> cache)
    {
        try
        {
            if (cache == null || row == null) return "";
            object h; row.TryGetValue("hash", out h);
            string key = Convert.ToString(h);
            if (key.Length == 0) return "";
            object e; cache.TryGetValue(key, out e);
            var entry = e as Dictionary<string, object>;
            if (entry == null) return "";
            object v; entry.TryGetValue(_lang == 0 ? "ja" : "en", out v);
            return Convert.ToString(v);
        }
        catch (Exception) { return ""; }
    }

    // THE HEADLINE IS DERIVED, NOT TYPED. The first line used to be the actor -- a module
    // path like "relay.selfimprove.frozen CLI", which takes two values across the whole ledger
    // and tells a reader nothing -- and the line that actually read as the title was the raw
    // --reason string the agent had typed. A record whose headline is its own input text
    // cannot be scanned.
    //
    // Every part of this is computable from the record, so none of it is generated: the event
    // maps to a fixed verb, the target is the basenames of `changed`. Substituting a language
    // model for a lookup table is the same design fault this system has committed before.
    string EventVerb(string kind)
    {
        if (kind == "rebless")           return T("ev_rebless");
        if (kind == "baseline_mismatch") return T("ev_mismatch");
        if (kind == "rebless_revoke")    return T("ev_revoke");
        if (kind == "genome_apply")      return T("ev_apply");
        if (kind == "genome_revert")     return T("ev_revert");
        if (kind == "branch_create")     return T("ev_branch_new");
        if (kind == "branch_delete")     return T("ev_branch_del");
        return kind;
    }

    // "UNPINNED:" marks a file that was not yet in the baseline when the drift was detected.
    // It is a state marker on the same path, so a mismatch and the re-signing that closed it
    // disagree on the string while naming the same file. Compared raw, 2 of 21 pairs looked
    // like "detected something other than what was approved" -- which is a real anomaly and
    // must never be hidden, so the comparison has to be able to tell the two cases apart.
    static string StripPin(string path)
    {
        return path != null && path.StartsWith("UNPINNED:") ? path.Substring(9) : path;
    }

    static List<string> ChangedPaths(Dictionary<string, object> row)
    {
        var out_ = new List<string>();
        object changed; row.TryGetValue("changed", out changed);
        var files = changed as Dictionary<string, object>;
        if (files != null) foreach (var k in files.Keys) out_.Add(StripPin(k));
        out_.Sort();
        return out_;
    }

    static string BaseNames(List<string> paths)
    {
        var seen = new List<string>();
        foreach (var p in paths)
        {
            string b = p;
            int i = b.LastIndexOfAny(new char[] { '/', '\\' });
            if (i >= 0) b = b.Substring(i + 1);
            if (b.Length > 0 && !seen.Contains(b)) seen.Add(b);
        }
        return string.Join(", ", seen.ToArray());
    }

    // A record written by a test run: everything it touched is under a temp directory. 20 of
    // the 78 records in the live ledger are these -- test genome applies and reverts that were
    // written to the operator's real ledger rather than a redirected one. They are real rows
    // and stay in the file; they are simply not events in this repository's life.
    static bool IsTestRecord(Dictionary<string, object> row)
    {
        var paths = new List<string>();
        object changed; row.TryGetValue("changed", out changed);
        var files = changed as Dictionary<string, object>;
        if (files == null || files.Count == 0) return false;
        foreach (var k in files.Keys)
        {
            string p = StripPin(k);
            bool temp = p.IndexOf("Temp", StringComparison.OrdinalIgnoreCase) >= 0
                     || p.IndexOf("tmp", StringComparison.OrdinalIgnoreCase) >= 0;
            if (!temp) return false;
        }
        return true;
    }

    static bool SameTargets(Dictionary<string, object> a, Dictionary<string, object> b)
    {
        var pa = ChangedPaths(a); var pb = ChangedPaths(b);
        if (pa.Count != pb.Count) return false;
        for (int i = 0; i < pa.Count; i++) if (pa[i] != pb[i]) return false;
        return true;
    }

    string GapText(double a, double b)
    {
        double sec = Math.Abs(b - a);
        bool ja = _lang == 0;
        if (sec < 90) return ja ? "すぐ" : "moments";
        if (sec < 3600) return ((int)(sec / 60)).ToString() + (ja ? "分" : " min");
        if (sec < 86400) return ((int)(sec / 3600)).ToString() + (ja ? "時間" : " hr");
        return ((int)(sec / 86400)).ToString() + (ja ? "日" : " d");
    }

    // The colour a record spends, spent on the event NAME rather than on a strip beside it.
    //
    // NO COLOURED LEFT RAILS. This was written with one, on the strength of a line in Theme.cs
    // recommending them, and the operator's response was that they have said repeatedly they
    // dislike the look -- a thick coloured bar down the left edge reads as a sticky note. The
    // line in Theme.cs has been corrected too, because a stale recommendation will simply be
    // followed again by whoever reads it next.
    //
    // Re-signing is the routine event here (24 in a week), so it stays neutral; the anomalies
    // are what get colour, which is the same rule the header follows.
    Brush KindBrush(string kind)
    {
        if (kind == "baseline_mismatch" || kind == "revoke")
            return new SolidColorBrush(StatusColorFor("bad", _dark));
        return Fg;
    }

    static void SetClip(TextBlock t, bool open)
    {
        if (t == null) return;
        t.TextWrapping = open ? TextWrapping.Wrap : TextWrapping.NoWrap;
        t.TextTrimming = open ? TextTrimming.None : TextTrimming.CharacterEllipsis;
    }

    // Long single lines, clipped rather than wrapped: eight records that each wrap to four
    // lines is the same unreadable wall in a different shape. The full text is on the tooltip
    // and one click on the record opens every clipped line in it.
    TextBlock ClipLine(string text, Brush fg, double size, bool mono)
    {
        var t = new TextBlock();
        t.Text = text; t.Foreground = fg; t.FontSize = size;
        if (mono) t.FontFamily = new FontFamily(Theme.CodeFont);
        t.TextWrapping = TextWrapping.NoWrap;
        t.TextTrimming = TextTrimming.CharacterEllipsis;
        if (!string.IsNullOrEmpty(text)) t.ToolTip = text;
        return t;
    }
}
