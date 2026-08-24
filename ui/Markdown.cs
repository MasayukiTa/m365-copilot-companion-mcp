// Minimal Markdown-to-WPF renderer for the native WPF chat app.
// C# 5 only (csc.exe v4.0.30319). No Roslyn-era syntax.
// Compiled together with CopilotChat.cs (global scope, no namespace).

using System;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;

// Markdown renderer entry point. Integration: Md.Render(text) -> UIElement.
public static class Md
{
    // Theme resource keys defined by the host app.
    private const string KeyFg = "Fg";
    private const string KeyMuted = "Muted";
    private const string KeyBorder = "Border";
    private const string KeyCodeBg = "CodeBg";
    private const string KeyAccent = "Accent";

    // Render Markdown text into a vertical StackPanel of block elements.
    // Never throws: on any failure, falls back to a plain TextBlock of raw text.
    public static UIElement Render(string text)
    {
        try
        {
            if (text == null)
            {
                text = "";
            }

            var root = new StackPanel();
            root.Orientation = Orientation.Vertical;

            // Normalize line endings and split into lines.
            string normalized = text.Replace("\r\n", "\n").Replace("\r", "\n");
            string[] lines = normalized.Split('\n');

            int i = 0;
            var paragraph = new List<string>();

            while (i < lines.Length)
            {
                string line = lines[i];
                string trimmed = line.TrimStart();

                // Fenced code block start.
                if (IsFence(trimmed))
                {
                    FlushParagraph(root, paragraph);

                    string lang = ExtractLang(trimmed);
                    var codeLines = new List<string>();
                    i++;
                    while (i < lines.Length && !IsFence(lines[i].TrimStart()))
                    {
                        codeLines.Add(lines[i]);
                        i++;
                    }
                    // Skip the closing fence if present.
                    if (i < lines.Length)
                    {
                        i++;
                    }

                    root.Children.Add(BuildCodeBlock(string.Join("\n", codeLines.ToArray()), lang));
                    continue;
                }

                // Blank line: terminates a paragraph.
                if (trimmed.Length == 0)
                {
                    FlushParagraph(root, paragraph);
                    i++;
                    continue;
                }

                // Heading.
                int hLevel = HeadingLevel(trimmed);
                if (hLevel > 0)
                {
                    FlushParagraph(root, paragraph);
                    string htext = trimmed.Substring(hLevel).TrimStart();
                    // Drop the leading "#"s and the following space already handled.
                    root.Children.Add(BuildHeading(htext, hLevel));
                    i++;
                    continue;
                }

                // Unordered list item.
                if (IsUnorderedItem(trimmed))
                {
                    FlushParagraph(root, paragraph);
                    string itemText = trimmed.Substring(2);
                    root.Children.Add(BuildListItem("•", itemText));
                    i++;
                    continue;
                }

                // Ordered list item ("<n>. ").
                string orderedMarker;
                string orderedText;
                if (IsOrderedItem(trimmed, out orderedMarker, out orderedText))
                {
                    FlushParagraph(root, paragraph);
                    root.Children.Add(BuildListItem(orderedMarker, orderedText));
                    i++;
                    continue;
                }

                // Blockquote.
                if (trimmed.StartsWith("> ") || trimmed == ">")
                {
                    FlushParagraph(root, paragraph);
                    string qtext = trimmed.Length >= 2 ? trimmed.Substring(2) : "";
                    root.Children.Add(BuildBlockquote(qtext));
                    i++;
                    continue;
                }

                // Otherwise: accumulate into the current paragraph.
                paragraph.Add(trimmed);
                i++;
            }

            FlushParagraph(root, paragraph);

            return root;
        }
        catch (Exception)
        {
            // Hard fallback: plain text block of the raw input.
            var fallback = new TextBlock();
            fallback.Text = text ?? "";
            fallback.TextWrapping = TextWrapping.Wrap;
            fallback.SetResourceReference(TextBlock.ForegroundProperty, KeyFg);
            return fallback;
        }
    }

    // True if the (left-trimmed) line is a fence: ``` optionally followed by a lang tag.
    private static bool IsFence(string trimmed)
    {
        return trimmed.StartsWith("```");
    }

    // Extract the language tag after the opening fence, or null.
    private static string ExtractLang(string trimmed)
    {
        string rest = trimmed.Substring(3).Trim();
        if (rest.Length == 0)
        {
            return null;
        }
        return rest;
    }

    // Heading level 1..3 if the line begins with "# ", "## ", "### "; else 0.
    private static int HeadingLevel(string trimmed)
    {
        if (trimmed.StartsWith("### "))
        {
            return 3;
        }
        if (trimmed.StartsWith("## "))
        {
            return 2;
        }
        if (trimmed.StartsWith("# "))
        {
            return 1;
        }
        return 0;
    }

    private static bool IsUnorderedItem(string trimmed)
    {
        return trimmed.StartsWith("- ") || trimmed.StartsWith("* ") || trimmed.StartsWith("+ ");
    }

    // Match "<digits>. " at the start. Outputs the marker ("1.") and the remaining text.
    private static bool IsOrderedItem(string trimmed, out string marker, out string itemText)
    {
        marker = null;
        itemText = null;

        int n = 0;
        while (n < trimmed.Length && char.IsDigit(trimmed[n]))
        {
            n++;
        }
        if (n == 0)
        {
            return false;
        }
        // Need ". " after the digits.
        if (n + 1 < trimmed.Length && trimmed[n] == '.' && trimmed[n + 1] == ' ')
        {
            marker = trimmed.Substring(0, n) + ".";
            itemText = trimmed.Substring(n + 2);
            return true;
        }
        return false;
    }

    // Flush accumulated paragraph lines into a wrapped TextBlock.
    private static void FlushParagraph(StackPanel root, List<string> paragraph)
    {
        if (paragraph.Count == 0)
        {
            return;
        }
        string joined = string.Join(" ", paragraph.ToArray());
        paragraph.Clear();

        var tb = new TextBlock();
        tb.TextWrapping = TextWrapping.Wrap;
        tb.FontSize = 14;
        tb.LineHeight = 21;
        tb.Margin = new Thickness(0, 3, 0, 3);
        tb.SetResourceReference(TextBlock.ForegroundProperty, KeyFg);
        AppendInlines(tb.Inlines, joined);
        root.Children.Add(tb);
    }

    // Build a heading TextBlock.
    private static UIElement BuildHeading(string text, int level)
    {
        double size = 14.5;
        if (level == 1)
        {
            size = 19;
        }
        else if (level == 2)
        {
            size = 16.5;
        }

        var tb = new TextBlock();
        tb.FontWeight = FontWeights.Bold;
        tb.FontSize = size;
        tb.TextWrapping = TextWrapping.Wrap;
        tb.Margin = new Thickness(0, 10, 0, 4);
        tb.SetResourceReference(TextBlock.ForegroundProperty, KeyFg);
        AppendInlines(tb.Inlines, text);
        return tb;
    }

    // Build a single list-item row: a Muted marker followed by inline-formatted text.
    private static UIElement BuildListItem(string marker, string text)
    {
        var panel = new StackPanel();
        panel.Orientation = Orientation.Horizontal;
        panel.Margin = new Thickness(8, 2, 0, 2);

        var bullet = new TextBlock();
        bullet.Text = marker + " ";
        bullet.FontSize = 14;
        bullet.MinWidth = 18;
        bullet.SetResourceReference(TextBlock.ForegroundProperty, KeyMuted);
        panel.Children.Add(bullet);

        var body = new TextBlock();
        body.TextWrapping = TextWrapping.Wrap;
        body.FontSize = 14;
        body.SetResourceReference(TextBlock.ForegroundProperty, KeyFg);
        AppendInlines(body.Inlines, text);
        panel.Children.Add(body);

        return panel;
    }

    // Build a blockquote: a quiet left border with indented Muted text.
    //
    // NOT Accent, and 1px rather than 3. A grey left border is the blockquote convention and
    // reads as typography; three pixels of the primary-action orange reads as a status rail on
    // a block, which is the shape the operator has objected to for months. Nothing rendered it
    // -- Md.Render still has no callers -- and that is exactly why it was worth fixing: dormant
    // code looks blessed, and whoever wires this up later would have shipped the violation
    // believing it had been reviewed.
    private static UIElement BuildBlockquote(string text)
    {
        var border = new Border();
        border.BorderThickness = new Thickness(1, 0, 0, 0);
        border.SetResourceReference(Border.BorderBrushProperty, KeyBorder);
        border.Padding = new Thickness(10, 2, 0, 2);
        border.Margin = new Thickness(0, 4, 0, 4);

        var tb = new TextBlock();
        tb.TextWrapping = TextWrapping.Wrap;
        tb.FontSize = 14;
        tb.SetResourceReference(TextBlock.ForegroundProperty, KeyMuted);
        AppendInlines(tb.Inlines, text);

        border.Child = tb;
        return border;
    }

    // Build a fenced code block: a rounded border with optional lang label and a code TextBox.
    private static UIElement BuildCodeBlock(string code, string lang)
    {
        var border = new Border();
        border.SetResourceReference(Border.BackgroundProperty, KeyCodeBg);
        border.SetResourceReference(Border.BorderBrushProperty, KeyBorder);
        border.BorderThickness = new Thickness(1);
        border.CornerRadius = new CornerRadius(8);
        border.Padding = new Thickness(10);
        border.Margin = new Thickness(0, 6, 0, 6);

        var stack = new StackPanel();
        stack.Orientation = Orientation.Vertical;

        if (!string.IsNullOrEmpty(lang))
        {
            var langLabel = new TextBlock();
            langLabel.Text = lang;
            langLabel.FontSize = 11;
            langLabel.Margin = new Thickness(0, 0, 0, 6);
            langLabel.SetResourceReference(TextBlock.ForegroundProperty, KeyMuted);
            stack.Children.Add(langLabel);
        }

        var box = new TextBox();
        box.Text = code;
        box.IsReadOnly = true;
        box.IsTabStop = false;
        box.BorderThickness = new Thickness(0);
        box.Background = Brushes.Transparent;
        box.FontFamily = new FontFamily("Cascadia Mono, Consolas");
        box.FontSize = 12.5;
        box.TextWrapping = TextWrapping.NoWrap;
        box.HorizontalScrollBarVisibility = ScrollBarVisibility.Auto;
        box.VerticalScrollBarVisibility = ScrollBarVisibility.Disabled;
        box.SetResourceReference(TextBox.ForegroundProperty, KeyFg);
        stack.Children.Add(box);

        border.Child = stack;
        return border;
    }

    // Parse inline formatting (**bold**, `code`) into the given Inlines collection.
    // Unmatched ** or ` are treated as literal text.
    private static void AppendInlines(InlineCollection inlines, string text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return;
        }

        var buffer = new System.Text.StringBuilder();
        int i = 0;
        int len = text.Length;

        while (i < len)
        {
            // Inline code span: `...`
            if (text[i] == '`')
            {
                int close = text.IndexOf('`', i + 1);
                if (close > i)
                {
                    FlushRun(inlines, buffer);
                    string codeText = text.Substring(i + 1, close - i - 1);
                    inlines.Add(BuildCodeRun(codeText));
                    i = close + 1;
                    continue;
                }
                // Unmatched backtick: literal.
                buffer.Append(text[i]);
                i++;
                continue;
            }

            // Bold span: **...**
            if (i + 1 < len && text[i] == '*' && text[i + 1] == '*')
            {
                int close = IndexOfDouble(text, i + 2);
                if (close > i)
                {
                    FlushRun(inlines, buffer);
                    string boldText = text.Substring(i + 2, close - (i + 2));
                    var bold = new Bold();
                    // Inner bold text may itself contain inline code.
                    AppendInlines(bold.Inlines, boldText);
                    inlines.Add(bold);
                    i = close + 2;
                    continue;
                }
                // Unmatched **: literal.
                buffer.Append("**");
                i += 2;
                continue;
            }

            buffer.Append(text[i]);
            i++;
        }

        FlushRun(inlines, buffer);
    }

    // Find the next "**" starting at index 'from'; returns its index or -1.
    private static int IndexOfDouble(string text, int from)
    {
        for (int j = from; j + 1 < text.Length; j++)
        {
            if (text[j] == '*' && text[j + 1] == '*')
            {
                return j;
            }
        }
        return -1;
    }

    // Emit buffered plain text as a Run, then clear the buffer.
    private static void FlushRun(InlineCollection inlines, System.Text.StringBuilder buffer)
    {
        if (buffer.Length == 0)
        {
            return;
        }
        var run = new Run(buffer.ToString());
        inlines.Add(run);
        buffer.Length = 0;
    }

    // Build a styled inline code Run with a hair space of padding feel.
    private static Run BuildCodeRun(string codeText)
    {
        // U+200A hair space around the code for a little breathing room.
        var run = new Run(" " + codeText + " ");
        run.FontFamily = new FontFamily("Cascadia Mono, Consolas");
        run.SetResourceReference(TextElement.BackgroundProperty, KeyCodeBg);
        return run;
    }
}
