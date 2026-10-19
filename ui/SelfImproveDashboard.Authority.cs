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

partial class SelfImproveDashboardWindow
{
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
}
