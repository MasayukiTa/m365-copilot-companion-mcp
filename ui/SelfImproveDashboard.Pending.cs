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
