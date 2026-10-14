// FleetCockpit.CardTabs.cs -- card tabs: overview, timeline, conversation tab, review ledger, logs, artifacts.
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

    // Per-card selected tab (0=Overview,1=Conversation,2=Review,3=Logs). Persisted so a streaming
    // worker's open tab doesn't snap back to Overview on each status re-render.
    Dictionary<string, int> _cardTab = new Dictionary<string, int>();

    // Expanded-card tab group (spec): Overview default; raw transcript/refuter/internal fields live
    // behind Conversation/Review/Logs instead of all dumping onto the surface. Clicking a tab flips
    // panel visibility in place (no re-render needed); the choice is remembered in _cardTab.
    UIElement BuildCardTabs(Dictionary<string, object> w, string name, string goal, string last,
                            string reason, bool terminal)
    {
        int sel = _cardTab.ContainsKey(name) ? _cardTab[name] : 0;
        string outcome = S(w, "outcome");
        bool done = outcome == "DONE";
        int reviews = I(w, "verify_attempts");
        bool verifiedOk = string.Equals(S(w, "verified"), "True", StringComparison.OrdinalIgnoreCase);
        string tpath = S(w, "transcript");

        var wrap = new StackPanel { Margin = new Thickness(24, 12, 0, 2) };

        var panels = new UIElement[] {
            TabOverview(goal, last, outcome, terminal, reviews, verifiedOk, tpath, w),
            TabConversation(tpath, S(w, "name"), S(w, "conv_url")),
            TabReview(reason, done, terminal, reviews, tpath),
            TabLogs(w, reason)
        };
        string[] labels = _lang == 0 ? new string[] { "概要", "会話", "レビュー", "ログ" }
                                      : new string[] { "Overview", "Conversation", "Review", "Logs" };

        var strip = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 12) };
        var txts = new TextBlock[4];
        var unders = new Border[4];
        for (int i = 0; i < 4; i++)
        {
            int idx = i;
            var tcol = new StackPanel();
            var tt = new TextBlock { Text = labels[i], FontSize = 12.5, Margin = new Thickness(0, 0, 0, 4) };
            // Half the height: a fully rounded cap on the underline. Geometry, not style.
        var un = new Border { Height = 2, CornerRadius = new CornerRadius(2 / 2.0) };
            tcol.Children.Add(tt); tcol.Children.Add(un);
            txts[i] = tt; unders[i] = un;
            var box = new Border { Child = tcol, Cursor = Cursors.Hand, Background = Brushes.Transparent, Margin = new Thickness(0, 0, 16, 0) };
            box.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e)
            {
                e.Handled = true;
                _cardTab[name] = idx;
                for (int j = 0; j < 4; j++)
                {
                    panels[j].Visibility = j == idx ? Visibility.Visible : Visibility.Collapsed;
                    PaintTab(txts[j], unders[j], j == idx);
                }
            };
            strip.Children.Add(box);
        }
        wrap.Children.Add(strip);
        for (int i = 0; i < 4; i++)
        {
            panels[i].Visibility = i == sel ? Visibility.Visible : Visibility.Collapsed;
            PaintTab(txts[i], unders[i], i == sel);
            wrap.Children.Add(panels[i]);
        }
        return wrap;
    }

    void PaintTab(TextBlock t, Border underline, bool active)
    {
        t.Foreground = active ? Fg : Muted;
        t.FontWeight = active ? FontWeights.SemiBold : FontWeights.Normal;
        underline.Background = active ? Theme.Br(Theme.Accent(_dark)) : Brushes.Transparent;
    }

    TextBlock SectLabel(string s)
    {
        return new TextBlock { Text = s, Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 11.5,
                               FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 12, 0, 4) };
    }

    // ── P1 ARTIFACT LINKS ───────────────────────────────────────────────────────
    // Detect Windows file/folder paths inside agent result text and make them clickable.
    // Matches: a drive-letter path (C:\... or C:/...) OR a UNC path (\\host\share\...),
    // with either separator, allowing Japanese and other non-space/non-quote characters in
    // segments, stopping at whitespace, quotes, or bracket delimiters. Trailing punctuation
    // (Japanese 。、 and ASCII . , ) ] ; :) is stripped by CleanPathTail after the match so a
    // path at the end of a sentence doesn't swallow the period. Parsing is capped by the caller
    // to the first ~4000 chars so a huge blob can't stall the UI thread.
    static readonly Regex _pathRe = new Regex(
        @"(?:[A-Za-z]:[\\/]|\\\\[^\\/\s""'<>|]+[\\/])[^\s""'<>|(){}\[\]]+",
        RegexOptions.Compiled);
    const int PathScanCap = 4000;

    // Strip trailing sentence punctuation the regex may have greedily included.
    static string CleanPathTail(string p)
    {
        if (string.IsNullOrEmpty(p)) return p;
        int end = p.Length;
        while (end > 0)
        {
            char c = p[end - 1];
            if (c == '。' || c == '、' || c == '.' || c == ',' || c == ')' || c == ']'
                || c == ';' || c == ':' || c == '!' || c == '?' || c == '"' || c == '\'')
                end--;
            else break;
        }
        return p.Substring(0, end);
    }

    // Reveal a detected path: file -> explorer /select (highlight in its folder); directory ->
    // open the folder; neither exists -> no-op (the run is rendered non-interactive with a tooltip).
    static bool RevealPath(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                System.Diagnostics.Process.Start("explorer.exe", "/select,\"" + path + "\"");
                return true;
            }
            else if (Directory.Exists(path))
            {
                System.Diagnostics.Process.Start("explorer.exe", "\"" + path + "\"");
                return true;
            }
        }
        catch (Exception) { }
        return false;
    }

    // Distinct EXISTING paths (file or folder) found in `text`, in first-seen order, capped scan.
    List<string> DetectExistingPaths(string text)
    {
        var outp = new List<string>();
        if (string.IsNullOrEmpty(text)) return outp;
        string scan = text.Length > PathScanCap ? text.Substring(0, PathScanCap) : text;
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (Match m in _pathRe.Matches(scan))
        {
            string p = CleanPathTail(m.Value);
            if (p.Length < 4) continue;
            bool exists;
            try { exists = File.Exists(p) || Directory.Exists(p); } catch (Exception) { exists = false; }
            if (!exists) continue;
            if (seen.Add(p)) outp.Add(p);
        }
        return outp;
    }

    // A hyperlink-styled run for one path. Clickable when the path exists (reveal in folder / open
    // folder); disabled + "見つかりません" tooltip when it does not.
    Inline MakePathRun(string rawPath)
    {
        string path = CleanPathTail(rawPath);
        bool exists;
        try { exists = File.Exists(path) || Directory.Exists(path); } catch (Exception) { exists = false; }
        if (!exists)
        {
            var dead = new Run(rawPath);
            dead.Foreground = Muted;
            dead.ToolTip = T("path_missing");
            return dead;
        }
        var link = new Hyperlink(new Run(rawPath));
        link.Foreground = Theme.Br(Theme.Info(_dark));   // link color from tokens
        link.Cursor = Cursors.Hand;
        link.ToolTip = path;
        string captured = path;
        link.Click += delegate { RevealPath(captured); };
        return link;
    }

    // Result text as an inline-capable, wrapping, selectable block with any detected file/folder
    // paths rendered as clickable links. Falls back to the plain RoText TextBox when the text has
    // no detectable path, so wrapping / em-height stay identical to the old rendering. Uses the
    // SAME MaxHeight / scroll affordance so tall results still scroll rather than push the card.
    UIElement ResultText(string s, Brush fg, double size)
    {
        var paths = DetectPathSpans(s);
        if (paths.Count == 0) return RoText(s, fg, size);

        var rtb = new RichTextBox {
            IsReadOnly = true, IsDocumentEnabled = true, BorderThickness = new Thickness(0),
            Background = Brushes.Transparent, Padding = new Thickness(0), Foreground = fg,
            FontSize = size, IsTabStop = false, MaxHeight = 160,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        var para = new Paragraph { Margin = new Thickness(0), LineHeight = size * 1.35, Foreground = fg };
        int cur = 0;
        foreach (var span in paths)
        {
            if (span.Key > cur) para.Inlines.Add(new Run(s.Substring(cur, span.Key - cur)));
            string raw = s.Substring(span.Key, span.Value - span.Key);
            para.Inlines.Add(MakePathRun(raw));
            cur = span.Value;
        }
        if (cur < s.Length) para.Inlines.Add(new Run(s.Substring(cur)));
        rtb.Document = new FlowDocument(para) { PagePadding = new Thickness(0), Foreground = fg };
        // Same as SwallowMouseUp (which is TextBox-typed): stop the card's row click from firing when
        // the user selects text or clicks inside the result body.
        rtb.AddHandler(UIElement.MouseLeftButtonUpEvent,
            new MouseButtonEventHandler(delegate (object snd, MouseButtonEventArgs ev) { ev.Handled = true; }), true);
        return rtb;
    }

    // Character spans (start,end) of raw path substrings in the FULL text (only within the scan cap
    // so cost is bounded). Returns raw match spans (trailing punctuation NOT trimmed here so the
    // surrounding plain text keeps that punctuation); MakePathRun trims it for the click target.
    List<KeyValuePair<int, int>> DetectPathSpans(string text)
    {
        var outp = new List<KeyValuePair<int, int>>();
        if (string.IsNullOrEmpty(text)) return outp;
        int cap = Math.Min(text.Length, PathScanCap);
        foreach (Match m in _pathRe.Matches(text.Substring(0, cap)))
        {
            string cleaned = CleanPathTail(m.Value);
            if (cleaned.Length < 4) continue;
            // span covers only the cleaned path so trailing "。" etc. stays in the plain run
            outp.Add(new KeyValuePair<int, int>(m.Index, m.Index + cleaned.Length));
        }
        return outp;
    }

    // The compact 成果物 row on a COMPLETED card: up to 3 distinct existing files, each clickable
    // (reveal in folder), with "+N" when more were detected. No element when none found.
    UIElement ArtifactsRow(string resultText)
    {
        var files = new List<string>();
        foreach (string p in DetectExistingPaths(resultText))
        {
            bool isFile;
            try { isFile = File.Exists(p); } catch (Exception) { isFile = false; }
            if (isFile) files.Add(p);
        }
        if (files.Count == 0) return null;

        var sp = new StackPanel();
        sp.Children.Add(SectLabel(T("artifacts")));
        var wrap = new WrapPanel();
        int shown = Math.Min(3, files.Count);
        for (int i = 0; i < shown; i++)
        {
            string full = files[i];
            var chip = new Border {
                BorderThickness = new Thickness(1), BorderBrush = Border, Background = BtnBg,
                CornerRadius = new CornerRadius(Theme.RadSmall), Padding = new Thickness(8, 2, 8, 2),
                Margin = new Thickness(0, 0, 6, 4), Cursor = Cursors.Hand };
            var tb = new TextBlock {
                Text = Path.GetFileName(full), Foreground = Theme.Br(Theme.Info(_dark)),
                FontSize = 12, TextTrimming = TextTrimming.CharacterEllipsis, MaxWidth = 240 };
            tb.ToolTip = full;
            chip.Child = tb;
            string captured = full;
            chip.MouseLeftButtonUp += delegate (object o, MouseButtonEventArgs ev) { ev.Handled = true; RevealPath(captured); };
            wrap.Children.Add(chip);
        }
        if (files.Count > shown)
            wrap.Children.Add(new TextBlock {
                Text = "+" + (files.Count - shown), Foreground = Muted, FontSize = 12,
                VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 0, 4) });
        sp.Children.Add(wrap);
        return sp;
    }

    TextBox RoText(string s, Brush fg, double size)
    {
        var t = new TextBox { Text = s, Foreground = fg, FontSize = size, IsReadOnly = true,
            BorderThickness = new Thickness(0), Background = Brushes.Transparent, Padding = new Thickness(0),
            IsTabStop = false, TextWrapping = TextWrapping.Wrap, MaxHeight = 160,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        SwallowMouseUp(t);
        return t;
    }

    UIElement ExecutionOverview(Dictionary<string, object> w)
    {
        var execution = Obj(w, "execution");
        if (execution == null) return null;

        var sp = new StackPanel();
        string state = S(execution, "state");
        string current = S(execution, "current_step");
        int currentIndex = I(execution, "current_step_index");
        int total = I(execution, "total_steps");
        int completed = I(execution, "completed_count");
        string lastProgress = S(execution, "last_progress");
        string next = S(execution, "next_step");
        string waiting = S(execution, "waiting_reason");

        string head = (total > 0 ? (currentIndex + "/" + total) : currentIndex.ToString());
        if (!string.IsNullOrEmpty(state)) head += " · " + state;
        if (!string.IsNullOrEmpty(current)) head += " · " + current;
        var headLine = new StackPanel { Orientation = Orientation.Horizontal };
        headLine.Children.Add(MakeIcon("play_arrow", 13, Fg));
        headLine.Children.Add(new TextBlock { Text = head, Foreground = Fg, FontSize = 12.5,
            FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(4, 0, 0, 0) });
        sp.Children.Add(headLine);

        if (!string.IsNullOrEmpty(lastProgress))
            sp.Children.Add(new TextBlock { Text = (_lang == 0 ? "進捗: " : "Progress: ") + lastProgress,
                Foreground = Muted, FontSize = 12.5, Margin = new Thickness(0, 3, 0, 0), TextWrapping = TextWrapping.Wrap });
        if (!string.IsNullOrEmpty(waiting))
            sp.Children.Add(new TextBlock { Text = (_lang == 0 ? "待機理由: " : "Waiting: ") + waiting,
                Foreground = Muted, FontSize = 12.5, Margin = new Thickness(0, 3, 0, 0), TextWrapping = TextWrapping.Wrap });

        object doneRaw;
        if (execution.TryGetValue("completed_steps", out doneRaw) && doneRaw is object[])
        {
            var done = (object[])doneRaw;
            if (done.Length > 0)
            {
                sp.Children.Add(new TextBlock { Text = (_lang == 0 ? "完了 " : "Completed ") + completed,
                    Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 11.5, Margin = new Thickness(0, 5, 0, 1) });
                foreach (object obj in done)
                {
                    var step = obj as Dictionary<string, object>;
                    if (step == null) continue;
                    string instruction = S(step, "instruction");
                    string summary = S(step, "summary");
                    string line = instruction;
                    if (!string.IsNullOrEmpty(summary) && summary != instruction) line += " — " + summary;
                    var doneLine = new StackPanel { Orientation = Orientation.Horizontal,
                        Margin = new Thickness(8, 1, 0, 1) };
                    doneLine.Children.Add(MakeIcon("check", 12, Muted));
                    doneLine.Children.Add(new TextBlock { Text = line, Foreground = Muted, FontSize = 12,
                        Margin = new Thickness(4, 0, 0, 0), TextWrapping = TextWrapping.Wrap });
                    sp.Children.Add(doneLine);
                }
            }
        }

        if (!string.IsNullOrEmpty(next))
            sp.Children.Add(new TextBlock { Text = (_lang == 0 ? "次: " : "Next: ") + next,
                Foreground = Muted, FontSize = 12.5, Margin = new Thickness(0, 5, 0, 0), TextWrapping = TextWrapping.Wrap });

        object artsRaw;
        if (execution.TryGetValue("artifacts", out artsRaw) && artsRaw is object[])
        {
            var arts = (object[])artsRaw;
            if (arts.Length > 0)
            {
                sp.Children.Add(new TextBlock { Text = _lang == 0 ? "成果物" : "Artifacts",
                    Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 11.5, Margin = new Thickness(0, 5, 0, 1) });
                foreach (object obj in arts)
                {
                    var artifact = obj as Dictionary<string, object>;
                    if (artifact == null) continue;
                    string label = S(artifact, "path");
                    if (string.IsNullOrEmpty(label)) label = S(artifact, "name");
                    if (string.IsNullOrEmpty(label)) label = S(artifact, "uri");
                    if (string.IsNullOrEmpty(label)) label = _lang == 0 ? "成果物" : "artifact";
                    sp.Children.Add(new TextBlock { Text = "• " + label, Foreground = Muted, FontSize = 12,
                        Margin = new Thickness(8, 1, 0, 1), TextWrapping = TextWrapping.Wrap });
                }
            }
        }
        return sp;
    }

    UIElement TabOverview(string goal, string last, string outcome, bool terminal, int reviews,
                          bool verifiedOk, string tpath, Dictionary<string, object> w)
    {
        var sp = new StackPanel();
        UIElement execView = ExecutionOverview(w);
        if (execView != null)
        {
            sp.Children.Add(SectLabel(_lang == 0 ? "実行" : "Execution"));
            sp.Children.Add(execView);
        }
        sp.Children.Add(SectLabel(_lang == 0 ? "結果" : "Result"));
        // Precedence: display_result (cleaned final answer from runner) > last > OutcomeLabel fallback.
        string displayResult = (w != null) ? S(w, "display_result") : "";
        string result = WorkerResultText(w, terminal);
        // P1: render the result with clickable file/folder paths (falls back to plain text when none).
        sp.Children.Add(ResultText(result, Fg, 13));

        // P1: compact 成果物 row on COMPLETED cards, listing the distinct existing files detected.
        if (terminal)
        {
            UIElement artRow = ArtifactsRow(result);
            if (artRow != null) sp.Children.Add(artRow);
        }

        var checks = new List<string>();
        // Prefer display_result evidence, then last, for the "final answer obtained" check.
        bool hasFinalAnswer = !string.IsNullOrEmpty(displayResult) || !string.IsNullOrEmpty(last);
        if (hasFinalAnswer)
            checks.Add(_lang == 0 ? "最終応答を取得" : "Final response obtained");
        int verifyAttempts = (w != null) ? I(w, "verify_attempts") : reviews;
        if (verifyAttempts > 0)
            checks.Add(_lang == 0 ? ("レビュー " + verifyAttempts + " 回実施") : ("Reviewed " + verifyAttempts + " time(s)"));
        if (verifiedOk)
            checks.Add(_lang == 0 ? "レビューで最終応答を確認 / Verified by review" : "Verified by review");
        // If terminal and no other evidence at all, fall back to the outcome label (e.g. CANCELLED).
        if (terminal && checks.Count == 0)
            checks.Add(OutcomeLabel(outcome));
        if (checks.Count > 0)
        {
            sp.Children.Add(SectLabel(_lang == 0 ? "チェック" : "Checks"));
            foreach (var c in checks)
                sp.Children.Add(new TextBlock { Text = "・" + c, Foreground = Muted, FontSize = 12.5, Margin = new Thickness(0, 1, 0, 1) });
        }

        // ── Timeline section ──────────────────────────────────────────────────────
        // Prefer phase_events (same source as Evidence Spine) when available; fall back to transcript.
        sp.Children.Add(SectLabel(_lang == 0 ? "タイムライン" : "Timeline"));
        var tsEvents = BuildTimelineEvents(tpath, outcome, terminal, reviews, w);
        foreach (var ev in tsEvents)
            sp.Children.Add(new TextBlock {
                Text = "・" + ev.Item1, Foreground = Theme.Br(ev.Item2), FontSize = 12,
                Margin = new Thickness(0, 1, 0, 1), TextWrapping = TextWrapping.Wrap });

        sp.Children.Add(SectLabel(_lang == 0 ? "指示" : "Goal"));
        sp.Children.Add(RoText(goal, Muted, 12.5));
        return sp;
    }

    // Build the ordered event list for the Timeline section.
    // Prefers phase_events from the worker dict (same source as Evidence Spine) when present.
    // Falls back to transcript-derived timestamps when phase_events is absent.
    List<Tuple<string, string>> BuildTimelineEvents(string tpath, string outcome, bool terminal, int reviews,
                                     Dictionary<string, object> w)
    {
        bool ja = _lang == 0;
        Func<double, string> fmtTs = delegate(double ts)
        {
            if (ts <= 0) return "";
            try
            {
                var dt = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(ts).ToLocalTime();
                return dt.ToString("HH:mm") + " ";
            }
            catch { return ""; }
        };

        // ── REAL mode: phase_events present on this worker ─────────────────────────
        if (w != null)
        {
            object peRaw;
            if (w.TryGetValue("phase_events", out peRaw) && peRaw is object[])
            {
                object[] peArr = (object[])peRaw;
                if (peArr.Length > 0)
                {
                    var evs2 = new List<Tuple<string, string>>();
                    foreach (object peObj in peArr)
                    {
                        var pe = peObj as Dictionary<string, object>;
                        if (pe == null) continue;
                        double peTs = 0;
                        object peTsRaw;
                        if (pe.TryGetValue("ts", out peTsRaw) && peTsRaw != null)
                        { try { peTs = Convert.ToDouble(peTsRaw); } catch { } }
                        string peEvent = "";
                        object peEvRaw;
                        if (pe.TryGetValue("event", out peEvRaw) && peEvRaw != null)
                            peEvent = peEvRaw.ToString();
                        // Use the same event-history vocabulary as the Spine (Theme.TimelineLabel)
                        string localLabel = Theme.TimelineLabel(peEvent, _lang);
                        if (string.IsNullOrEmpty(localLabel) || localLabel == peEvent)
                        {
                            object peLblRaw;
                            if (pe.TryGetValue("label", out peLblRaw) && peLblRaw != null
                                && !string.IsNullOrEmpty(peLblRaw.ToString()))
                                localLabel = peLblRaw.ToString();
                        }
                        if (string.IsNullOrEmpty(localLabel)) localLabel = peEvent;
                        string timePrefix = peTs > 0 ? fmtTs(peTs) : "";
                        evs2.Add(new Tuple<string, string>(timePrefix + localLabel, Theme.TimelineColor(peEvent, _dark)));
                    }
                    if (evs2.Count > 0) return evs2;
                }
            }
        }

        // ── COMPUTED fallback: derive timestamps from transcript ──────────────────
        double metaTs = 0, firstTurnTs = 0;
        bool hasTs = false;
        try
        {
            if (!string.IsNullOrEmpty(tpath) && File.Exists(tpath))
            {
                string[] lines;
                using (var fsr = new FileStream(tpath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                using (var sr = new StreamReader(fsr, Encoding.UTF8))
                    lines = sr.ReadToEnd().Replace("\r", "").Split('\n');
                foreach (var ln in lines)
                {
                    if (string.IsNullOrEmpty(ln)) continue;
                    Dictionary<string, object> obj;
                    try { obj = _js.DeserializeObject(ln) as Dictionary<string, object>; } catch { continue; }
                    if (obj == null) continue;
                    if (obj.ContainsKey("meta") && Convert.ToBoolean(obj["meta"]))
                    {
                        if (obj.ContainsKey("ts") && obj["ts"] != null)
                        { metaTs = Convert.ToDouble(obj["ts"]); hasTs = true; }
                        continue;
                    }
                    if (obj.ContainsKey("role") && obj.ContainsKey("ts") && obj["ts"] != null)
                    {
                        if (firstTurnTs == 0)
                            firstTurnTs = Convert.ToDouble(obj["ts"]);
                        if (firstTurnTs > 0) break;
                    }
                }
            }
        }
        catch { }

        var evs = new List<Tuple<string, string>>();
        string queuedTs = hasTs ? fmtTs(metaTs) : "";
        evs.Add(new Tuple<string, string>(queuedTs + (ja ? "投入" : "Queued"), Theme.TimelineColor("pending", _dark)));
        string startTs = (firstTurnTs > 0) ? fmtTs(firstTurnTs) : "";
        evs.Add(new Tuple<string, string>(startTs + (ja ? "開始" : "Started"), Theme.TimelineColor("ready", _dark)));
        if (reviews > 0)
            evs.Add(new Tuple<string, string>(
                ja ? ("レビュー (" + reviews + "x)") : ("Reviewed (" + reviews + "x)"),
                Theme.TimelineColor("refuting", _dark)));
        if (terminal || outcome == "INTERRUPTED")
        {
            string outcomeEv;
            string outcomeKey;
            switch (outcome)
            {
                case "DONE":      outcomeEv = ja ? "完了" : "Completed"; outcomeKey = "done"; break;
                case "MAXTURNS":  outcomeEv = ja ? "ターン上限" : "Max turns reached"; outcomeKey = "maxturns"; break;
                case "STUCK":     outcomeEv = ja ? "停滞" : "Stuck"; outcomeKey = "stuck"; break;
                case "ERROR":     outcomeEv = ja ? "エラー" : "Error"; outcomeKey = "error"; break;
                case "CANCELLED": outcomeEv = ja ? "停止" : "Cancelled"; outcomeKey = "cancelled"; break;
                case "INTERRUPTED": outcomeEv = ja ? "中断" : "Interrupted"; outcomeKey = "interrupted"; break;
                case "EVIDENCE_CONTRADICTED": outcomeEv = ja ? "記録と矛盾" : "Contradicted"; outcomeKey = "stuck"; break;
                default:           outcomeEv = string.IsNullOrEmpty(outcome) ? (ja ? "終了" : "Ended") : outcome; outcomeKey = "cancelled"; break;
            }
            evs.Add(new Tuple<string, string>(outcomeEv, Theme.TimelineColor(outcomeKey, _dark)));
        }
        return evs;
    }

    // Read the "ts" of the first turn in the transcript (meta or first role line) to compute
    // per-card elapsed. Returns 0 if no transcript or no ts field found.
    double ReadTranscriptStartTs(string tpath)
    {
        try
        {
            if (string.IsNullOrEmpty(tpath) || !File.Exists(tpath)) return 0;
            string[] lines;
            using (var fsr = new FileStream(tpath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8))
                lines = sr.ReadToEnd().Replace("\r", "").Split('\n');
            foreach (var ln in lines)
            {
                if (string.IsNullOrEmpty(ln)) continue;
                Dictionary<string, object> obj;
                try { obj = _js.DeserializeObject(ln) as Dictionary<string, object>; } catch { continue; }
                if (obj == null) continue;
                // meta line has "ts" -- this is the best proxy for "worker was queued"
                if (obj.ContainsKey("meta") && Convert.ToBoolean(obj["meta"]))
                {
                    if (obj.ContainsKey("ts") && obj["ts"] != null)
                        return Convert.ToDouble(obj["ts"]);
                }
            }
        }
        catch { }
        return 0;
    }

    UIElement TabConversation(string tpath, string workerName, string convUrl)
    {
        if (!string.IsNullOrEmpty(tpath))
        {
            var mini = MiniThread(tpath, workerName, convUrl);
            if (mini != null) return mini;
        }
        return new TextBlock { Text = _lang == 0 ? "（会話の履歴はまだありません）" : "(No conversation yet)",
                               Foreground = Muted, FontSize = 12.5 };
    }

    UIElement TabReview(string reason, bool done, bool terminal, int reviews, string transcriptPath = null)
    {
        var sp = new StackPanel();
        if (done)
        {
            // DONE: never surface raw "REFUTED" -- frame review as the check that it was.
            sp.Children.Add(new TextBlock {
                Text = _lang == 0 ? "レビューで内容を確認し、最終回答を確定しました。"
                                  : "Review checked the work and the final answer was confirmed.",
                Foreground = Muted, FontSize = 12.5, TextWrapping = TextWrapping.Wrap });
            if (reviews > 0)
                sp.Children.Add(new TextBlock {
                    Text = (_lang == 0 ? "確認回数: " : "Review passes: ") + reviews,
                    Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 12, Margin = new Thickness(0, 4, 0, 0) });
        }
        else if (terminal && !string.IsNullOrEmpty(reason))
        {
            sp.Children.Add(SectLabel(_lang == 0 ? "レビュー指摘" : "Review note"));
            sp.Children.Add(RoText(reason, Muted, 12.5));
        }
        else
        {
            sp.Children.Add(new TextBlock {
                Text = _lang == 0 ? "（レビュー記録はまだありません）" : "(No review notes yet)",
                Foreground = Muted, FontSize = 12.5 });
        }
        // The reviewer's own conversation, read from the sqlite ledger (full text, every reply).
        if (!string.IsNullOrEmpty(transcriptPath)) sp.Children.Add(ReviewerLedgerPanel(transcriptPath));
        return sp;
    }

    // ── REVIEWER RECORD (from the sqlite ledger) ───────────────────────────────────────────────
    // The full text the reviewer was sent and every reply, with lens, time and verdict, read from
    // sessions.sqlite3 (fleet_turns, key <worker key>__refuter_<lens>) through scripts\ledger_dump.py
    // -- the UI cannot open sqlite itself. Loaded on demand (the button), windowless, bounded by a
    // timeout; a run from before the reviewer was recorded says so instead of showing nothing.
    UIElement ReviewerLedgerPanel(string transcriptPath)
    {
        bool ja = _lang == 0;
        var host = new StackPanel { Margin = new Thickness(0, 12, 0, 0) };
        string wkey = "";
        try { wkey = Path.GetFileNameWithoutExtension(transcriptPath ?? ""); } catch (Exception) { }
        if (string.IsNullOrEmpty(wkey)) return host;
        host.Children.Add(SectLabel(ja ? "反証者の記録(台帳 sqlite)" : "Reviewer record (sqlite ledger)"));
        var body = new StackPanel();
        var btn = new Button();
        btn.Content = ja ? "反証の入力全文と全返答を表示" : "Show reviewer prompts and replies";
        btn.Cursor = Cursors.Hand; btn.FontSize = 12; btn.Padding = new Thickness(12, 3, 12, 3);
        btn.HorizontalAlignment = HorizontalAlignment.Left; btn.BorderThickness = new Thickness(1);
        btn.Background = BtnBg; btn.BorderBrush = Border; btn.Foreground = Fg;
        btn.Template = FlatButtonTemplate();
        string stateDir = "";
        try { stateDir = Path.GetDirectoryName(Path.GetFullPath(_statusPath)); } catch (Exception) { }
        string repo = RepoRoot();
        btn.Click += delegate (object s, RoutedEventArgs ev)
        {
            ev.Handled = true;
            btn.IsEnabled = false;
            body.Children.Clear();
            body.Children.Add(new TextBlock { Text = ja ? "台帳を読み込み中…" : "Reading the ledger...",
                                              Foreground = Muted, FontSize = 12 });
            var t = new Thread(new ThreadStart(delegate
            {
                string json = RunLedgerDump(repo, wkey, stateDir);
                try
                {
                    Dispatcher.BeginInvoke(new Action(delegate
                    {
                        btn.IsEnabled = true;
                        body.Children.Clear();
                        RenderReviewerLedger(body, json);
                    }));
                }
                catch (Exception) { }
            })) { IsBackground = true };
            t.Start();
        };
        host.Children.Add(btn);
        host.Children.Add(body);
        return host;
    }

    // Runs scripts\ledger_dump.py windowless and returns its single JSON line ("" on any failure).
    string RunLedgerDump(string repo, string key, string stateDir)
    {
        try
        {
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "\"" + Path.Combine(repo, "scripts", "ledger_dump.py") + "\" --key \"" + key + "\""
                + (string.IsNullOrEmpty(stateDir) ? "" : " --store-dir \"" + Path.Combine(stateDir, "sessions") + "\"");
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false; psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true; psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = Encoding.UTF8;
            var sbOut = new StringBuilder();
            using (var p = System.Diagnostics.Process.Start(psi))
            {
                p.OutputDataReceived += delegate (object _s, System.Diagnostics.DataReceivedEventArgs e)
                { if (e.Data != null) lock (sbOut) sbOut.AppendLine(e.Data); };
                p.ErrorDataReceived += delegate (object _s, System.Diagnostics.DataReceivedEventArgs e) { };
                p.BeginOutputReadLine(); p.BeginErrorReadLine();
                if (!p.WaitForExit(30000)) { try { p.Kill(); } catch (Exception) { } return ""; }
                p.WaitForExit();
            }
            lock (sbOut) return sbOut.ToString().Trim();
        }
        catch (Exception) { return ""; }
    }

    void RenderReviewerLedger(StackPanel body, string json)
    {
        bool ja = _lang == 0;
        Dictionary<string, object> d = null;
        try { if (!string.IsNullOrEmpty(json)) d = _js.Deserialize<Dictionary<string, object>>(json); } catch (Exception) { }
        if (d == null)
        {
            body.Children.Add(new TextBlock { Text = ja ? "台帳を読めませんでした(ledger_dump の出力なし)。" : "Could not read the ledger (no output from ledger_dump).",
                                              Foreground = Muted, FontSize = 12, TextWrapping = TextWrapping.Wrap });
            return;
        }
        var turns = d.ContainsKey("turns") ? d["turns"] as System.Collections.IEnumerable : null;
        bool any = false;
        if (turns != null)
        {
            foreach (object o in turns)
            {
                var tr = o as Dictionary<string, object>;
                if (tr == null) continue;
                any = true;
                string role = S(tr, "role");
                string label;
                switch (role)
                {
                    case "refuter_user": label = ja ? "反証者への入力(全文)" : "Sent to the reviewer (full)"; break;
                    case "refuter_wire": label = ja ? "実際に送信された全文" : "Payload actually sent"; break;
                    case "refuter_assistant": label = ja ? "反証者の返答(全文)" : "Reviewer reply (full)"; break;
                    case "refuter_verdict": label = ja ? "判定" : "Verdict"; break;
                    default: label = role; break;
                }
                string when = "";
                try
                {
                    double ts = Dbl(tr, "ts");
                    if (ts > 0) when = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(ts).ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss");
                }
                catch (Exception) { }
                string lens = S(tr, "lens");
                string meta = "[" + (string.IsNullOrEmpty(lens) ? "-" : lens) + "] " + label
                    + (when.Length > 0 ? "  " + when : "")
                    + (S(tr, "route").Length > 0 ? "  " + S(tr, "route") : "")
                    + (S(tr, "sha16").Length > 0 ? "  sha16:" + S(tr, "sha16") : "")
                    + (string.Equals(S(tr, "truncated"), "True", StringComparison.OrdinalIgnoreCase)
                        ? (ja ? "  (上限で切詰め済み)" : "  (cut at the size cap)") : "");
                body.Children.Add(new TextBlock { Text = meta, Foreground = role == "refuter_verdict" ? Fg : Muted,
                                                  FontSize = 11.5, FontWeight = FontWeights.SemiBold,
                                                  Margin = new Thickness(0, 8, 0, 2), TextWrapping = TextWrapping.Wrap });
                var tb = new TextBox();
                tb.Text = S(tr, "text");
                tb.Foreground = Muted; tb.FontSize = 12; tb.FontFamily = new FontFamily(Theme.CodeFont);
                tb.IsReadOnly = true; tb.TextWrapping = TextWrapping.Wrap;
                tb.BorderThickness = new Thickness(0); tb.Background = QuoteBg; tb.Padding = new Thickness(8);
                tb.MaxHeight = 280; tb.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
                SwallowMouseUp(tb);
                body.Children.Add(tb);
            }
        }
        if (!any)
        {
            body.Children.Add(new TextBlock {
                Text = ja ? "この実行には反証の全文が台帳にありません(反証の記録を始める前の実行、または反証が走っていません)。"
                          : "The ledger holds no reviewer text for this run (it predates reviewer recording, or no review ran).",
                Foreground = Muted, FontSize = 12, TextWrapping = TextWrapping.Wrap });
        }
    }

    UIElement TabLogs(Dictionary<string, object> w, string reason)
    {
        var sb = new StringBuilder();
        sb.Append("status=").Append(S(w, "status")).Append("  outcome=").Append(S(w, "outcome"));
        sb.Append("  turn=").Append(S(w, "turn")).Append("  verify_attempts=").Append(S(w, "verify_attempts"));
        sb.Append("  verified=").Append(S(w, "verified"));
        if (!string.IsNullOrEmpty(S(w, "submitter"))) sb.Append("  submitter=").Append(S(w, "submitter"));
        sb.Append('\n');
        if (!string.IsNullOrEmpty(reason)) sb.Append("\nreason:\n").Append(reason).Append('\n');
        var box = new Border { Background = QuoteBg, CornerRadius = new CornerRadius(Theme.RadCard), Padding = new Thickness(12, 12, 12, 12) };
        var t = RoText(sb.ToString(), Muted, 12);
        t.FontFamily = new FontFamily(Theme.CodeFont);
        t.MaxHeight = 220;
        box.Child = t;
        return box;
    }

    string OutcomeLabel(string outcome)
    {
        bool ja = _lang == 0;
        switch (outcome)
        {
            case "DONE": return ja ? "完了" : "Done";
            case "MAXTURNS": return ja ? "ターン上限に到達" : "Hit turn limit";
            case "STUCK": return ja ? "停滞して終了" : "Stuck";
            case "ERROR": return ja ? "エラーで終了" : "Error";
            case "CANCELLED": return ja ? "停止されました" : "Cancelled";
            case "INTERRUPTED": return ja ? "中断されました(コーディネータ停止)" : "Interrupted (coordinator died)";
            // The worker said it was finished; the recorded tool calls say otherwise -- the
            // acceptance command was never run, or nothing was written. NOT an error and NOT a
            // completion: a claim that could not be believed. DONE is a self-report measured at
            // 0.718 precision, and this is what checking it looks like on the card.
            case "EVIDENCE_CONTRADICTED": return ja ? "記録と矛盾" : "Contradicted by record";
            default: return string.IsNullOrEmpty(outcome) ? "" : outcome;
        }
    }
}
