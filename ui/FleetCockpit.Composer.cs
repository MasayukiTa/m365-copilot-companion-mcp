// FleetCockpit.Composer.cs -- the goal composer: input bar, slash commands, goal-command popup, folder/instruction prompts.
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

    TextBox _goalInput;
    Button _startBtn, _folderBtn;
    TextBlock _startNote;
    Border _composerBox;             // the rounded composer surface (themed in PaintChrome)
    TextBlock _composerHint;         // footer "one goal per line · / for commands"
    TextBlock _composerWatermark;    // placeholder shown when the textarea is empty

    // ④ task-injection: a bottom-docked composer (spec: agent-workspace, not a form). Type goals
    // (one per line) and launch a fleet. The big top textarea + right-side vertical button stack
    // are gone; the box now sits at the bottom with an inline footer (folder=secondary, start=primary).
    UIElement BuildInputBar()
    {
        _inBar = new Border();
        _inBar.Padding = new Thickness(Theme.PadApp, 0, Theme.PadApp, 8);   // was top 4 — remove the gap above the composer
        DockPanel.SetDock(_inBar, Dock.Bottom);

        _composerBox = new Border();
        _composerBox.CornerRadius = new CornerRadius(Theme.RadComposer);
        _composerBox.BorderThickness = new Thickness(0);   // frameless at rest; focus adds 1px accent
        _composerBox.Padding = new Thickness(12, 6, 12, 6); // was (12,8,12,8) — tighter to hug content
        // Floating Codex/Claude-Code style: soft shadow carries the separation, no hard frame.
        _composerBox.Effect = new System.Windows.Media.Effects.DropShadowEffect
        { BlurRadius = 14, ShadowDepth = 2, Opacity = 0.16, Color = System.Windows.Media.Color.FromRgb(0, 0, 0) };
        // Match the main chat composer: clicking the composer surface (including lower padding /
        // footer hint area) should focus the textarea. Do not mark handled so buttons still click.
        _composerBox.MouseLeftButtonDown += delegate { if (_goalInput != null) _goalInput.Focus(); };

        var col = new StackPanel();

        // ── textarea + watermark overlay (a Grid so they stack in the same cell) ──
        var taGrid = new Grid();
        _goalInput = new TextBox();
        // AN AUTOMATION ID, so a test can address this box by name instead of guessing.
        // Without one the only way to tell the composer from the history search box was
        // their WIDTH -- 1008 pixels against 160 -- which holds while a run is idle and
        // breaks the moment a run adds steer boxes to the worker cards. A guess that
        // works most of the time is the worst kind: it typed a goal into the right box
        // for a week and would have typed one into the search box eventually.
        System.Windows.Automation.AutomationProperties.SetAutomationId(_goalInput, "goalInput");
        _goalInput.AcceptsReturn = true; _goalInput.TextWrapping = TextWrapping.Wrap;
        _goalInput.MinHeight = 30; _goalInput.MaxHeight = 180;   // compact: ~30 (was 38), max 180 internal scroll
        _goalInput.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _goalInput.FontSize = Theme.FsBody;
        _goalInput.BorderThickness = new Thickness(0);            // the composer carries the border
        _goalInput.Background = Brushes.Transparent;
        _goalInput.Padding = new Thickness(2, 1, 2, 1);
        _goalInput.VerticalContentAlignment = VerticalAlignment.Top;
        BuildGoalCmdPopup();
        _goalInput.TextChanged += delegate { UpdateGoalCmdPopup(); };
        _goalInput.GotKeyboardFocus += delegate { PaintComposerFocus(true); };
        _goalInput.LostKeyboardFocus += delegate { PaintComposerFocus(false); };
        _goalInput.PreviewKeyDown += delegate (object s, KeyEventArgs e)
        {
            // slash-command autocomplete (like the main chat): navigate / accept / dismiss
            if (_gcmdPopup != null && _gcmdPopup.IsOpen && _gcmdList.Items.Count > 0)
            {
                if (e.Key == Key.Down) { _gcmdList.SelectedIndex = Math.Min(_gcmdList.SelectedIndex + 1, _gcmdList.Items.Count - 1); _gcmdList.ScrollIntoView(_gcmdList.SelectedItem); e.Handled = true; return; }
                if (e.Key == Key.Up) { _gcmdList.SelectedIndex = Math.Max(_gcmdList.SelectedIndex - 1, 0); _gcmdList.ScrollIntoView(_gcmdList.SelectedItem); e.Handled = true; return; }
                if (e.Key == Key.Tab || e.Key == Key.Return) { AcceptGoalCommand(); e.Handled = true; return; }
                if (e.Key == Key.Escape) { _gcmdPopup.IsOpen = false; e.Handled = true; return; }
            }
            if (e.Key == Key.Return && (Keyboard.Modifiers & ModifierKeys.Control) != 0)
            {
                e.Handled = true;
                // Ctrl+Enter adds tasks to the live run; starts a new fleet when idle.
                //
                // AND A SLASH SETTING IS A SETTING HERE TOO. The send button below already
                // calls HandleSlashSetting first, with a note saying the bug was found by
                // typing a slash setting into the real window and finding settings.txt unchanged
                // while the running worker had been handed the text. That fix went to the
                // button and stopped there -- this path, which is the one every keyboard user
                // and scripts/win/submit_via_ui.ps1 take, kept the original behaviour.
                //
                // The same fault reached by two callers is one fault; fixing the caller you
                // happened to be looking at leaves it live everywhere else.
                if (HandleSlashSetting()) return;
                if (_composerRunActive) TryAddGoalsToActiveRun();
                else StartFleet();
            }
        };
        taGrid.Children.Add(_goalInput);
        // short placeholder (spec Fleet: "タスクを入力..."); the "one per line / slash" guidance
        // moves to the footer hint so the box itself stays quiet.
        _composerWatermark = new TextBlock
        {
            Text = _lang == 0 ? "タスクを入力..." : "Add tasks...",
            IsHitTestVisible = false, FontSize = Theme.FsBody,
            Margin = new Thickness(4, 1, 4, 0), VerticalAlignment = VerticalAlignment.Top,
            TextTrimming = TextTrimming.CharacterEllipsis
        };
        taGrid.Children.Add(_composerWatermark);
        _goalInput.TextChanged += delegate { _composerWatermark.Visibility = string.IsNullOrEmpty(_goalInput.Text) ? Visibility.Visible : Visibility.Collapsed; };
        col.Children.Add(taGrid);

        // ── footer: left hint, right button cluster (folder=secondary, start=primary) ──
        var footer = new DockPanel { LastChildFill = false };
        footer.Margin = new Thickness(0, 6, 0, 0);  // was 8 -- tighter footer gap

        var btns = new StackPanel { Orientation = Orientation.Horizontal };
        _folderBtn = new Button();
        _folderBtn.Cursor = Cursors.Hand; _folderBtn.BorderThickness = new Thickness(1);
        _folderBtn.Height = Theme.BtnH; _folderBtn.FontSize = 12;
        _folderBtn.Padding = new Thickness(12, 0, 12, 0);
        _folderBtn.Click += delegate { FolderToGoals(); };
        btns.Children.Add(_folderBtn);
        _startBtn = new Button();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_startBtn, "startButton");
        _startBtn.Cursor = Cursors.Hand; _startBtn.BorderThickness = new Thickness(0);
        _startBtn.Height = Theme.BtnH; _startBtn.MinWidth = 132; _startBtn.FontWeight = FontWeights.SemiBold;
        _startBtn.Margin = new Thickness(8, 0, 0, 0); _startBtn.Padding = new Thickness(16, 0, 16, 0);
        // When a run is active, the primary button adds tasks to that run.
        _startBtn.Click += delegate
        {
            // A SETTING IS NEVER A TASK. Slash settings are consumed locally before the primary
            // action is chosen. With an active run, ordinary text is durably added to that run;
            // when idle, ordinary text starts a new run. Worker-specific steering lives on each
            // worker card, so `/fanout`, `/effort`, and `/approval` must never enter either task
            // path as Copilot instructions.
            if (HandleSlashSetting()) return;
            if (_composerRunActive) TryAddGoalsToActiveRun();
            else StartFleet();
        };
        btns.Children.Add(_startBtn);
        DockPanel.SetDock(btns, Dock.Right);
        footer.Children.Add(btns);

        // "/" affordance button on the left of the footer hint
        var slashBtn = new Button();
        slashBtn.Content = "/"; slashBtn.FontSize = 13; slashBtn.FontWeight = FontWeights.SemiBold;
        slashBtn.Foreground = Muted;   // match the footer hint's muted tone (was defaulting to dark Fg)
        slashBtn.Cursor = Cursors.Hand;
        slashBtn.BorderThickness = new Thickness(0); slashBtn.Background = Brushes.Transparent;
        slashBtn.Padding = new Thickness(4, 0, 6, 0); slashBtn.VerticalAlignment = VerticalAlignment.Center;
        slashBtn.ToolTip = _lang == 0 ? "スラッシュコマンドを入力" : "Type a slash command";
        slashBtn.Template = FlatButtonTemplate();
        slashBtn.Click += delegate
        {
            if (_goalInput == null) return;
            // Insert "/" at current line start (or just append if line non-empty without "/")
            string txt = _goalInput.Text ?? "";
            int caret = _goalInput.CaretIndex;
            if (caret > txt.Length) caret = txt.Length;
            int ls = caret > 0 ? txt.LastIndexOf('\n', caret - 1) + 1 : 0;
            string line = txt.Substring(ls, caret - ls);
            if (line.Length == 0 || line[0] != '/')
            {
                _goalInput.Text = txt.Substring(0, ls) + "/" + txt.Substring(ls);
                _goalInput.CaretIndex = ls + 1;
            }
            _goalInput.Focus();
        };
        DockPanel.SetDock(slashBtn, Dock.Left);
        footer.Children.Add(slashBtn);

        _composerHint = new TextBlock
        {
            Text = _lang == 0 ? "1行に1ゴール（複数可） ·「/」でコマンド" : "One goal per line · \"/\" for commands",
            FontSize = Theme.FsMeta, VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis
        };
        footer.Children.Add(_composerHint);   // no Dock -> fills the left
        col.Children.Add(footer);

        // transient start feedback ("Started (N goals)" / errors), below the footer
        _startNote = new TextBlock();
        _startNote.FontSize = Theme.FsMeta; _startNote.Margin = new Thickness(2, 6, 0, 0);
        _startNote.TextWrapping = TextWrapping.Wrap;
        col.Children.Add(_startNote);

        _composerBox.Child = col;
        _inBar.Child = _composerBox;
        return _inBar;
    }
    Border _inBar;

    // Focus ring: composer is frameless at rest (BorderThickness=0). On focus, add a 1px
    // accent outline so keyboard users get a clear indicator without re-introducing the boxy look.
    void PaintComposerFocus(bool focused)
    {
        if (_composerBox == null) return;
        if (focused)
        {
            _composerBox.BorderThickness = new Thickness(1);
            _composerBox.BorderBrush = Theme.Br(Theme.Accent(_dark));
        }
        else
        {
            _composerBox.BorderThickness = new Thickness(0);
            _composerBox.BorderBrush = System.Windows.Media.Brushes.Transparent;
        }
    }


    // A slash SETTING typed into the composer: apply it, clear the box, and report it.
    // Returns true when the text was a setting and has been dealt with.
    //
    // Shared by the send button and StartFleet so there is one place that decides what a
    // slash command means. There used to be only the StartFleet copy, which sat behind two
    // earlier returns -- the "a run is already going" refusal, and the button's own
    // start-vs-steer branch -- so during a run these were handed to Copilot as instructions
    // and never applied. A setting must never become a message.
    bool HandleSlashSetting()
    {
        if (_goalInput == null) return false;
        string g0 = (_goalInput.Text ?? "").Trim();
        if (!g0.StartsWith("/")) return false;
        if (g0.IndexOf('\n') >= 0) return false;      // several lines is several goals, not a command

        bool handled = false;
        if (g0.StartsWith("/effort ", StringComparison.OrdinalIgnoreCase))
        {
            string v = g0.Substring(8).Trim().ToLower();
            if (v == "min" || v == "max" || v == "ultra" || v == "auto")
            {
                _effort = v; SaveKey("effort", _effort); PaintEffort();
                if (_startNote != null)
                    _startNote.Text = (_lang == 0 ? "推論モード→ " : "Effort set to ") + _effort;
                handled = true;
            }
        }
        else if (g0.StartsWith("/approval ", StringComparison.OrdinalIgnoreCase))
        {
            string v = g0.Substring(10).Trim().ToLower();
            if (v == "run" || v == "plan" || v == "auto")
            {
                _approval = v; SaveKey("approval", _approval); PaintApproval();
                if (_startNote != null)
                    _startNote.Text = (_lang == 0 ? "実行方式→ " : "Approval mode set to ") + _approval;
                handled = true;
            }
        }
        else if (g0.StartsWith("/runtime ", StringComparison.OrdinalIgnoreCase))
        {
            string v = g0.Substring(9).Trim().ToLower();
            if (v == "fleet" || v == "durable")
            {
                _runtimeMode = v;
                SaveKey("runtime", _runtimeMode);
                if (_startNote != null)
                    _startNote.Text = _lang == 0
                        ? (_runtimeMode == "durable" ? "次のタスクは長時間実行モードで開始します。" : "次のタスクは従来Fleetで開始します。")
                        : (_runtimeMode == "durable" ? "Next task will use the durable runtime." : "Next task will use classic Fleet.");
                handled = true;
            }
        }
        else if (g0.StartsWith("/fanout", StringComparison.OrdinalIgnoreCase))
        {
            string v = g0.Length > 7 ? g0.Substring(7).Trim().ToLower() : "";
            if (v == "on" || v == "off")
            {
                _fanout = (v == "on");
                SaveKey("fanout", _fanout ? "on" : "off");
                PaintFanout();
                if (_startNote != null)
                    _startNote.Text = _lang == 0
                        ? (_fanout ? "分割実行 ON — 長い依頼を独立したサブタスクに分けて並列実行し、結果を統合します。"
                                   : "分割実行 OFF — 依頼は 1 つの会話で実行します。")
                        : (_fanout ? "Fan-out ON — long goals are split, run in parallel and merged."
                                   : "Fan-out OFF — a goal runs in one conversation.");
                handled = true;
            }
        }
        if (handled) _goalInput.Text = "";
        return handled;
    }

    // Paint the composer as task intake in both states; a live run changes Start -> Add.
    // Only repaints when the mode actually changes (keyed off _composerRunActive).
    void PaintComposerMode(bool runActive)
    {
        if (_composerRunActive == runActive) return;
        _composerRunActive = runActive;

        bool ja = _lang == 0;
        if (runActive)
        {
            // Active-run mode: add new tasks to the existing queue.
            if (_composerWatermark != null)
                _composerWatermark.Text = ja ? "タスクを追加..." : "Add tasks...";
            if (_composerHint != null)
                _composerHint.Text = ja
                    ? "実行中のキューに追加 · 割り込みは各タスクカードから · / でコマンド"
                    : "adds to the active run · steer from a task card · '/' for commands";
            if (_startBtn != null)
            {
                _startBtn.Content = ja ? "追加" : "Add";
                _startBtn.Background = AccentFill;
                _startBtn.Foreground = AccentFg;
            }
            if (_folderBtn != null)
            {
                _folderBtn.Visibility = Visibility.Visible;
            }
        }
        else
        {
            // Idle mode: add goals / start.
            if (_composerWatermark != null)
                _composerWatermark.Text = ja ? "タスクを追加..." : "Add tasks...";
            if (_composerHint != null)
                _composerHint.Text = ja
                    ? "1行に1ゴール（複数可） · / でコマンド"
                    : "One goal per line · '/' for commands";
            if (_startBtn != null)
            {
                _startBtn.Content = T("start");
                _startBtn.Background = AccentFill;
                _startBtn.Foreground = AccentFg;
            }
            if (_folderBtn != null)
            {
                _folderBtn.Visibility = Visibility.Visible;
            }
        }
    }

    // --- slash-command autocomplete for the goal box (parity with the main chat) ---
    Popup _gcmdPopup; ListBox _gcmdList;
    Popup _helpPopup;   // /help text shown HERE (a compact popup) instead of dumped into the goal box
    static readonly string[][] _goalCommandsJa = {
        new[]{"/help","コマンド一覧を表示"},
        new[]{"/code","<機能> を実装し、pytest テストも書いて通す"},
        new[]{"/fix","<ファイル> の <不具合> を直し、テストを通す"},
        new[]{"/test","<対象> の pytest テストを書く"},
        new[]{"/refactor","<対象> を読みやすくリファクタする(挙動は変えない)"},
        new[]{"/doc","<対象> の README/説明 を書く"},
        new[]{"/review","<対象> をレビューして問題点を箇条書きで挙げる"},
        new[]{"/research","<問い> を Claude で深掘り調査する"},
        new[]{"/effort","推論モードを設定: min|max|ultra|auto"},
        new[]{"/approval","実行方式を設定: run|plan|auto（互換コマンド）"},
        new[]{"/fanout","分割実行: on|off（長い依頼を分けて並列実行し統合）"},
        new[]{"/runtime","実行基盤: durable|fleet（長時間実行 / 従来Fleet）"},
    };
    static readonly string[][] _goalCommandsEn = {
        new[]{"/help","Show the command list"},
        new[]{"/code","implement <feature> and write + pass pytest tests"},
        new[]{"/fix","fix <bug> in <file> and make the tests pass"},
        new[]{"/test","write pytest tests for <target>"},
        new[]{"/refactor","refactor <target> for readability (no behavior change)"},
        new[]{"/doc","write the README / docs for <target>"},
        new[]{"/review","review <target> and list the issues as bullets"},
        new[]{"/research","deep-research <question> with Claude"},
        new[]{"/effort","set reasoning mode: min|max|ultra|auto"},
        new[]{"/approval","set approval mode: run|plan|auto"},
        new[]{"/fanout","split a long goal, run the parts in parallel, merge: on|off"},
        new[]{"/runtime","execution runtime: durable|fleet"},
    };
    // Localized at access time so the slash palette (and the template it inserts) follows the UI language.
    string[][] _goalCommands { get { return _lang == 0 ? _goalCommandsJa : _goalCommandsEn; } }
    void BuildGoalCmdPopup()
    {
        _gcmdList = new ListBox { MaxHeight = 220, BorderThickness = new Thickness(0) };
        _gcmdList.Background = BtnBg;
        _gcmdList.PreviewMouseLeftButtonUp += delegate { AcceptGoalCommand(); };
        var border = new Border { Child = _gcmdList, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(Theme.RadCard), Padding = new Thickness(4) };
        border.Background = BtnBg; border.BorderBrush = Accent;
        _gcmdPopup = new Popup { PlacementTarget = _goalInput, Placement = PlacementMode.Top, StaysOpen = false, Width = 520 };
        _gcmdPopup.Child = border;
    }
    ListBoxItem MakeGCmdItem(string name, string template)
    {
        var sp = new StackPanel { Orientation = Orientation.Horizontal };
        var n = new TextBlock { Text = name, FontWeight = FontWeights.SemiBold, MinWidth = 90, Foreground = Accent };
        var d = new TextBlock { Text = template, Margin = new Thickness(8, 0, 0, 0), Foreground = Muted, TextTrimming = TextTrimming.CharacterEllipsis };
        sp.Children.Add(n); sp.Children.Add(d);
        return new ListBoxItem { Content = sp, Tag = template, Padding = new Thickness(6, 4, 6, 4), Foreground = Fg };
    }
    void CurrentGoalLine(out int lineStart, out string line)
    {
        string txt = _goalInput.Text ?? ""; int caret = _goalInput.CaretIndex;
        if (caret > txt.Length) caret = txt.Length;
        lineStart = caret > 0 ? txt.LastIndexOf('\n', caret - 1) + 1 : 0;
        line = txt.Substring(lineStart, caret - lineStart);
    }
    void UpdateGoalCmdPopup()
    {
        try
        {
            int ls; string line; CurrentGoalLine(out ls, out line);
            if (line.Length >= 1 && line[0] == '/' && line.IndexOf(' ') < 0)
            {
                string pre = line.ToLower();
                _gcmdList.Items.Clear();
                foreach (var c in _goalCommands)
                    if (c[0].StartsWith(pre)) _gcmdList.Items.Add(MakeGCmdItem(c[0], c[1]));
                if (_gcmdList.Items.Count > 0) { _gcmdList.SelectedIndex = 0; _gcmdPopup.IsOpen = true; }
                else _gcmdPopup.IsOpen = false;
            }
            else if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
        }
        catch (Exception) { }
    }
    void AcceptGoalCommand()
    {
        if (_gcmdList.SelectedItem == null && _gcmdList.Items.Count > 0) _gcmdList.SelectedIndex = 0;
        var item = _gcmdList.SelectedItem as ListBoxItem;
        if (item != null)
        {
            string template = item.Tag as string;
            var sp = item.Content as StackPanel;
            string cmdName = (sp != null && sp.Children.Count > 0 && sp.Children[0] is TextBlock)
                ? ((TextBlock)sp.Children[0]).Text : "";

            // /help: show in a compact popup, NOT dumped into the goal box (which would
            // bury the card list). Remove the typed "/help" line and surface the popup.
            if (cmdName == "/help")
            {
                int lsh; string lineh; CurrentGoalLine(out lsh, out lineh);
                string txth = _goalInput.Text ?? "";
                int lineEndH = txth.IndexOf('\n', lsh);
                if (lineEndH < 0) lineEndH = txth.Length;
                _goalInput.Text = txth.Substring(0, lsh) + txth.Substring(lineEndH);
                _goalInput.CaretIndex = lsh;
                if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
                ShowGoalHelp();
                return;
            }

            // /effort and /approval: if the current line has an argument, apply it immediately
            // and clear the line. If no arg, insert the template (prompts for value) instead.
            if (cmdName == "/effort" || cmdName == "/approval" || cmdName == "/runtime")
            {
                int ls2; string line2; CurrentGoalLine(out ls2, out line2);
                // line2 looks like "/effort" or "/effort max"
                string[] parts = line2.Trim().Split(new char[]{' '}, 2, StringSplitOptions.RemoveEmptyEntries);
                if (parts.Length >= 2)
                {
                    string argVal = parts[1].Trim().ToLower();
                    bool applied = false;
                    if (cmdName == "/effort")
                    {
                        if (argVal == "min" || argVal == "max" || argVal == "ultra" || argVal == "auto")
                        {
                            _effort = argVal;
                            SaveKey("effort", _effort);
                            PaintEffort();
                            applied = true;
                            if (_startNote != null) _startNote.Text = (_lang == 0 ? "推論モード→ " : "Effort set to ") + _effort;
                        }
                    }
                    else if (cmdName == "/approval")
                    {
                        if (argVal == "run" || argVal == "plan" || argVal == "auto")
                        {
                            _approval = argVal;
                            SaveKey("approval", _approval);
                            PaintApproval();
                            applied = true;
                            if (_startNote != null) _startNote.Text = (_lang == 0 ? "実行方式→ " : "Run mode set to ") + _approval;
                        }
                    }
                    else  // /runtime
                    {
                        if (argVal == "fleet" || argVal == "durable")
                        {
                            _runtimeMode = argVal;
                            SaveKey("runtime", _runtimeMode);
                            applied = true;
                            if (_startNote != null)
                                _startNote.Text = _lang == 0
                                    ? (_runtimeMode == "durable" ? "次のタスクは長時間実行モードで開始します。" : "次のタスクは従来Fleetで開始します。")
                                    : (_runtimeMode == "durable" ? "Next task will use the durable runtime." : "Next task will use classic Fleet.");
                        }
                    }
                    if (applied)
                    {
                        // Remove the slash-command line from the input
                        string txt2 = _goalInput.Text ?? "";
                        int caret2 = _goalInput.CaretIndex;
                        if (caret2 > txt2.Length) caret2 = txt2.Length;
                        // Find end of the line (up to next \n or end of string)
                        int lineEnd = txt2.IndexOf('\n', ls2);
                        if (lineEnd < 0) lineEnd = txt2.Length;
                        _goalInput.Text = txt2.Substring(0, ls2) + txt2.Substring(lineEnd);
                        _goalInput.CaretIndex = ls2;
                        if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
                        _goalInput.Focus();
                        return;
                    }
                }
                // No valid arg: insert template so user can type the value
                // template is the description string — instead insert the command name + space
                int ls3; string line3; CurrentGoalLine(out ls3, out line3);
                string txt3 = _goalInput.Text ?? ""; int caret3 = _goalInput.CaretIndex;
                if (caret3 > txt3.Length) caret3 = txt3.Length;
                string insert = cmdName + " ";
                _goalInput.Text = txt3.Substring(0, ls3) + insert + txt3.Substring(caret3);
                _goalInput.CaretIndex = ls3 + insert.Length;
                if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
                _goalInput.Focus();
                return;
            }

            // Default: replace current line with the template text
            int ls; string line; CurrentGoalLine(out ls, out line);
            string txt = _goalInput.Text ?? ""; int caret = _goalInput.CaretIndex;
            if (caret > txt.Length) caret = txt.Length;
            _goalInput.Text = txt.Substring(0, ls) + template + txt.Substring(caret);
            _goalInput.CaretIndex = ls + template.Length;
        }
        if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
        _goalInput.Focus();
    }
    string GoalHelpText()
    {
        // Structured (short lines, each wraps cleanly) instead of one long paragraph whose tail
        // was clipped at the popup's right edge. Commands, reasoning and approval are split out so
        // no single line runs long enough to need the old fixed 520px width.
        if (_lang != 0)
            return "Commands\n"
                + "/code <feature> - implement + pytest tests\n"
                + "/fix <target> - fix a bug + verify\n"
                + "/test <target> - add pytest tests\n"
                + "/refactor <target> - tidy without changing behavior\n"
                + "/doc <target> - write README / docs\n"
                + "/review <target> - review and list issues\n"
                + "/research <question> - deep research\n"
                + "/runtime durable|fleet - choose long-running or classic runtime\n"
                + "\nReasoning (top bar)\n"
                + "min - fastest, least reasoning\n"
                + "max - deepest reasoning\n"
                + "ultra - max + extra checks\n"
                + "auto - pick per task\n"
                + "\nRun mode (top bar)\n"
                + "run - run now\n"
                + "plan - wait for plan approval\n"
                + "auto - plain fleet waits for plan approval\n"
                + "(folder autonomy uses GO / ASK / STOP)";
        return "コマンド\n"
            + "/code <機能> - 実装と pytest テスト\n"
            + "/fix <対象> - 不具合修正と検証\n"
            + "/test <対象> - pytest テスト追加\n"
            + "/refactor <対象> - 挙動を変えず整理\n"
            + "/doc <対象> - README/説明を書く\n"
            + "/review <対象> - 問題点レビュー\n"
            + "/research <問い> - 深掘り調査\n"
            + "\n推論（上部設定）\n"
            + "min - 最速・推論最小\n"
            + "max - 最も深い推論\n"
            + "ultra - max + 追加チェック\n"
            + "auto - タスクに応じ自動\n"
            + "\n実行方式（上部設定）\n"
            + "run - 即実行\n"
            + "plan - 計画承認待ち\n"
            + "auto - 通常フリートは計画承認待ち\n"
            + "（自律コーディング(フォルダ)は GO / ASK / STOP）";
    }

    // /help must NOT be dumped into the goal box (it turns the input into a giant scroll
    // region that covers the card list). Show it in a compact, dismissable popup anchored
    // to the goal input instead. Reuses the same popup styling as the slash palette.
    void ShowGoalHelp()
    {
        try
        {
            if (_goalInput == null) return;
            // Close every other floating popup/dropdown first (header settings/overflow/effort/
            // approval + the slash palette) so help is the only one showing; keep help itself open.
            CloseHeaderPopups("help");

            var txt = new TextBlock();
            txt.Text = GoalHelpText();
            txt.TextWrapping = TextWrapping.Wrap;
            txt.Foreground = Fg;
            txt.FontSize = 12;
            txt.LineHeight = 18;

            var sv = new ScrollViewer();
            sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
            sv.MaxHeight = 260;
            sv.Content = txt;

            var border = new Border();
            border.Child = sv;
            border.BorderThickness = new Thickness(1);
            border.CornerRadius = new CornerRadius(Theme.RadCard);
            border.Padding = new Thickness(12, 12, 12, 12);
            border.Background = BtnBg;
            border.BorderBrush = Accent;

            // No fixed Width (the old 520 clipped the long approval line at the right edge). Let the
            // border size to content within a min/max band; each line wraps (txt.TextWrapping=Wrap).
            border.MinWidth = 360;
            border.MaxWidth = 560;
            if (_helpPopup == null)
                _helpPopup = new Popup { PlacementTarget = _goalInput, Placement = PlacementMode.Top, StaysOpen = false };
            _helpPopup.Child = border;
            _helpPopup.IsOpen = true;
        }
        catch (Exception) { }
    }

    // ③ Claude-Code-style: pick a folder + a plain-language instruction -> code_task runs
    // it as ONE self-verifying task. It auto-detects how to verify (pytest if there is a
    // test suite, else compile; npm test for Node) and only accepts DONE once that
    // actually passes. No per-file fan-out, no goals to review -- you say it, it runs.
    void FolderToGoals()
    {
        try
        {
            // Modern Explorer-style picker (the old WinForms tree dialog is clunky): the
            // user browses to the target folder and picks ANY file in it; we use its
            // parent folder. Reliable + no COM.
            var ofd = new Microsoft.Win32.OpenFileDialog();
            ofd.Title = _lang == 0 ? "対象フォルダ内の任意のファイルを選択（その親フォルダが対象になります）"
                                   : "Pick ANY file inside the target folder (its parent folder is used)";
            ofd.Filter = _lang == 0 ? "すべてのファイル|*.*" : "All files|*.*";
            ofd.CheckFileExists = true;
            if (ofd.ShowDialog() != true) return;
            string folder = Path.GetDirectoryName(ofd.FileName);
            if (string.IsNullOrEmpty(folder)) return;
            string instr = PromptInstruction();
            if (string.IsNullOrEmpty(instr)) return;

            // Autonomy Contract pre-flight (folder path only — quick Start is unchanged).
            if (!ShowAutonomyContract(folder, instr)) return;

            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            string stateDir = Path.GetDirectoryName(_statusPath);

            // code_task: natural language in, auto-verified autonomous run out. Writes the
            // same status.json this cockpit already tails, so the task shows up live.
            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            if (_approval == "auto")
                psi.Arguments = "-m relay.overnight_task -i \"" + instr.Replace("\"", "'")
                    + "\" -f \"" + folder + "\" --state-dir \"" + stateDir + "\""
                    + " --hours 8 --auto-mode --effort " + _effort;
            else
                psi.Arguments = "-m relay.code_task -i \"" + instr.Replace("\"", "'")
                    + "\" -f \"" + folder + "\" --state-dir \"" + stateDir + "\" --effort " + _effort
                    + (_approval == "plan" ? " --plan" : "");
            psi.WorkingDirectory = repo; psi.UseShellExecute = false; psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }

            var p = new System.Diagnostics.Process();
            p.StartInfo = psi;
            p.Start();
            _startNote.Text = _lang == 0
                ? (_approval == "auto" ? "自動承認ゲート付き長時間タスクを開始。ASK/STOPなら実行せずレポートします。"
                   : _approval == "plan" ? "コーディング(計画モード)を開始。計画提示後「承認待ち」になります。カードに承認/修正を steer で送ってください。"
                   : "コーディングタスクを開始（検証方法は自動検出。テスト/コンパイルが通るまで完了しません）。")
                : (_approval == "auto" ? "Started long task with auto approval gate. ASK/STOP writes a report without launching."
                   : _approval == "plan" ? "Coding (plan mode) started -> it will pause at 承認待ち; steer the card to approve/edit."
                   : "Coding task started (auto-verified; not done until tests/compile pass).");
        }
        catch (Exception ex)
        {
            _startNote.Text = (_lang == 0 ? "フォルダ処理失敗: " : "Folder error: ") + ex.Message;
        }
    }

    string PromptInstruction()
    {
        var w = new Window();
        w.Title = _lang == 0 ? "フォルダへの指示" : "Instruction";
        w.Width = 480; w.Height = 230; w.Background = Bg;
        w.WindowStartupLocation = WindowStartupLocation.CenterOwner; w.Owner = this;
        var sp = new StackPanel(); sp.Margin = new Thickness(16);
        var lbl = new TextBlock();
        lbl.Text = _lang == 0 ? "各ファイルに適用する指示（編集はMCPツールで実行されます）:"
                              : "Instruction applied to each file (edits run via MCP tools):";
        lbl.Foreground = Fg; lbl.TextWrapping = TextWrapping.Wrap; lbl.Margin = new Thickness(0, 0, 0, 8);
        sp.Children.Add(lbl);
        var tb = new TextBox();
        tb.MinHeight = 64; tb.AcceptsReturn = true; tb.TextWrapping = TextWrapping.Wrap;
        tb.Background = BtnBg; tb.Foreground = Fg; tb.BorderBrush = Border;
        tb.BorderThickness = new Thickness(1); tb.Padding = new Thickness(8); tb.CaretBrush = Fg;
        sp.Children.Add(tb);
        var btns = new StackPanel(); btns.Orientation = Orientation.Horizontal;
        btns.HorizontalAlignment = HorizontalAlignment.Right; btns.Margin = new Thickness(0, 12, 0, 0);
        string[] box = new string[1];
        var ok = new Button();
        ok.Content = _lang == 0 ? "生成" : "Generate"; ok.IsDefault = true;
        ok.Background = AccentFill; ok.Foreground = AccentFg; ok.BorderThickness = new Thickness(0);
        ok.Padding = new Thickness(16, 4, 16, 4); ok.Cursor = Cursors.Hand; ok.FontWeight = FontWeights.SemiBold;
        ok.Click += delegate { box[0] = tb.Text; w.DialogResult = true; };
        var cancel = new Button();
        cancel.Content = _lang == 0 ? "取消" : "Cancel"; cancel.IsCancel = true;
        cancel.Margin = new Thickness(8, 0, 0, 0); cancel.Padding = new Thickness(16, 4, 16, 4);
        cancel.Cursor = Cursors.Hand; cancel.Background = BtnBg; cancel.Foreground = Fg;
        cancel.BorderBrush = Border; cancel.BorderThickness = new Thickness(1);
        btns.Children.Add(ok); btns.Children.Add(cancel);
        sp.Children.Add(btns);
        w.Content = sp;
        bool? r = w.ShowDialog();
        return (r == true) ? box[0] : null;
    }

    // Small modal text-input dialog for the "続ける" (Continue) follow-up instruction on a
    // finished history row. Modeled on PromptInstruction(): same Window pattern, Owner=this,
    // theme brushes, OK+Cancel. Returns the typed string, or null on empty/cancel.
    string PromptFollowup()
    {
        var w = new Window();
        w.Title = _lang == 0 ? "続けて指示する" : "Continue this task";
        w.Width = 480; w.Height = 230; w.Background = Bg;
        w.WindowStartupLocation = WindowStartupLocation.CenterOwner; w.Owner = this;
        var sp = new StackPanel(); sp.Margin = new Thickness(16);
        var lbl = new TextBlock();
        lbl.Text = _lang == 0 ? "前回の成果物を踏まえて追加で何をさせますか？"
                              : "Follow-up instruction (uses the prior task's saved outputs):";
        lbl.Foreground = Fg; lbl.TextWrapping = TextWrapping.Wrap; lbl.Margin = new Thickness(0, 0, 0, 8);
        sp.Children.Add(lbl);
        var tb = new TextBox();
        tb.MinHeight = 64; tb.AcceptsReturn = true; tb.TextWrapping = TextWrapping.Wrap;
        tb.Background = BtnBg; tb.Foreground = Fg; tb.BorderBrush = Border;
        tb.BorderThickness = new Thickness(1); tb.Padding = new Thickness(8); tb.CaretBrush = Fg;
        sp.Children.Add(tb);
        var btns = new StackPanel(); btns.Orientation = Orientation.Horizontal;
        btns.HorizontalAlignment = HorizontalAlignment.Right; btns.Margin = new Thickness(0, 12, 0, 0);
        string[] box = new string[1];
        var ok = new Button();
        ok.Content = _lang == 0 ? "続ける" : "Continue"; ok.IsDefault = true;
        ok.Background = AccentFill; ok.Foreground = AccentFg; ok.BorderThickness = new Thickness(0);
        ok.Padding = new Thickness(16, 4, 16, 4); ok.Cursor = Cursors.Hand; ok.FontWeight = FontWeights.SemiBold;
        ok.Click += delegate { box[0] = tb.Text; w.DialogResult = true; };
        var cancel = new Button();
        cancel.Content = _lang == 0 ? "取消" : "Cancel"; cancel.IsCancel = true;
        cancel.Margin = new Thickness(8, 0, 0, 0); cancel.Padding = new Thickness(16, 4, 16, 4);
        cancel.Cursor = Cursors.Hand; cancel.Background = BtnBg; cancel.Foreground = Fg;
        cancel.BorderBrush = Border; cancel.BorderThickness = new Thickness(1);
        btns.Children.Add(ok); btns.Children.Add(cancel);
        sp.Children.Add(btns);
        w.Content = sp;
        bool? r = w.ShowDialog();
        if (r != true) return null;
        string txt = box[0];
        if (txt == null || txt.Trim().Length == 0) return null;
        return txt;
    }

    // The follow-up as it should be SENT, given whether the old conversation is being reopened.
    //
    // WHEN THERE IS AN ID, SEND THE FOLLOW-UP AND NOTHING ELSE. The caller already puts
    // `resume_conv` on the goal, so the worker continues the real conversation and the server
    // still holds every prior turn -- pasting the prior goal on top of that is not context, it
    // is a second copy of context the conversation already has. And it compounds: `priorGoal` is
    // the previously ASSEMBLED goal, so each continuation swallows the last one whole. Measured
    // on screen 2026-09-14: a follow-up of one sentence arrived as a wall containing the
    // original goal, an earlier follow-up, a five-step editing procedure and a verification
    // request. At that size no instruction can be tested -- the operator's rule is that anything
    // outside bench measurement and coding goes in the length a person types, and the machinery
    // was making that impossible.
    //
    // The paste below is the FALLBACK, for a row with no conversation id, and the comment it
    // replaces stated its own premise: "There is NO stable reopenable URL for this agent". There
    // is one now. Every fleet transcript carries a guid line and ContextTokenLimitExceeded on a
    // resumed conversation is the proof that the server keeps the history.
    string ContinueText(string priorGoal, string followup, string conversationId)
    {
        return string.IsNullOrEmpty(conversationId)
            ? BuildContinueGoal(priorGoal, followup)
            : followup;
    }

    // Build the FRESH goal text for a continuation that has NO conversation to reopen: prepend
    // the prior task's context and tell the agent to re-read its on-disk outputs first.
    string BuildContinueGoal(string priorGoal, string followup)
    {
        if (_lang == 0)
        {
            return "【前回タスクの続き】\n前回のゴール: " + priorGoal
                + "\n前回の成果物はディスク上に保存済み（このタスクで作成・更新したファイル/フォルダ）。まず関連するファイル/フォルダを list_directory / read_file で読み直して前回の文脈と成果物を把握してから、次の追加指示を実行して。完了したら最後の行に DONE。\n追加指示: "
                + followup;
        }
        return "[Continuation of a prior task]\nPrior goal: " + priorGoal
            + "\nThe prior task's outputs are saved on disk (files/folders this task created or updated). First re-read the relevant files/folders with list_directory / read_file to recover the prior context and outputs, then carry out the follow-up instruction below. When finished, put DONE on the last line.\nFollow-up: "
            + followup;
    }

    // ── Autonomy Contract pre-flight dialog (folder autonomous path only) ─────────
    // Shows a modal summary of what the agent will and will not do before the run
    // launches. Returns true if the user clicks Delegate, false if they Cancel.
    // REAL fields are shown plainly; FUTURE/unimplemented items are labeled with ⚠.
    bool ShowAutonomyContract(string folder, string instruction)
    {
        bool ja = _lang == 0;

        var w = new Window();
        w.Title = ja ? "自律委任 — 確認" : "Autonomy Contract";
        w.Width = 540;
        w.SizeToContent = SizeToContent.Height;
        w.WindowStartupLocation = WindowStartupLocation.CenterOwner;
        w.Owner = this;
        w.Background = Theme.Br(Theme.Surface(_dark));
        w.ResizeMode = ResizeMode.NoResize;

        var outer = new StackPanel();
        outer.Margin = new Thickness(0);

        // ── Title bar band ────────────────────────────────────────────────────────
        var titleBand = new Border();
        titleBand.Background = Theme.Br(Theme.Bg(_dark));
        titleBand.BorderBrush = Theme.Br(Theme.Border(_dark));
        titleBand.BorderThickness = new Thickness(0, 0, 0, 1);
        titleBand.Padding = new Thickness(24, 16, 24, 16);
        var titleTb = new TextBlock();
        titleTb.Text = ja ? "AUTONOMY CONTRACT / 自律委任" : "AUTONOMY CONTRACT / 自律委任";
        titleTb.FontSize = 11;
        titleTb.FontWeight = FontWeights.SemiBold;
        titleTb.Foreground = Theme.Br(Theme.Muted(_dark));
        titleBand.Child = titleTb;
        outer.Children.Add(titleBand);

        // ── Body ──────────────────────────────────────────────────────────────────
        var body = new StackPanel();
        body.Margin = new Thickness(24, 20, 24, 8);

        // Helper: section label (small-caps style via all-upper + Muted + small font)
        // C#5: no local methods, use a Func<> delegate
        Func<string, TextBlock> makeSectionLabel = delegate(string txt)
        {
            var tb2 = new TextBlock();
            tb2.Text = txt.ToUpperInvariant();
            tb2.FontSize = 10;
            tb2.FontWeight = FontWeights.SemiBold;
            tb2.Foreground = Theme.Br(Theme.Muted(_dark));
            tb2.Margin = new Thickness(0, 0, 0, 3);
            return tb2;
        };

        // Helper: value text (body size, Text color)
        Func<string, TextBlock> makeValue = delegate(string txt)
        {
            var tb2 = new TextBlock();
            tb2.Text = txt;
            tb2.FontSize = 13;
            tb2.Foreground = Theme.Br(Theme.Text(_dark));
            tb2.TextWrapping = TextWrapping.Wrap;
            return tb2;
        };

        // Helper: a [FUTURE] caveat row with ⚠ prefix (Warning color, smaller)
        Func<string, TextBlock> makeFutureNote = delegate(string txt)
        {
            var tb2 = new TextBlock();
            tb2.Text = txt;
            tb2.FontSize = 11;
            tb2.Foreground = Theme.Br(Theme.Warning(_dark));
            tb2.TextWrapping = TextWrapping.Wrap;
            tb2.Margin = new Thickness(0, 2, 0, 0);
            return tb2;
        };

        // Helper: thin rule between sections
        Func<Border> makeRule = delegate()
        {
            var sep = new Border();
            sep.Height = 1;
            sep.Background = Theme.Br(Theme.Border(_dark));
            sep.Margin = new Thickness(0, 16, 0, 16);
            return sep;
        };

        // ── 1. Directive [REAL] ───────────────────────────────────────────────────
        body.Children.Add(makeSectionLabel(ja ? "指示 / Directive" : "Directive / 指示"));
        var directiveBorder = new Border();
        directiveBorder.BorderBrush = Theme.Br(Theme.Border(_dark));
        directiveBorder.BorderThickness = new Thickness(1);
        directiveBorder.CornerRadius = new CornerRadius(Theme.RadSmall);
        directiveBorder.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        directiveBorder.Padding = new Thickness(12, 8, 12, 8);
        directiveBorder.Margin = new Thickness(0, 0, 0, 0);
        var directiveTb = new TextBlock();
        directiveTb.Text = instruction;
        directiveTb.FontSize = 13;
        directiveTb.Foreground = Theme.Br(Theme.Text(_dark));
        directiveTb.TextWrapping = TextWrapping.Wrap;
        directiveBorder.Child = directiveTb;
        body.Children.Add(directiveBorder);
        body.Children.Add(makeRule());

        // ── 2. Scope [参考 / not enforced] ────────────────────────────────────────
        // The folder Scope is INFORMATIONAL only. tools/contract_gate.py keeps it on the contract
        // for context, but check_op() inspects op_class alone -- it never tests whether a path is
        // inside this folder -- so the folder boundary is NOT enforced. Label it honestly so the
        // dialog doesn't imply a guard that doesn't exist (op-class gate below stays [REAL]).
        body.Children.Add(makeSectionLabel(ja ? "作業対象 / Target" : "Target / 作業対象"));
        body.Children.Add(makeValue(folder));
        var scopeTag = new TextBlock();
        scopeTag.Text = ja ? "[参考表示 — フォルダ境界は強制されません / not enforced]"
                           : "[reference only — folder boundary not enforced / 参考]";
        scopeTag.FontSize = 10;
        scopeTag.Foreground = Theme.Br(Theme.Faint(_dark));
        scopeTag.TextWrapping = TextWrapping.Wrap;
        scopeTag.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(scopeTag);
        body.Children.Add(makeRule());

        // ── 3. Allowed [REAL] ─────────────────────────────────────────────────────
        body.Children.Add(makeSectionLabel(ja ? "許可 / Allowed" : "Allowed / 許可"));
        body.Children.Add(makeValue(
            ja ? "ファイルの編集・コマンド実行・テスト実行 / edit files, run commands, run tests"
               : "edit files · run commands · run tests"));
        var allowedSub = new TextBlock();
        allowedSub.Text = ja ? "(relayのデフォルト実行権限 — from relay permission policy)"
                             : "(relay default capability — from relay permission policy)";
        allowedSub.FontSize = 11;
        allowedSub.Foreground = Theme.Br(Theme.Muted(_dark));
        allowedSub.TextWrapping = TextWrapping.Wrap;
        allowedSub.Margin = new Thickness(0, 2, 0, 0);
        body.Children.Add(allowedSub);
        var allowedTag = new TextBlock();
        allowedTag.Text = "[REAL]";
        allowedTag.FontSize = 10;
        allowedTag.Foreground = Theme.Br(Theme.Faint(_dark));
        allowedTag.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(allowedTag);
        body.Children.Add(makeRule());

        // ── 4. Ask before [REAL — tool gate enforces these op-classes] ──────────
        body.Children.Add(makeSectionLabel(ja ? "確認してから / Ask before" : "Ask before / 確認してから"));
        var askNote = new TextBlock();
        askNote.Text = ja ? "(対応ツール経由の操作だけ、ツールゲートが実行前に一時停止して確認します — only ops routed through supported tools are checked)"
                          : "(tool gate pauses for approval only on ops routed through supported tools — not a blanket guard)";
        askNote.FontSize = 11;
        askNote.Foreground = Theme.Br(Theme.Muted(_dark));
        askNote.TextWrapping = TextWrapping.Wrap;
        askNote.Margin = new Thickness(0, 0, 0, 6);
        body.Children.Add(askNote);

        // Three checkboxes — all checked by default
        var cbDelete = new CheckBox();
        cbDelete.Content = ja ? "対応ツール経由のファイル削除 / Delete files (via supported tools)"
                              : "Delete files via supported tools / 対応ツール経由のファイル削除";
        cbDelete.IsChecked = true;
        cbDelete.Foreground = Theme.Br(Theme.Text(_dark));
        cbDelete.FontSize = 13;
        cbDelete.Margin = new Thickness(0, 2, 0, 2);
        body.Children.Add(cbDelete);

        var cbOutbound = new CheckBox();
        cbOutbound.Content = ja ? "対応ツール経由のOutlook送信 / Outlook sends (via supported tools)"
                                : "Outlook sends via supported tools / 対応ツール経由のOutlook送信";
        cbOutbound.IsChecked = true;
        cbOutbound.Foreground = Theme.Br(Theme.Text(_dark));
        cbOutbound.FontSize = 13;
        cbOutbound.Margin = new Thickness(0, 2, 0, 2);
        body.Children.Add(cbOutbound);

        var cbShell = new CheckBox();
        cbShell.Content = ja ? "対応ツール経由の破壊的shell / Destructive shell (via supported tools)"
                             : "Destructive shell via supported tools / 対応ツール経由の破壊的shell";
        cbShell.IsChecked = true;
        cbShell.Foreground = Theme.Br(Theme.Text(_dark));
        cbShell.FontSize = 13;
        cbShell.Margin = new Thickness(0, 2, 0, 2);
        body.Children.Add(cbShell);

        var askTag = new TextBlock();
        askTag.Text = "[REAL — enforced via tool gate, supported tools only]";
        askTag.FontSize = 10;
        askTag.Foreground = Theme.Br(Theme.Faint(_dark));
        askTag.Margin = new Thickness(0, 3, 0, 0);
        body.Children.Add(askTag);
        body.Children.Add(makeRule());

        // ── 5. Stop when: turn budget [REAL — relay enforces budget_turns] ────
        body.Children.Add(makeSectionLabel(ja ? "停止条件 / Stop when" : "Stop when / 停止条件"));
        var budgetNote = new TextBlock();
        budgetNote.Text = ja ? "(ターン上限はrelayが強制します。0 = 上限なし — relay enforces budget; 0 = no budget)"
                             : "(turn budget is enforced by the relay — 0 means no budget)";
        budgetNote.FontSize = 11;
        budgetNote.Foreground = Theme.Br(Theme.Muted(_dark));
        budgetNote.TextWrapping = TextWrapping.Wrap;
        budgetNote.Margin = new Thickness(0, 0, 0, 6);
        body.Children.Add(budgetNote);

        var budgetRow = new StackPanel();
        budgetRow.Orientation = Orientation.Horizontal;
        budgetRow.Margin = new Thickness(0, 0, 0, 2);
        var budgetLbl = new TextBlock();
        budgetLbl.Text = ja ? "ターン上限 / Turn budget: " : "ターン上限 / Turn budget: ";
        budgetLbl.FontSize = 13;
        budgetLbl.Foreground = Theme.Br(Theme.Text(_dark));
        budgetLbl.VerticalAlignment = VerticalAlignment.Center;
        budgetRow.Children.Add(budgetLbl);

        var budgetBox = new TextBox();
        budgetBox.Text = "200";
        budgetBox.Width = 70;
        budgetBox.FontSize = 13;
        budgetBox.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        budgetBox.Foreground = Theme.Br(Theme.Text(_dark));
        budgetBox.BorderBrush = Theme.Br(Theme.Border(_dark));
        budgetBox.BorderThickness = new Thickness(1);
        budgetBox.Padding = new Thickness(6, 2, 6, 2);
        budgetBox.VerticalAlignment = VerticalAlignment.Center;
        budgetBox.ToolTip = ja ? "0 = 上限なし / 0 = no budget" : "0 = no budget";
        budgetRow.Children.Add(budgetBox);

        var budgetZeroLbl = new TextBlock();
        budgetZeroLbl.Text = ja ? "  (0 = 上限なし)" : "  (0 = no budget)";
        budgetZeroLbl.FontSize = 11;
        budgetZeroLbl.Foreground = Theme.Br(Theme.Muted(_dark));
        budgetZeroLbl.VerticalAlignment = VerticalAlignment.Center;
        budgetRow.Children.Add(budgetZeroLbl);
        body.Children.Add(budgetRow);

        var budgetTag = new TextBlock();
        budgetTag.Text = "[REAL — enforced by relay per-worker turn count]";
        budgetTag.FontSize = 10;
        budgetTag.Foreground = Theme.Br(Theme.Faint(_dark));
        budgetTag.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(budgetTag);
        body.Children.Add(makeRule());

        // ── 6. Acceptance [REAL/partial] ─────────────────────────────────────────
        body.Children.Add(makeSectionLabel(ja ? "受入条件 / Acceptance" : "Acceptance / 受入条件"));
        body.Children.Add(makeValue(ja ? "指定なし / none specified" : "none specified (no checks[] provided)"));
        var acceptTag = new TextBlock();
        acceptTag.Text = "[REAL/partial — checks[] from goal, if any]";
        acceptTag.FontSize = 10;
        acceptTag.Foreground = Theme.Br(Theme.Faint(_dark));
        acceptTag.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(acceptTag);
        body.Children.Add(makeRule());

        // ── 7. Report [REAL] ─────────────────────────────────────────────────────
        body.Children.Add(makeSectionLabel(ja ? "報告 / Report" : "Report / 報告"));
        body.Children.Add(makeValue(ja ? "完了時に要約と最終更新 / summary + last update on completion"
                                      : "summary + last update on completion"));
        var reportSub = new TextBlock();
        reportSub.Text = ja ? "(outcome + transcript末尾からカードに表示)"
                            : "(outcome + last transcript excerpt shown on card)";
        reportSub.FontSize = 11;
        reportSub.Foreground = Theme.Br(Theme.Muted(_dark));
        reportSub.TextWrapping = TextWrapping.Wrap;
        reportSub.Margin = new Thickness(0, 2, 0, 0);
        body.Children.Add(reportSub);
        var reportTag = new TextBlock();
        reportTag.Text = "[REAL]";
        reportTag.FontSize = 10;
        reportTag.Foreground = Theme.Br(Theme.Faint(_dark));
        reportTag.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(reportTag);
        body.Children.Add(makeRule());

        // ── 8. Effort / Approval (read-only display, header dropdowns are authoritative) ──
        var effortRow = new StackPanel();
        effortRow.Orientation = Orientation.Horizontal;
        effortRow.Margin = new Thickness(0, 0, 0, 4);
        var effortLbl = new TextBlock();
        effortLbl.Text = (ja ? "推論 / Effort: " : "Effort: ");
        effortLbl.FontSize = 12;
        effortLbl.Foreground = Theme.Br(Theme.Muted(_dark));
        effortLbl.VerticalAlignment = VerticalAlignment.Center;
        effortRow.Children.Add(effortLbl);
        var effortVal = new TextBlock();
        effortVal.Text = _effort;
        effortVal.FontSize = 12;
        effortVal.FontWeight = FontWeights.SemiBold;
        effortVal.Foreground = Theme.Br(Theme.Text(_dark));
        effortVal.VerticalAlignment = VerticalAlignment.Center;
        effortVal.Margin = new Thickness(4, 0, 24, 0);
        effortRow.Children.Add(effortVal);
        // read-only mirror of the header effort-policy combo (the header dropdown is authoritative)
        var epLbl = new TextBlock();
        epLbl.Text = T("effort_policy") + ": ";
        epLbl.FontSize = 12;
        epLbl.Foreground = Theme.Br(Theme.Muted(_dark));
        epLbl.VerticalAlignment = VerticalAlignment.Center;
        effortRow.Children.Add(epLbl);
        var epVal = new TextBlock();
        epVal.Text = _effortPolicy;
        epVal.FontSize = 12;
        epVal.FontWeight = FontWeights.SemiBold;
        epVal.Foreground = Theme.Br(Theme.Text(_dark));
        epVal.VerticalAlignment = VerticalAlignment.Center;
        epVal.Margin = new Thickness(4, 0, 24, 0);
        effortRow.Children.Add(epVal);
        var approvalLbl = new TextBlock();
        approvalLbl.Text = (ja ? "実行方式 / Run mode: " : "Run mode: ");
        approvalLbl.FontSize = 12;
        approvalLbl.Foreground = Theme.Br(Theme.Muted(_dark));
        approvalLbl.VerticalAlignment = VerticalAlignment.Center;
        effortRow.Children.Add(approvalLbl);
        var approvalVal = new TextBlock();
        approvalVal.Text = _approval;
        approvalVal.FontSize = 12;
        approvalVal.FontWeight = FontWeights.SemiBold;
        approvalVal.Foreground = Theme.Br(Theme.Text(_dark));
        approvalVal.VerticalAlignment = VerticalAlignment.Center;
        approvalVal.Margin = new Thickness(4, 0, 0, 0);
        effortRow.Children.Add(approvalVal);
        body.Children.Add(effortRow);
        var effortNote = new TextBlock();
        effortNote.Text = ja ? "(ヘッダーのドロップダウンが主設定 / header dropdowns are authoritative)"
                             : "(header dropdowns are authoritative — shown here for reference)";
        effortNote.FontSize = 10;
        effortNote.Foreground = Theme.Br(Theme.Faint(_dark));
        effortNote.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(effortNote);

        outer.Children.Add(body);

        // ── Footer: Cancel + Delegate buttons ────────────────────────────────────
        var footerBand = new Border();
        footerBand.BorderBrush = Theme.Br(Theme.Border(_dark));
        footerBand.BorderThickness = new Thickness(0, 1, 0, 0);
        footerBand.Background = Theme.Br(Theme.Bg(_dark));
        footerBand.Padding = new Thickness(24, 16, 24, 16);

        var footerRow = new StackPanel();
        footerRow.Orientation = Orientation.Horizontal;
        footerRow.HorizontalAlignment = HorizontalAlignment.Right;

        bool[] delegated = new bool[1];
        delegated[0] = false;

        var cancelBtn = new Button();
        cancelBtn.Content = ja ? "キャンセル / Cancel" : "Cancel";
        cancelBtn.IsCancel = true;
        cancelBtn.Height = Theme.BtnH;
        cancelBtn.Padding = new Thickness(16, 0, 16, 0);
        cancelBtn.Background = Brushes.Transparent;
        cancelBtn.Foreground = Theme.Br(Theme.Text(_dark));
        cancelBtn.BorderBrush = Theme.Br(Theme.Border(_dark));
        cancelBtn.BorderThickness = new Thickness(1);
        cancelBtn.Cursor = Cursors.Hand;
        cancelBtn.Margin = new Thickness(0, 0, 12, 0);
        cancelBtn.Click += delegate { w.DialogResult = false; };
        footerRow.Children.Add(cancelBtn);

        var delegateBtn = new Button();
        delegateBtn.Content = ja ? "委任する →" : "Delegate →";
        delegateBtn.IsDefault = true;
        delegateBtn.Height = Theme.BtnH;
        delegateBtn.Padding = new Thickness(20, 0, 20, 0);
        delegateBtn.Background = Theme.Br(Theme.AccentFill(_dark));
        delegateBtn.Foreground = new SolidColorBrush(C(Theme.AccentFg(_dark)));
        delegateBtn.BorderThickness = new Thickness(0);
        delegateBtn.FontWeight = FontWeights.SemiBold;
        delegateBtn.Cursor = Cursors.Hand;
        delegateBtn.Click += delegate { delegated[0] = true; w.DialogResult = true; };
        footerRow.Children.Add(delegateBtn);

        footerBand.Child = footerRow;
        outer.Children.Add(footerBand);

        var scroll = new ScrollViewer();
        scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        scroll.MaxHeight = 680;
        scroll.Content = outer;
        w.Content = scroll;

        bool? result = w.ShowDialog();
        bool didDelegate = result == true && delegated[0];
        if (didDelegate)
        {
            // Write active_contract.json with user-chosen op-class gates + turn budget.
            // ask_before = only the op-classes whose checkbox was checked (unchecked = not gated).
            // stop_when = ["budget"] when budgetTurns > 0, else [].
            // budget_turns = the integer value from the text box (0 = no budget).
            try
            {
                string contractDir = Path.GetDirectoryName(_statusPath);
                string contractPath = Path.Combine(contractDir, "active_contract.json");
                long epochSec = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                string escapedFolder = folder.Replace("\\", "\\\\").Replace("\"", "\\\"");

                // Build ask_before array from checkboxes
                var askBefore = new System.Collections.Generic.List<string>();
                if (cbDelete.IsChecked == true) askBefore.Add("\"delete\"");
                if (cbOutbound.IsChecked == true) askBefore.Add("\"outbound\"");
                if (cbShell.IsChecked == true) askBefore.Add("\"shell_destructive\"");
                string askBeforeJson = "[" + string.Join(",", askBefore.ToArray()) + "]";

                // Parse turn budget (default 0 on invalid input)
                int budgetTurns = 0;
                int parsedBudget;
                if (int.TryParse(budgetBox.Text, out parsedBudget) && parsedBudget >= 0)
                    budgetTurns = parsedBudget;
                string stopWhenJson = budgetTurns > 0 ? "[\"budget\"]" : "[]";

                string contractJson = "{\"active\":true,\"scope\":\"" + escapedFolder
                    + "\",\"ask_before\":" + askBeforeJson
                    + ",\"stop_when\":" + stopWhenJson
                    + ",\"budget_turns\":" + budgetTurns
                    + ",\"started\":" + epochSec + "}";
                File.WriteAllText(contractPath, contractJson, new UTF8Encoding(false));
            }
            catch (Exception) { }
        }
        return didDelegate;
    }
}
