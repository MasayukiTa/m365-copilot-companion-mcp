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


partial class SelfImproveDashboardWindow : Window
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
    JavaScriptSerializer _js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
    double _upm = 960;
    Dictionary<string, string> _glyphs = new Dictionary<string, string>();

    ContentControl _iconHost;
    TextBlock _header, _sub, _freshnessLine;
    Button _themeBtn, _langBtn, _refreshBtn;
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
    //
    // STATIC ON PURPOSE. The cockpit publishes the always-visible health strip and has no
    // dashboard window to ask; before this, the only place the frozen set was ever compared
    // was inside a card at the bottom of a window somebody had to open. A second
    // implementation over there would be a second thing to keep in step, and the two
    // disagreeing is worse than neither existing. The only instance member it used was a
    // stateless serializer.
    internal static bool FrozenMatches(out int checkedCount, out List<string> differing,
                              out bool anchorOk)
    {
        checkedCount = 0; differing = new List<string>(); anchorOk = false;
        try
        {
            string root = RepoRoot();
            string bp = Path.Combine(root, "relay", "selfimprove", "frozen_baseline.json");
            if (!File.Exists(bp)) { differing.Add("NO_BASELINE"); return false; }
            var doc = (Dictionary<string, object>)(new JavaScriptSerializer { MaxJsonLength = int.MaxValue }
                .DeserializeObject(File.ReadAllText(bp, Encoding.UTF8)));
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

    // SelfImproveInUse() / FrozenGate used to live here, feeding the cockpit health strip's
    // 7th dot (added 8ce5c47, 2026-09-19; removed after owner feedback on 12b06fd,
    // 2026-09-24 -- the label did not fit the strip and the strip was the wrong place for it:
    // the self-improvement loop already refuses to run on drift, and this dashboard already
    // shows the same fact below with its own re-sign button). Removed with the dot; nothing
    // else called them.

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
        var head = new WrapPanel(); head.Margin = new Thickness(0, 12, 0, 0);

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

        // -- AND SOMEWHERE TO ACT, right under the chip that says something is wrong.
        //
        //    The pending card learned this once already, in its own comment: "a card that only
        //    offers copy states that a decision is waiting without offering anywhere to make
        //    it". This card said the frozen set was adrift and offered nothing at all, so every
        //    re-signing went through a terminal -- and an approval made in a terminal is not on
        //    the screen that exists to hold approvals. That is how the operator came to be
        //    looking for this and not finding it.
        //
        //    IT IS NOT A SHORTCUT. It runs the same CLI with the same flags, so a re-signing
        //    that reaches the files the standing delegation excludes is refused here exactly as
        //    it is from a shell, and the refusal is shown rather than swallowed.
        //
        //    Rendered only when the set differs: there is nothing to press in the normal state,
        //    and a button that is always there is one more thing to read past.
        if (!intact)
        {
            var why = MuteRow(T("auth_resign_why"));
            why.Margin = new Thickness(0, 12, 0, 0);
            col.Children.Add(why);

            string files  = string.Join(", ", differing.ToArray());
            string theCmd = "python -m relay.selfimprove.frozen --snapshot --force"
                          + " --reason \"" + files.Replace("\"", "'") + "\""
                          + " --authorization \"" + (_lang == 0 ? "<あなたの言葉>" : "<your words>") + "\"";

            var acts = new StackPanel();
            acts.Orientation = Orientation.Horizontal;
            acts.Margin = new Thickness(0, 8, 0, 0);

            var go = new Button();
            go.Content = T("auth_resign");
            go.Padding = new Thickness(16, 5, 16, 5);
            go.Margin  = new Thickness(0, 0, 8, 0);
            string theFiles = files;
            go.Click += delegate
            {
                string kind;
                string said = AskForDecision(T("auth_resign_t"),
                                             new string[] { T("auth_r1"), T("auth_r2") },
                                             out kind);
                if (said == null) return;              // closed, nothing done
                CarryOutFrozenResign("re-signed on the dashboard: " + theFiles, said);
                ForceRender();
            };
            acts.Children.Add(go);

            // The command stays as the second way in. Somebody working from a shell, or on a
            // machine where this window will not open, still needs the line.
            var cp = new Button();
            cp.Content = T("pending_copy");
            cp.Padding = new Thickness(12, 5, 12, 5);
            var cpRef = cp; string cmdRef = theCmd;
            cp.Click += delegate
            {
                try { Clipboard.SetText(cmdRef); cpRef.Content = T("pending_copied"); }
                catch (Exception) { }
            };
            acts.Children.Add(cp);
            col.Children.Add(acts);
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
                object a;
                rows[i].TryGetValue("event", out a);
                if (Convert.ToString(a) != "baseline_mismatch") continue;
                // THE NEXT RE-SIGNING, not the next ROW. Anything at all landing in between --
                // a genome apply, another detection -- pushed the pair apart and the mismatch
                // was then drawn in red as unresolved, which is the one state on this screen
                // that must never be claimed wrongly. One of the live ledger's 24 says exactly
                // that today.
                //
                // Still only the FIRST subsequent re-signing, and still only when the targets
                // agree. Scanning further would let a much later, unrelated re-signing swallow
                // a mismatch nobody ever answered.
                for (int j = i + 1; j < rows.Count; j++)
                {
                    object b; rows[j].TryGetValue("event", out b);
                    if (Convert.ToString(b) != "rebless") continue;
                    if (SameTargets(rows[i], rows[j])) closedBy[i] = j;
                    break;
                }
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
        btn.Margin  = new Thickness(0, 16, 0, 0);
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


    // THE OPERATOR'S OWN WORDS, TYPED BY THE OPERATOR. The ledger's contract is that an
    // authorisation is verbatim; a button that recorded "approved from the dashboard" would be
    // this window putting words in their mouth, which is the one thing the quote form exists to
    // prevent. MessageBox cannot take text, so this is the smallest window that can.
    // THE QUEUE ITSELF, NOT A SNAPSHOT OF IT. These entries used to come from
    // .fleet/selfimprove_dashboard.json, which only a separate `dashboard --write` refreshes.
    // So approving worked -- the queue recorded the operator's chosen phrase correctly -- and
    // the card did not move, because the window re-rendered the same stale file. The approval
    // was real and the screen said otherwise, which is worse than the button not working:
    // there is nothing to retry and nothing that looks wrong.
    //
    // Read straight from the append-only jsonl, replaying rows the way the module does. Same
    // reasoning as the frozen-set check above: this window computes what it shows rather than
    // believing a file some other process is responsible for keeping current.
    List<Dictionary<string, object>> ReadPendingQueue(bool wantOpen)
    {
        var open_ = new List<Dictionary<string, object>>();
        try
        {
            string p = Path.Combine(RepoRoot(), ".fleet", "selfimprove",
                                    "pending_decisions.jsonl");
            if (!File.Exists(p)) return open_;

            var byId = new Dictionary<string, Dictionary<string, object>>();
            var order = new List<string>();
            foreach (string line in File.ReadAllLines(p, Encoding.UTF8))
            {
                string t = line.Trim();
                if (t.Length == 0) continue;
                Dictionary<string, object> row;
                try { row = (Dictionary<string, object>)_js.DeserializeObject(t); }
                catch (Exception) { continue; }   // several processes append; a torn tail is normal
                if (row == null) continue;
                string id = S(row, "id");
                if (id.Length == 0) continue;
                string ev = S(row, "event");
                if (ev == "queued")
                {
                    if (byId.ContainsKey(id)) continue;
                    row["status"] = "open";
                    byId[id] = row; order.Add(id);
                }
                else if (ev == "resolved" && byId.ContainsKey(id))
                {
                    byId[id]["status"] = S(row, "status").Length > 0 ? S(row, "status") : "done";
                    byId[id]["authorization"] = S(row, "authorization");
                    byId[id]["authorization_kind"] = S(row, "authorization_kind");
                }
            }
            foreach (string id in order)
            {
                string st = S(byId[id], "status");
                if (wantOpen == (st == "open")) open_.Add(byId[id]);
            }
            if (!wantOpen) open_.Reverse();      // history reads newest first
        }
        catch (Exception) { }
        return open_;
    }

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
        col.Margin = new Thickness(20, 16, 20, 16);

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
            b.Margin = new Thickness(0, 12, 0, 0);
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
        own.Margin = new Thickness(0, 16, 0, 0);
        own.FontSize = Theme.FsMeta;
        col.Children.Add(own);

        var row = new StackPanel();
        row.Orientation = Orientation.Horizontal;
        row.HorizontalAlignment = HorizontalAlignment.Right;
        row.Margin = new Thickness(0, 12, 0, 0);
        var cancel = new Button();
        cancel.Content = T("pd_cancel");
        cancel.Padding = new Thickness(16, 5, 16, 5);
        cancel.Margin = new Thickness(0, 0, 8, 0);
        var ok = new Button();
        ok.Content = T("pd_own");
        ok.Padding = new Thickness(16, 5, 16, 5);
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
    // Carry out an approved re-signing, with the operator's own words as the authorization.
    //
    // WHY THE WORDS ARE PASSED STRAIGHT THROUGH: frozen.py requires the instruction that
    // specified this act, quoted verbatim, and this is the only moment the system holds it.
    // Re-typing it into a terminal later produces a ledger entry quoting a reconstruction.
    //
    // NOTHING IS RELAXED HERE. It runs the same CLI, so a re-signing that reaches files the
    // standing delegation excludes is refused exactly as it is from a shell -- and the refusal
    // is shown rather than swallowed, because an approval that silently did nothing is the
    // failure this whole change is about.
    // TAKES THE REASON, NOT AN ID. It was written for the pending card, where the id WAS
    // the reason ("decision <pid>"). The authority card has no card and no id -- the
    // frozen set is simply adrift -- and a helper that can only name a queue row is a
    // helper only that queue can use, which is why re-signing anything else still meant a
    // terminal.
    void CarryOutFrozenResign(string reasonText, string words)
    {
        try
        {
            string root   = RepoRoot();
            string venvPy = Path.Combine(root, ".venv", "Scripts", "python.exe");
            var psi = new ProcessStartInfo();
            psi.FileName  = File.Exists(venvPy) ? venvPy : "python";
            // --authorization - reads the words from stdin, for the same reason RecordDecision
            // does: quoting them into argv mangled quotes and broke on newlines, and these are
            // the words a ledger promises are unaltered.
            // The reason is quoted into argv, so a quote inside it would end the argument
            // early and hand the rest to the parser as flags. The WORDS never go this way --
            // they are read from stdin, unaltered, because the ledger promises that.
            psi.Arguments = "-m relay.selfimprove.frozen --snapshot --force"
                          + " --reason \"" + (reasonText ?? "").Replace("\"", "'") + "\""
                          + " --authorization -";
            psi.RedirectStandardInput  = true;
            psi.WorkingDirectory       = root;
            psi.UseShellExecute        = false;
            psi.CreateNoWindow         = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError  = true;
            var proc = new Process(); proc.StartInfo = psi; proc.Start();
            var wordBytes = new UTF8Encoding(false).GetBytes(words ?? "");
            proc.StandardInput.BaseStream.Write(wordBytes, 0, wordBytes.Length);
            proc.StandardInput.BaseStream.Flush();
            proc.StandardInput.Close();
            string so = proc.StandardOutput.ReadToEnd();
            string se = proc.StandardError.ReadToEnd();
            proc.WaitForExit(60000);
            if (proc.ExitCode != 0)
            {
                MessageBox.Show(this, T("pd_resign_failed") + "\n\n" + so + "\n" + se,
                                T("pending_sec"), MessageBoxButton.OK, MessageBoxImage.Warning);
            }
            else
            {
                MessageBox.Show(this, T("pd_resign_done") + "\n\n" + so.Trim(),
                                T("pending_sec"), MessageBoxButton.OK, MessageBoxImage.Information);
            }
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, T("pd_resign_failed") + "\n\n" + ex.Message,
                            T("pending_sec"), MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    bool RecordDecision(string pid, string verb, string words, string kind)
    {
        try
        {
            string root   = RepoRoot();
            string venvPy = Path.Combine(root, ".venv", "Scripts", "python.exe");
            var psi = new ProcessStartInfo();
            psi.FileName  = File.Exists(venvPy) ? venvPy : "python";
            // THE WORDS GO DOWN STDIN, NOT INTO ARGV. This replaced every double quote with
            // an apostrophe to survive the command line, so an authorisation containing one
            // was recorded as something the operator had not written -- while the dialog above
            // promises that nothing is summarised or reworded. A multi-line answer, or one
            // ending in a backslash, broke the quoting outright. Escaping harder would have
            // been another rule to get right; not quoting at all is one fewer.
            psi.Arguments = "-m relay.selfimprove.pending " + verb + " " + pid
                          + " --authorization -"
                          + " --kind " + (string.IsNullOrEmpty(kind) ? "typed" : kind);
            psi.RedirectStandardInput = true;
            psi.WorkingDirectory       = root;
            psi.UseShellExecute        = false;
            psi.CreateNoWindow         = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError  = true;
            var proc = new Process(); proc.StartInfo = psi; proc.Start();
            // BYTES, NOT THE STREAM'S DEFAULT ENCODING. This console is cp932; a
            // StreamWriter using it would mangle the operator's own words on the way to a
            // record whose whole promise is that they are unaltered. (StandardInputEncoding
            // does not exist on this framework, so the encoding is applied here instead.)
            var wordBytes = new UTF8Encoding(false).GetBytes(words ?? "");
            proc.StandardInput.BaseStream.Write(wordBytes, 0, wordBytes.Length);
            proc.StandardInput.BaseStream.Flush();
            proc.StandardInput.Close();          // the child reads to EOF
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
        // UNANSWERED ONLY. Answered proposals used to stay here so that approving did not look
        // like being ignored, which put decided items under a heading that says they are still
        // waiting -- the heading and its contents contradicting each other. They belong in a
        // history, which is what BuildDecisions below is.
        var rows = ReadPendingQueue(true);
        if (rows == null || rows.Count == 0) return null;

        var card = SectionCard("pending_sec", "pending_exp");
        var col  = (StackPanel)card.Child;

        var head = new WrapPanel(); head.Margin = new Thickness(0, 12, 0, 0);
        head.Children.Add(Pill(rows.Count.ToString() + (_lang == 0 ? " 件" : " open"), "warn"));
        col.Children.Add(head);

        double now = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;
        foreach (var r in rows)
        {
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
            var flist2 = files as System.Collections.ArrayList;
            if (flist.Count == 0 && flist2 != null)
                foreach (var f in flist2) flist.Add(Convert.ToString(f));
            // No approved decoration here any more: this section reads unanswered items
            // only, so that branch could not fire -- and a test was anchored on it,
            // passing on code nothing could reach. Answered ones live in BuildDecisions.
            var filesTb = ClipLine(string.Join(", ", flist.ToArray()), Fg,
                                   Theme.FsMeta, true);
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

            // -- the controls. A card that only offers "copy" states that a decision is
            //    waiting without offering anywhere to make it, which is the same shape as the
            //    notification that opened a text file of commands to paste.
            var actions = new StackPanel();
            actions.Orientation = Orientation.Horizontal;
            actions.Margin = new Thickness(0, 12, 0, 0);

            {
                var yes = new Button();
                yes.Content = T("pd_approve");
                yes.Padding = new Thickness(16, 5, 16, 5);
                yes.Margin  = new Thickness(0, 0, 8, 0);
                string theId = pid;
                // ROWS QUEUED BEFORE THE FIELD EXISTED STILL WORK. The operator already has one
                // sitting on this screen; if recognition depended solely on a field added
                // afterwards, that card would stay exactly as inert as it was before this
                // change -- which is the complaint, not the fix. The command is what the row
                // has always carried, so it is the second way in.
                string theAction = S(r, "action");
                if (theAction.Length == 0 && S(r, "command").IndexOf(
                        "frozen --snapshot --force", StringComparison.OrdinalIgnoreCase) >= 0)
                    theAction = "frozen_resign";
                yes.Click += delegate
                {
                    string kind;
                    string said = AskForDecision(T("pd_ask_t"),
                                                 new string[] { T("pd_a1"), T("pd_a2") },
                                                 out kind);
                    if (said == null) return;                    // closed, nothing recorded
                    if (!RecordDecision(theId, "--approve", said, kind)) return;
                    // AND THEN DO IT, when the proposal is one this screen can carry out.
                    // Approving a Skill IS the act; approving this used to leave a card
                    // reading "waiting on the agent" and a command to go and run elsewhere.
                    // The operator said as much: the signing flows are not operable the way
                    // the Skill approvals are. The words they just typed are the
                    // authorization, so the ledger quotes the person who decided rather than
                    // a second invocation nobody recorded.
                    if (theAction == "frozen_resign")
                        CarryOutFrozenResign("approved on the dashboard (decision "
                                             + theId + ")", said);
                    ForceRender();
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

    // WHAT WAS ASKED AND ANSWERED. Separate from the queue above because a heading that says
    // "awaiting a decision" must not hold decided items, and separate from the authority
    // ledger because that records acts, while these record answers -- including the answers
    // that stopped an act from happening.
    //
    // An approved entry stays here until the work is done, so an approval is visibly
    // outstanding rather than disappearing the moment it is given.
    UIElement BuildDecisions()
    {
        var rows = ReadPendingQueue(false);
        if (rows == null || rows.Count == 0) return null;

        var card = SectionCard("dh_sec", "dh_exp");
        var col  = (StackPanel)card.Child;
        double now = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;

        int shown = 0;
        foreach (var r in rows)
        {
            if (r == null || shown >= 8) break;
            string status = S(r, "status");
            string words  = S(r, "authorization");

            var body = new StackPanel();

            var g = new Grid();
            var c0 = new ColumnDefinition(); c0.Width = new GridLength(1, GridUnitType.Star);
            var c1 = new ColumnDefinition(); c1.Width = GridLength.Auto;
            g.ColumnDefinitions.Add(c0); g.ColumnDefinitions.Add(c1);

            string mark = status == "approved" ? T("pd_approved")
                        : (status == "dropped" ? T("dh_dropped") : T("dh_done"));
            string title = S(r, "reason");
            var titleTb = ClipLine(title, Fg, 13.0, false);
            titleTb.FontWeight = FontWeights.SemiBold;
            Grid.SetColumn(titleTb, 0); g.Children.Add(titleTb);

            var st = new TextBlock();
            st.Text = status == "approved" ? mark + " \u00b7 " + T("pd_waiting") : mark;
            st.Foreground = status == "dropped" ? Muted : Theme.Br(Theme.Faint(_dark));
            st.FontSize = 11;
            st.VerticalAlignment = VerticalAlignment.Center;
            st.Margin = new Thickness(12, 0, 0, 0);
            Grid.SetColumn(st, 1); g.Children.Add(st);
            body.Children.Add(g);

            object files; r.TryGetValue("files", out files);
            var flist = new List<string>();
            var farr = files as object[];
            if (farr != null) foreach (var f in farr) flist.Add(Convert.ToString(f));
            var alist = files as System.Collections.ArrayList;
            if (flist.Count == 0 && alist != null)
                foreach (var f in alist) flist.Add(Convert.ToString(f));
            if (flist.Count > 0)
            {
                var fTb = ClipLine(string.Join(", ", flist.ToArray()),
                                   Theme.Br(Theme.Faint(_dark)), Theme.FsLog, true);
                fTb.Margin = new Thickness(0, 3, 0, 0);
                body.Children.Add(fTb);
            }

            if (words.Length > 0)
            {
                // The operator's own phrase, in the form this file reserves for verbatim text.
                var q = new Border();
                q.Background = QuoteBg;
                q.CornerRadius = new CornerRadius(Theme.RadSmall);
                q.Padding = new Thickness(8, 4, 8, 4);
                q.Margin = new Thickness(0, 6, 0, 0);
                q.HorizontalAlignment = HorizontalAlignment.Left;
                var qt = new TextBlock();
                qt.Text = "\u201c" + words + "\u201d";
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
            rec.Child = body;
            col.Children.Add(rec);
            shown++;
        }
        return card;
    }


}
