// CopilotChat.Messages.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
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
    // Scroll to the latest ONLY while the user is following along (at the bottom). If they have
    // scrolled up to read back, _stickBottom is false and we leave the viewport where they put it
    // -- the ScrollChanged handler re-arms following when they return to the bottom.
    void StickToEnd()
    {
        if (_stickBottom && _scroll != null) _scroll.ScrollToEnd();
    }

    // ── message rendering (Claude-like: user bubble, assistant plain) ───────────
    void AddUser(string text)
    {
        RemoveEmptyState();   // first real message -> the empty state must go
        var tb = new TextBox
        {
            Text = text, IsReadOnly = true, BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            TextWrapping = TextWrapping.Wrap, IsTabStop = false, FontFamily = new FontFamily(Theme.UiFont), FontSize = 14
        };
        SetRef(tb, ForegroundProperty, "Fg");
        var bubble = new Border { Child = tb, CornerRadius = new CornerRadius(Theme.RadBubble), Padding = new Thickness(16, 12, 16, 12), Margin = new Thickness(40, 6, 0, 24), HorizontalAlignment = HorizontalAlignment.Right, MaxWidth = 560 };
        SetRef(bubble, BackgroundProperty, "UserBg");
        _messages.Children.Add(bubble);
        StickToEnd();
    }

    // assistant: small "Copilot" label (+ hover copy button) + full-width plain text
    // (no bubble). Returns the content panel so a live turn can swap dots -> text; also
    // hands back the outer block via 'outer' so callers can stash the message text on
    // its .Tag for the copy button to read at click time.
    Panel AddAssistantContainer(out StackPanel outer)
    {
        RemoveEmptyState();   // an assistant turn is real content -> clear the empty state
        var block = new StackPanel { Margin = new Thickness(0, 6, 40, 24) };
        // header row: "Copilot" label on the left, hover-revealed copy button on the right
        var header = new DockPanel { Margin = new Thickness(0, 0, 0, 7) };
        var lbl = new TextBlock { Text = "Copilot", FontSize = 11, Margin = new Thickness(2, 0, 0, 0), FontWeight = FontWeights.Normal, VerticalAlignment = VerticalAlignment.Center };
        SetRef(lbl, TextBlock.ForegroundProperty, "Faint");
        var blockRef = block;
        var copy = new Button
        {
            Content = "\uE8C8", FontFamily = new FontFamily("Segoe MDL2 Assets"), FontSize = 12,
            Width = 26, BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            Cursor = Cursors.Hand, Visibility = Visibility.Hidden, ToolTip = T("copy")
        };
        SetRef(copy, ForegroundProperty, "Muted");
        copy.Click += delegate
        {
            var txt = blockRef.Tag as string;
            if (txt == null) txt = "";
            try { System.Windows.Clipboard.SetText(txt); } catch { }
        };
        DockPanel.SetDock(copy, Dock.Right);
        header.Children.Add(copy); header.Children.Add(lbl);
        var content = new StackPanel { Margin = new Thickness(2, 0, 0, 0) };
        block.Children.Add(header); block.Children.Add(content);
        var copyRef = copy;
        block.MouseEnter += delegate { copyRef.Visibility = Visibility.Visible; };
        block.MouseLeave += delegate { copyRef.Visibility = Visibility.Hidden; };
        _messages.Children.Add(block);
        StickToEnd();
        outer = block;
        return content;
    }

    // convenience overload for callers that only need the content panel
    Panel AddAssistantContainer() { StackPanel ignore; return AddAssistantContainer(out ignore); }

    void AddAssistant(string text)
    {
        StackPanel outer;
        var content = AddAssistantContainer(out outer);
        RenderAssistantBody(content, outer, text);
    }

    // Render the settled assistant answer as a SELECTABLE read-only RichTextBox.
    // TextBlock is not selectable; plain TextBox does not support LineHeight/paragraph spacing.
    // RichTextBox + FlowDocument solves both: text is selectable and line-height is honored.
    // Streaming still uses a plain TextBox (MakeText / _pendingText); on stream completion
    // RenderAssistantBody is called to replace it with the richer layout — no flicker during
    // streaming, rich rendering for the settled message.
    // The copy button still copies the full original markdown via outer.Tag.
    void RenderAssistantBody(Panel content, StackPanel outer, string text)
    {
        content.Children.Clear();
        if (outer != null) outer.Tag = text;

        var rtb = new RichTextBox();
        rtb.IsReadOnly = true;
        rtb.IsTabStop = false;
        rtb.IsDocumentEnabled = false;
        rtb.BorderThickness = new Thickness(0);
        rtb.Background = Brushes.Transparent;
        rtb.Padding = BODY_PAD;
        rtb.Focusable = true;   // required for text selection to work
        rtb.HorizontalAlignment = HorizontalAlignment.Stretch;
        rtb.FontFamily = BODY_FACE;
        rtb.FontSize = BODY_SIZE;
        // Disable scrollbars so the RichTextBox auto-sizes to its content height
        // instead of clipping to a fixed viewport.
        ScrollViewer.SetVerticalScrollBarVisibility(rtb, ScrollBarVisibility.Disabled);
        ScrollViewer.SetHorizontalScrollBarVisibility(rtb, ScrollBarVisibility.Disabled);
        rtb.VerticalScrollBarVisibility = ScrollBarVisibility.Disabled;
        rtb.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        SetRef(rtb, ForegroundProperty, "Fg");
        SetRef(rtb, TextBoxBase.SelectionBrushProperty, "Accent");

        // Build a FlowDocument from the plain text.
        // Paragraphs are separated by one-or-more blank lines; within a paragraph,
        // single newlines become soft line breaks (LineBreak inlines).
        var doc = new FlowDocument();
        doc.PagePadding = new Thickness(0);
        doc.FontFamily = rtb.FontFamily;
        doc.FontSize = BODY_SIZE;
        // Ensure the document foreground picks up the theme color.
        // FlowDocument is DependencyObject but not FrameworkElement, so call
        // SetResourceReference directly rather than through the SetRef helper.
        doc.SetResourceReference(FlowDocument.ForegroundProperty, "Fg");

        string plain = PlainText(text);

        // A LONG ANSWER OPENS SHORT, ONCE.
        //
        // MEASURED BEFORE BUILDING ANYTHING, because the obvious design was the wrong one. An
        // external review recommended folding long TOOL OUTPUT; on this machine's transcripts
        // that is not where the length is. 138 assistant turns with text: median 280 characters,
        // p90 1,130, max 2,282, and only 8% mention a tool call at all. Folding tool output would
        // have been machinery aimed at a case that barely occurs, while the 14% of turns over a
        // thousand characters -- ordinary prose -- kept pushing everything else off the screen.
        //
        // So the threshold is the measurement: above ASSISTANT_FOLD_CHARS the body opens clipped
        // with a line saying how much is hidden, and one click shows all of it.
        //
        // THREE RULES FROM THE REVIEW, each of which is a way this goes wrong:
        //   * the summary is an ENTRANCE to the original, never a replacement -- so the fold is
        //     the same text, clipped, and never a paraphrase;
        //   * nothing the reader has opened is ever folded again -- a panel that re-folds while
        //     being read is worse than one that never folded;
        //   * folding happens on first render only, which is why it is here and not in the
        //     refresh path.
        bool folds = plain.Length > ASSISTANT_FOLD_CHARS;
        string full = plain;
        if (folds) plain = plain.Substring(0, ASSISTANT_FOLD_CHARS);

        FillFlowDocument(doc, plain);

        rtb.Document = doc;
        content.Children.Add(rtb);

        if (folds)
        {
            // SAYS HOW MUCH IS HIDDEN, not just that something is. "続きを表示" alone leaves the
            // reader guessing whether it is a line or a page -- which is the question they are
            // trying to answer before clicking.
            int hidden = full.Length - ASSISTANT_FOLD_CHARS;
            var more = new TextBlock
            {
                Text = _lang == 0 ? ("続きを表示（あと " + hidden + " 字）")
                                  : ("Show the rest (" + hidden + " more characters)"),
                FontSize = 12,
                Cursor = Cursors.Hand,
                Margin = new Thickness(2, 6, 0, 0),
            };
            SetRef(more, TextBlock.ForegroundProperty, "Accent");
            var rtbRef = rtb;
            var moreRef = more;
            string fullRef = full;
            more.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e)
            {
                e.Handled = true;
                // ONE WAY ONLY. There is no "fold again": the reader asked for the text, and a
                // control that takes it back is the panel changing under them, which is the
                // whole family of defect this file has been working through.
                rtbRef.Document = BuildFlowDocument(fullRef, rtbRef.FontFamily);
                moreRef.Visibility = Visibility.Collapsed;
            };
            content.Children.Add(more);
        }
    }

    //: Above this many characters an assistant answer opens clipped, with a line saying how
    //: much is hidden and one click to show all of it.
    //:
    //: FROM THE MEASUREMENT, not from a guess. 138 assistant turns on this machine: median 280
    //: characters, p90 1,130, max 2,282. A threshold at the p90 leaves the 86 percent that were
    //: already short untouched and catches the 14 percent that were burying everything under
    //: them. Set it much lower and every answer grows a control nobody needs.
    const int ASSISTANT_FOLD_CHARS = 1100;

    static string PlainText(string md)
    {
        if (md == null) return "";
        var sb = new StringBuilder();
        string[] lines = md.Replace("\r\n", "\n").Replace("\r", "\n").Split('\n');
        foreach (var raw in lines)
        {
            string ln = raw;
            int h = 0; while (h < ln.Length && ln[h] == '#') h++;
            if (h > 0 && h < ln.Length && ln[h] == ' ') ln = ln.Substring(h + 1);   // heading -> plain
            ln = ln.Replace("**", "").Replace("`", "");                              // drop bold/code markers
            sb.Append(ln).Append('\n');
        }
        return sb.ToString().TrimEnd('\n');
    }


    // The paragraph layout for an assistant answer, as a function so the folded body and the
    // full body are built the same way. It was inline, which is why unfolding had no way to
    // re-render without duplicating it -- and a second copy of a layout is a second place for it
    // to drift.
    static void FillFlowDocument(FlowDocument doc, string plain)
    {
        // Split on runs of 2+ newlines to identify paragraph boundaries.
        // We do this manually without Regex (C# 5 compatible, no extra imports).
        var paragraphBlocks = new List<string>();
        var lines = plain.Replace("\r\n", "\n").Replace("\r", "\n").Split('\n');
        var blockLines = new List<string>();
        for (int i = 0; i < lines.Length; i++)
        {
            if (lines[i].Length == 0)
            {
                // blank line: flush current block if non-empty
                if (blockLines.Count > 0)
                {
                    paragraphBlocks.Add(string.Join("\n", blockLines.ToArray()));
                    blockLines.Clear();
                }
            }
            else
            {
                blockLines.Add(lines[i]);
            }
        }
        // flush any trailing block
        if (blockLines.Count > 0)
            paragraphBlocks.Add(string.Join("\n", blockLines.ToArray()));

        if (paragraphBlocks.Count == 0)
        {
            // Empty text: add a single empty paragraph to avoid an empty-document edge case.
            doc.Blocks.Add(new Paragraph());
        }
        else
        {
            for (int pi = 0; pi < paragraphBlocks.Count; pi++)
            {
                bool isLast = (pi == paragraphBlocks.Count - 1);
                var para = new Paragraph();
                para.LineHeight = 22;
                para.LineStackingStrategy = LineStackingStrategy.BlockLineHeight;
                // Paragraph bottom margin: small on last to avoid excess trailing space.
                para.Margin = isLast ? new Thickness(0, 0, 0, 2) : new Thickness(0, 0, 0, 12);

                // Within a block, split on "\n" for soft line breaks.
                string[] segments = paragraphBlocks[pi].Split('\n');
                for (int si = 0; si < segments.Length; si++)
                {
                    para.Inlines.Add(new Run(segments[si]));
                    if (si < segments.Length - 1)
                        para.Inlines.Add(new LineBreak());
                }
                doc.Blocks.Add(para);
            }
        }

    }

    // A document for `plain` with the same layout, used when a folded answer is opened.
    static FlowDocument BuildFlowDocument(string plain, FontFamily family)
    {
        var d = new FlowDocument { PagePadding = new Thickness(0), FontFamily = family, FontSize = BODY_SIZE };
        d.SetResourceReference(FlowDocument.ForegroundProperty, "Fg");
        FillFlowDocument(d, plain);
        return d;
    }

    //: THE TWO RENDERINGS OF ONE ANSWER MUST MEASURE THE SAME.
    //
    // A streaming answer is a plain TextBox (MakeText); the settled one is a RichTextBox over a
    // FlowDocument (RenderAssistantBody), because TextBlock cannot be selected and TextBox has
    // no line height. That swap is fine as long as the two agree on everything that decides
    // where a line breaks -- and they did not. The streaming box asked for "Segoe UI" and the
    // settled one for "Segoe UI Variable, Segoe UI", which are different faces with different
    // advance widths, and the streaming box carried 2px of left padding the settled one did
    // not. So at the moment an answer finished, every line re-wrapped.
    //
    // An external review of this window named exactly this and said to treat it as a READING
    // POSITION problem rather than a cosmetic one: the reader is mid-sentence when the text
    // moves under them. These constants exist so the two paths cannot drift apart again, and
    // `test_an_answer_does_not_rewrap_when_it_settles` asserts both use them.

    const double BODY_SIZE = 14;

    TextBox MakeText(string text)
    {
        var tb = new TextBox
        {
            Text = text, IsReadOnly = true, BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            TextWrapping = TextWrapping.Wrap, IsTabStop = false,
            FontFamily = BODY_FACE, FontSize = BODY_SIZE, Padding = BODY_PAD
        };
        SetRef(tb, ForegroundProperty, "Fg");
        return tb;
    }

    // the sibling app chat loader: three slate dots, staggered animate-bounce.
    FrameworkElement MakeTyping()
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(2, 4, 0, 4) };
        for (int i = 0; i < 3; i++)
        {
            var dot = new Border { Width = 7, Height = 7, CornerRadius = new CornerRadius(7 / 2.0), Margin = new Thickness(0, 0, 5, 0), VerticalAlignment = VerticalAlignment.Center };
            SetRef(dot, BackgroundProperty, "Muted");
            var tt = new TranslateTransform();
            dot.RenderTransform = tt;
            var anim = new DoubleAnimation
            {
                From = 3, To = -4, Duration = new Duration(TimeSpan.FromMilliseconds(420)),
                AutoReverse = true, RepeatBehavior = RepeatBehavior.Forever,
                BeginTime = TimeSpan.FromMilliseconds(i * 150), EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut }
            };
            tt.BeginAnimation(TranslateTransform.YProperty, anim);
            row.Children.Add(dot);
        }
        var lbl = new TextBlock { Text = T("generating"), FontSize = 11.5, Margin = new Thickness(6, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center };
        SetRef(lbl, TextBlock.ForegroundProperty, "Faint");
        row.Children.Add(lbl);
        return row;
    }
}
