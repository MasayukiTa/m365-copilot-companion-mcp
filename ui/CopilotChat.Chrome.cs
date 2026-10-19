// CopilotChat.Chrome.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
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
    // ── small helpers ───────────────────────────────────────────────────────────
    Button Btn(string content, string bg, string fg, bool bordered)
    {
        var b = new Button { Content = content, Cursor = Cursors.Hand, BorderThickness = new Thickness(bordered ? 1 : 0) };
        SetRef(b, BackgroundProperty, bg); SetRef(b, ForegroundProperty, fg);
        if (bordered) SetRef(b, Control.BorderBrushProperty, "Border");
        return b;
    }
    void SetRef(FrameworkElement el, DependencyProperty p, string key) { el.SetResourceReference(p, key); }

    // ── Material Symbols glyphs (vector paths, NO emoji) — mirrors FleetCockpit's loader so the
    //    sidebar footer icons match the cockpit exactly. Reads the shared 8-glyph subset font
    //    (ui/assets/material_glyphs.json). Best-effort: a missing/unreadable file just yields
    //    empty placeholders (the tooltip still carries the label).

    void LoadGlyphs()
    {
        try
        {
            string p = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "assets", "material_glyphs.json");
            if (!File.Exists(p)) return;
            var o = _cjs.DeserializeObject(File.ReadAllText(p, Encoding.UTF8)) as Dictionary<string, object>;
            if (o == null) return;
            if (o.ContainsKey("unitsPerEm")) _upm = Convert.ToDouble(o["unitsPerEm"]);
            var g = o["glyphs"] as Dictionary<string, object>;
            if (g == null) return;
            foreach (KeyValuePair<string, object> kv in g) _glyphs[kv.Key] = kv.Value.ToString();
        }
        catch { }
    }
    UIElement MakeIcon(string name, double size)
    {
        if (!_glyphs.ContainsKey(name)) { var ph = new Border(); ph.Width = size; ph.Height = size; return ph; }
        var path = new System.Windows.Shapes.Path();
        Geometry geo = Geometry.Parse(_glyphs[name]).Clone();   // Geometry.Parse returns FROZEN -> Clone before Transform
        double s = size / _upm;
        geo.Transform = new MatrixTransform(s, 0, 0, -s, 0, s * _upm);   // font y-up -> WPF y-down
        path.Data = geo; path.Stretch = Stretch.None;
        path.Width = size; path.Height = size;
        path.HorizontalAlignment = HorizontalAlignment.Center;
        path.VerticalAlignment = VerticalAlignment.Center;
        SetRef(path, System.Windows.Shapes.Shape.FillProperty, "Muted");
        return path;
    }
    // Compact icon button matching FleetCockpit's IconButton look (quiet, hover surface via the
    // shared Button template). glyph names come from the 8-glyph subset.
    Button IconButton(string glyph, double size, string tip)
    {
        var b = new Button
        {
            Content = MakeIcon(glyph, size),
            Width = 36, Height = 32, Cursor = Cursors.Hand,
            BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            ToolTip = tip, Tag = glyph
        };
        return b;
    }

    // First line only, ellipsis-trimmed to `max` chars; the one definition is in ChatSend.cs.
    static string TrimTitle(string s, int max) { return ChatSend.TrimTitle(s, max); }

    // Header title tracks the active conversation (Wave 2). Untitled -> localized "New chat".
    void RefreshHeadTitle()
    {
        if (_headTitle == null) return;
        _headTitle.Text = (_conv == null || _conv.Untitled()) ? T("newchat") : TrimTitle(_conv.Title, 60);
    }

    // ── status dot: 4 states (Theme tokens) + tooltip ───────────────────────────
    //   "idle"    Success  — bridge reachable, nothing streaming
    //   "busy"    Accent   — a reply is streaming
    //   "offline" Danger   — bridge unreachable / last stream errored
    //   "signin"  Warning  — last answer looked like a sign-in / canned refusal
    // Precedence while idle: signin > offline > idle. "busy" always wins while generating.
    // MUST be called on the UI thread (background probe marshals via Dispatcher).
    void SetDot(string state)
    {
        _dotState = state;
        if (_statusDot == null) return;
        string token, tip;
        if (state == "busy")         { token = "Accent";  tip = T("dot_busy"); }
        else if (state == "offline") { token = "Danger";  tip = T("dot_offline"); }
        else if (state == "signin")  { token = "Warning"; tip = T("dot_signin"); }
        else                         { token = "Success"; tip = T("dot_idle"); }
        SetRef(_statusDot, BackgroundProperty, token);
        _statusDot.ToolTip = tip;
    }

    // Re-derive the IDLE dot color from the latest signals (reachability + last-answer verdict).
    // Never called while generating (busy owns the dot then). 'signinLatch' persists a sign-in
    // warning until the next clean answer clears it.

    void RefreshIdleDot()
    {
        if (_generating) return;
        if (_signinLatch) { SetDot("signin"); return; }
        SetDot(_bridgeReachable ? "idle" : "offline");
    }

    // Heuristic: did this answer look like a Copilot sign-in prompt / canned refusal?
    static bool LooksLikeRefusal(string ans)
    {
        if (string.IsNullOrEmpty(ans)) return false;
        if (ans.Contains("それに応答できませんでした")) return true;
        if (ans.IndexOf("I couldn't respond to that", StringComparison.OrdinalIgnoreCase) >= 0) return true;
        if (ans.Contains("サインイン") && ans.Contains("必要")) return true;
        return false;
    }

    // Low-cadence bridge reachability probe (piggybacked on the 800ms timer at a ~15s cadence).
    // Runs the actual GET on a background thread so a hung bridge never freezes the UI; the
    // result is marshalled back to update the dot. Proxy=null so loopback isn't routed through a
    // corporate proxy (which would make a local bridge look unreachable).
    void ProbeBridge()
    {
        if (WindowSelfTest.Active) return;   // --selftest touches no network
        if (_reachProbing) return;
        _reachProbing = true;
        new Thread((ThreadStart)delegate
        {
            bool ok = false;
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(_bridge + "/conv");
                req.Timeout = 2500; req.ReadWriteTimeout = 2500; req.Proxy = null;
                using (var resp = (HttpWebResponse)req.GetResponse())
                    ok = (int)resp.StatusCode < 500;
            }
            catch (WebException wex)
            {
                // A protocol response (even 4xx) still proves the bridge is up and answering.
                ok = wex.Response != null;
            }
            catch { ok = false; }
            _bridgeReachable = ok;
            _reachProbing = false;
            try { Dispatcher.BeginInvoke(new Action(delegate { RefreshIdleDot(); })); } catch { }
        }) { IsBackground = true }.Start();
    }

    // Generic inline recovery banner: a message + a single action button. Reused by the
    // sign-in/refusal banner (ShowSigninBanner) and the bridge-offline recovery banner
    // (send path in DoSend) -- both are "something is wrong, here is the one-click fix" UI.
    void ShowRecoveryBanner(string message, string buttonLabel, Action onClick)
    {
        if (_banner == null || _bannerBody == null) return;
        _bannerBody.Children.Clear();
        var head = new TextBlock
        {
            Text = message, FontWeight = FontWeights.SemiBold, FontSize = 13,
            TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 12)
        };
        SetRef(head, TextBlock.ForegroundProperty, "Fg");
        _bannerBody.Children.Add(head);
        var open = Btn(buttonLabel, "AccentFill", "AccentFg", false);
        open.Height = 30; open.Padding = new Thickness(16, 0, 16, 0); open.FontWeight = FontWeights.SemiBold;
        open.HorizontalAlignment = HorizontalAlignment.Left;
        open.Click += delegate { onClick(); };
        _bannerBody.Children.Add(open);
        SetRef(_banner, Border.BorderBrushProperty, "Warning");
        _banner.Visibility = Visibility.Visible;
    }

    // Show the sign-in / refusal banner with an actionable "Open Fleet Cockpit" link.
    void ShowSigninBanner()
    {
        ShowRecoveryBanner(T("signin_banner"), T("signin_open"), delegate { HideBanner(); OpenCockpit(); });
        _signinBannerShown = true;
    }

    // Composer focus ring WITHOUT a layout jump. Three states:
    //   "rest"  -> 1px Border token + full padding (12,6,12,6)
    //   "focus" -> 1px Accent       + full padding (12,6,12,6)
    //   "steer" -> 2px Accent       + padding-1    (11,5,11,5)   (compensates the +1 border)
    // Border+padding sums are identical in every state (13 horiz, 7 vert), so the composer's
    // ActualHeight/width never move as focus/steer change. BorderBrush swaps between the Border
    // and Accent tokens so both palette modes track the theme.
    void SetComposerRing(string state)
    {
        if (_composerBorder == null) return;
        if (state == "steer")
        {
            SetRef(_composerBorder, Border.BorderBrushProperty, "Accent");
            _composerBorder.BorderThickness = new Thickness(2);
            _composerBorder.Padding = new Thickness(12, 5, 12, 5);
        }
        else if (state == "focus")
        {
            SetRef(_composerBorder, Border.BorderBrushProperty, "Accent");
            _composerBorder.BorderThickness = new Thickness(1);
            _composerBorder.Padding = new Thickness(12, 6, 12, 6);
        }
        else // "rest"
        {
            SetRef(_composerBorder, Border.BorderBrushProperty, "Border");
            _composerBorder.BorderThickness = new Thickness(1);
            _composerBorder.Padding = new Thickness(12, 6, 12, 6);
        }
    }

    // Accent fill only when there is text to send OR while generating (Stop affordance).
    // Otherwise the button shows a neutral/disabled-looking state so the empty-input state
    // is visually distinct from the "ready to send" state. Also drives real enable/disable:
    // the button is disabled ONLY when idle with an empty input (nothing to send). While
    // generating it stays enabled because it doubles as the Stop control (see SendText/DoSend).
    void PaintSend()
    {
        if (_send == null) return;
        bool hasText = _input != null && _input.Text.Trim().Length > 0;
        bool active = _generating || hasText;
        // Enabled: generating (acts as Stop) OR there is text to send. Disabled: idle & empty.
        _send.IsEnabled = _generating || hasText;
        _send.Cursor = _send.IsEnabled ? Cursors.Hand : Cursors.Arrow;
        if (active)
        {
            SetRef(_send, BackgroundProperty, "Accent");
            SetRef(_send, ForegroundProperty, "AccentFg");
        }
        else
        {
            SetRef(_send, BackgroundProperty, "PanelAlt");
            SetRef(_send, ForegroundProperty, "Faint");   // disabled: faint foreground
        }
    }

    // Repo root: this exe runs from <repo>\ui, so one level up is <repo> -- same convention
    // already used for .fleet\status.json / .fleet\commands.d below.
    string RepoRoot() { return Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..")); }

    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int nCmdShow);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool RedrawWindow(IntPtr h, IntPtr lprc, IntPtr hrgn, uint flags);
    // Finding a process's window WITHOUT MainWindowHandle. That property returns 0 for a
    // process whose windows are all invisible -- which is a real state here, not a corner
    // case: a cockpit started beneath an SW_HIDE parent inherits the hidden show-state (the
    // same inheritance this file already documents for its own window), so it exists, owns
    // six windows, and reports no main window at all. Reading MainWindowHandle alone, the
    // launcher could neither focus it nor decide it was absent.
    delegate bool EnumWindowsProc(IntPtr h, IntPtr param);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool EnumWindows(EnumWindowsProc cb, IntPtr param);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool IsWindowVisible(IntPtr h);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern IntPtr GetWindow(IntPtr h, uint cmd);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern int GetWindowLong(IntPtr h, int index);

    // The VISIBLE top-level window belonging to `pid`, or IntPtr.Zero.
    //
    // VISIBLE IS THE FILTER, and it has to be. A WPF process owns several top-level windows
    // that are not the app -- measured here: HwndWrapper helpers with no title, plus
    // MediaContextNotificationWindow, SystemResourceNotifyWindow and CiceroUIWndFrame, which
    // DO have titles. So "has a caption" does not separate them, and returning any top-level
    // window would let this button report success while raising an invisible helper -- worse
    // than the bug it replaces, because it would also suppress the launch fallback.
    //
    // A MINIMISED window still satisfies IsWindowVisible (WS_VISIBLE stays set while
    // iconic), so the case this whole fix exists for is inside this filter, not outside it.
    static IntPtr TopLevelWindowOf(int pid)
    {
        IntPtr found = IntPtr.Zero;
        try
        {
            EnumWindows(delegate(IntPtr h, IntPtr _)
            {
                if (found != IntPtr.Zero) return false;
                uint owner;
                GetWindowThreadProcessId(h, out owner);
                if (owner != (uint)pid) return true;
                const uint GW_OWNER = 4;
                if (GetWindow(h, GW_OWNER) != IntPtr.Zero) return true;   // a dialog, not the app
                // VISIBLE, OR AT LEAST A WINDOW A PERSON COULD USE.
                //
                // Requiring visibility alone leaves the one state this button most needs to
                // rescue: a cockpit started beneath a hidden parent builds its window and
                // never sets WS_VISIBLE, so it is running, responding, and unreachable.
                // Style separates it from the WPF helpers that a title does not -- measured,
                // the app window carries caption+sysmenu+thickframe (0x6CF0000) while
                // MediaContextNotificationWindow and friends are bare popups (0x8C000000).
                const int GWL_STYLE = -16;
                const int WS_CAPTION = 0x00C00000, WS_SYSMENU = 0x00080000,
                          WS_THICKFRAME = 0x00040000;
                int style = GetWindowLong(h, GWL_STYLE);
                bool usable = (style & WS_CAPTION) == WS_CAPTION
                              && (style & WS_SYSMENU) != 0 && (style & WS_THICKFRAME) != 0;
                if (IsWindowVisible(h) || usable) found = h;
                return true;
            }, IntPtr.Zero);
        }
        catch { }
        return found;
    }

    // The daily launcher is windowless: start_all_hidden.vbs -> start_all.ps1 (-WindowStyle
    // Hidden = SW_HIDE) -> this app. A child of an SW_HIDE parent INHERITS that show-state
    // via STARTUPINFO.wShowWindow, so WPF builds the HWND without a real first paint: DWM
    // has no composed surface and the window shows as black / stale rectangles even though
    // the visual tree is intact (PrintWindow of the same HWND returns correct content).
    // One SW_SHOW + full redraw right after load discards the inherited state. Idempotent,
    // and a no-op when the app was started normally.
    void ForceVisibleOnce()
    {
        try
        {
            IntPtr h = new System.Windows.Interop.WindowInteropHelper(this).Handle;
            if (h == IntPtr.Zero) return;
            const int SW_SHOW = 5;
            const uint RDW_INVALIDATE = 0x0001, RDW_ERASE = 0x0004,
                       RDW_ALLCHILDREN = 0x0080, RDW_UPDATENOW = 0x0100;
            ShowWindow(h, SW_SHOW);
            RedrawWindow(h, IntPtr.Zero, IntPtr.Zero,
                         RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW);
        }
        catch { }
    }
    // Launch (or focus) the parallel-execution cockpit -- so the user never has to close
    // everything and restart just to reach it.
    void OpenCockpit()
    {
        // THREE WAYS THIS DID NOTHING AT ALL, all of them silent.
        //
        //  1. SetForegroundWindow does NOT un-minimise a window. A cockpit the user had
        //     minimised stayed minimised and the click appeared to do nothing -- the single
        //     most reachable state, since minimising it is how you get back to this window.
        //     ShowWindow was already imported and used elsewhere in this file; it was simply
        //     never called here.
        //  2. A process whose window does not exist YET (still starting) has
        //     MainWindowHandle == 0, and the old code returned on that: no focus, no launch,
        //     no message. Clicking again did nothing either, because the process was found
        //     again. A HUNG cockpit had the same shape and killed the button permanently.
        //  3. SetForegroundWindow can be refused by the foreground lock and returns false;
        //     the result was discarded, so a refusal was indistinguishable from success.
        //
        // And it ran on the UI thread: enumerating every process (291 on this machine, 38 ms
        // idle and worse while a fleet is running) and then touching MainWindowHandle, which
        // enumerates windows again -- against an 800 ms timer already doing file I/O. The
        // lookup now happens off the UI thread and only the window poke comes back to it.
        string exe = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "FleetCockpit.exe");
        System.Threading.ThreadPool.QueueUserWorkItem(delegate
        {
            IntPtr target = IntPtr.Zero;
            bool starting = false;
            try
            {
                foreach (var pr in System.Diagnostics.Process.GetProcessesByName("FleetCockpit"))
                {
                    using (pr)
                    {
                        if (target != IntPtr.Zero) continue;
                        IntPtr h = IntPtr.Zero;
                        try { h = pr.MainWindowHandle; } catch { }
                        // Fall back to enumeration: MainWindowHandle is 0 for a process whose
                        // windows are merely INVISIBLE, and that process is exactly the one
                        // this button needs to rescue.
                        if (h == IntPtr.Zero) { try { h = TopLevelWindowOf(pr.Id); } catch { } }
                        if (h != IntPtr.Zero)
                        {
                            // FOCUS IT EVEN IF IT IS NOT RESPONDING. A hung cockpit still has
                            // a window, and showing the user a frozen window they can close is
                            // strictly better than a button that does nothing and says nothing.
                            target = h;
                            continue;
                        }
                        // No window. Either it is starting, or it is a broken process that
                        // will never have one -- and the old code could not tell them apart,
                        // so ONE such process disabled this button for the rest of the
                        // session. Age decides: a young process is starting and deserves a
                        // moment; an old one with no window is not going to grow one.
                        // NO VISIBLE WINDOW. Either it is still starting, or it is a
                        // process that will never show one -- a cockpit launched beneath an
                        // SW_HIDE parent inherits the hidden show-state and owns only
                        // invisible windows. The old code could not tell those apart from a
                        // healthy cockpit, so ONE of them disabled this button for the rest
                        // of the session. Age decides: a young process is starting and gets a
                        // moment; an old one with nothing visible is not going to grow a
                        // window, and a second cockpit beats a dead button.
                        try
                        {
                            starting = starting ||
                                (DateTime.Now - pr.StartTime).TotalSeconds < 20;
                        }
                        catch { }
                    }
                }
            }
            catch { }

            Dispatcher.BeginInvoke((Action)delegate
            {
                if (target != IntPtr.Zero)
                {
                    const int SW_RESTORE = 9;
                    try
                    {
                        // RESTORE, THEN RAISE. SetForegroundWindow does not un-minimise, so on
                        // a minimised cockpit the old call raised nothing and the click looked
                        // like it had done nothing at all -- and minimising the cockpit is how
                        // you get back to this window, so it was the reachable state.
                        // SW_RESTORE is a no-op when the window is not iconic.
                        const int SW_SHOW = 5;
                        if (!IsWindowVisible(target)) ShowWindow(target, SW_SHOW);
                        ShowWindow(target, SW_RESTORE);
                        SetForegroundWindow(target);
                    }
                    catch { }
                    return;
                }
                // Nothing focusable. If something is mid-start, say so rather than launching a
                // second cockpit on top of it; otherwise start one.
                if (starting) return;
                try { if (File.Exists(exe)) System.Diagnostics.Process.Start(exe); }
                catch { }
            });
        });
    }

    // Replace the default WPF button chrome (which forces an unreadable light-blue
    // hover highlight) with a flat template: the button's own Background + a
    // color-independent translucent overlay on hover/press, content always on top.
    void AddButtonStyle()
    {
        const string xaml =
            "<Style xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'" +
            " xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml' TargetType='Button'>" +
            "<Setter Property='Template'><Setter.Value>" +
            "<ControlTemplate TargetType='Button'>" +
            "<Border Background='{TemplateBinding Background}' BorderBrush='{TemplateBinding BorderBrush}'" +
            " BorderThickness='{TemplateBinding BorderThickness}' CornerRadius='7' SnapsToDevicePixels='True'>" +
            "<Grid><Border x:Name='ov' Background='Transparent' CornerRadius='7'/>" +
            "<ContentPresenter Margin='{TemplateBinding Padding}'" +
            " HorizontalAlignment='{TemplateBinding HorizontalContentAlignment}'" +
            " VerticalAlignment='{TemplateBinding VerticalContentAlignment}'/></Grid></Border>" +
            "<ControlTemplate.Triggers>" +
            "<Trigger Property='IsMouseOver' Value='True'>" +
            "<Setter TargetName='ov' Property='Background' Value='{DynamicResource Hover}'/></Trigger>" +
            "<Trigger Property='IsPressed' Value='True'>" +
            "<Setter TargetName='ov' Property='Background' Value='{DynamicResource Press}'/></Trigger>" +
            "<Trigger Property='IsEnabled' Value='False'><Setter Property='Opacity' Value='0.5'/></Trigger>" +
            "</ControlTemplate.Triggers></ControlTemplate>" +
            "</Setter.Value></Setter></Style>";
        var style = (Style)System.Windows.Markup.XamlReader.Parse(xaml);
        Application.Current.Resources[typeof(Button)] = style;
    }
    void Set(string key, string hex) { Application.Current.Resources[key] = new SolidColorBrush(C(hex)); }
    void ApplyTheme()
    {
        // Single source of truth: Theme.cs (calm warm-neutral palette, spec Design Tokens).
        Set("Bg", Theme.Bg(_dark));
        Set("Panel", Theme.Surface(_dark));
        Set("PanelAlt", Theme.SurfaceSubtle(_dark));
        Set("Selected", Theme.Selected(_dark));   // active sidebar row card fill (no rail/colored border)
        Set("Border", Theme.Border(_dark));
        Set("BorderStrong", Theme.BorderStrong(_dark));
        Set("Fg", Theme.Text(_dark));
        Set("Muted", Theme.Muted(_dark));
        Set("Faint", Theme.Faint(_dark));
        Set("UserBg", Theme.SurfaceSubtle(_dark));
        Set("Accent", Theme.Accent(_dark));
        Set("AccentSoft", Theme.AccentSoft(_dark));
        Set("AccentFg", Theme.AccentFg(_dark));
        // The fill beneath it. "Accent" stays the bright mark used for borders and focus.
        Set("AccentFill", Theme.AccentFill(_dark));
        Set("Success", Theme.Success(_dark));   // status dot: idle & bridge reachable
        Set("Warning", Theme.Warning(_dark));   // status dot: sign-in / canned refusal
        Set("Danger", Theme.Danger(_dark));     // status dot: bridge unreachable / stream error
        Set("Hover", Theme.Hover(_dark));
        Set("Press", Theme.Press(_dark));
        Set("CodeBg", Theme.SurfaceSubtle(_dark));
    }

    // ── whole-UI zoom (4K readability) ───────────────────────────────────────────
    // A single ScaleTransform applied as the root content's LayoutTransform scales EVERYTHING
    // (text, icons, paddings) AND reflows layout (LayoutTransform participates in measure, unlike
    // RenderTransform). Persisted as ui_scale in the shared settings.txt (the cockpit honors the
    // same key). Clamp 0.8–2.0.
    static double ClampScale(double s)
    {
        if (s < 0.8) return 0.8;
        if (s > 2.0) return 2.0;
        return s;
    }

    // Current monitor scale = this window's device pixels per DIP (DPI/96). 1.0 at 100%, 1.5 at 150%.
    // Read from the live PresentationSource; falls back to 1.0 before the window is sourced.
    double CurrentMonitorScale()
    {
        try
        {
            var src = PresentationSource.FromVisual(this);
            if (src != null && src.CompositionTarget != null)
            {
                double m11 = src.CompositionTarget.TransformToDevice.M11;
                if (m11 > 0.01) return m11;
            }
        }
        catch { }
        return 1.0;
    }

    // AUTO effective scale for a monitor: target physical size / that monitor's own DPI scale, clamped.
    // monitorScale (WPF DPI) × effective ≈ _scaleTarget -> constant physical size across monitors. The
    // clamp can cap it on extreme monitors (see numeric proof in the report).
    double EffectiveAutoScale(double monitorScale)
    {
        if (monitorScale < 0.01) monitorScale = 1.0;
        return ClampScale(_scaleTarget / monitorScale);
    }

    // Apply _uiScale to the root LayoutTransform. showToast=true flashes the "NNN%" overlay
    // (interactive changes); false is silent (initial apply / external cockpit mirror / DPI recompute).
    void ApplyScale(bool showToast)
    {
        _uiScale = ClampScale(_uiScale);
        if (_rootScale == null) _rootScale = new ScaleTransform(1.0, 1.0);
        _rootScale.ScaleX = _uiScale; _rootScale.ScaleY = _uiScale;
        if (Content is FrameworkElement)
        {
            var fe = (FrameworkElement)Content;
            if (!ReferenceEquals(fe.LayoutTransform, _rootScale)) fe.LayoutTransform = _rootScale;
        }
        if (showToast) ShowScaleToast();
    }

    // Recompute + apply the AUTO effective scale for THIS window's current monitor. Silent by default.
    void ApplyAutoScale(bool toast)
    {
        _uiScale = EffectiveAutoScale(CurrentMonitorScale());
        ApplyScale(false);
        if (toast) ShowScaleToastText(T("ui_auto"));
    }

    // Nudge the zoom by delta (±0.1 typical) -> MANUAL mode. Clamp, apply, persist, flash the overlay.
    void BumpScale(double delta)
    {
        double next = ClampScale(_uiScale + delta);
        // Snap to a clean 0.05 grid so repeated notches don't drift (0.7999999…).
        next = System.Math.Round(next * 20.0) / 20.0;
        // If we were in auto, a +/- always transitions to manual even at a clamp edge.
        if (!_uiAuto && System.Math.Abs(next - _uiScale) < 0.0001) { ShowScaleToast(); return; }
        _uiAuto = false;
        _uiScale = next;
        ApplyScale(true);
        SaveSettings();
    }

    // Ctrl+0 = "back to automatic" (NOT 1.0): re-enter auto, recompute for this monitor, brief 自動 toast.
    void ResetScale()
    {
        _uiAuto = true;
        SaveSettings();          // persist ui_scale=auto
        ApplyAutoScale(true);    // recompute for the current monitor + "自動" toast
    }

    // PMv2 fires this when the window is dragged onto a differently-scaled monitor. In AUTO mode we
    // recompute the effective scale from the NEW monitor's DPI (newDpi.DpiScaleX = DPI/96) so physical
    // size stays constant -- SILENTLY (an automatic recompute must not flash the toast). Manual mode is
    // untouched. Never throw from here.
    protected override void OnDpiChanged(DpiScale oldDpi, DpiScale newDpi)
    {
        try { base.OnDpiChanged(oldDpi, newDpi); } catch { }
        try
        {
            if (_uiAuto)
            {
                double ms = (newDpi.DpiScaleX > 0.01) ? newDpi.DpiScaleX : CurrentMonitorScale();
                _uiScale = EffectiveAutoScale(ms);
                ApplyScale(false);   // silent: no toast on automatic DPI-change recompute
            }
        }
        catch { }
    }

    // Small fading "NNN%" overlay, anchored top-center over the content. Reuses one Border/timer.
    void ShowScaleToast() { ShowScaleToastText(null); }
    // txt != null shows that literal (e.g. "自動" when entering auto); null shows the current %.
    void ShowScaleToastText(string txt)
    {
        if (txt == null) txt = ((int)System.Math.Round(_uiScale * 100.0)) + "%";
        if (_scaleToast == null)
        {
            _scaleToastText = new TextBlock
            {
                Text = txt, FontSize = 13, FontWeight = FontWeights.SemiBold,
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center
            };
            SetRef(_scaleToastText, TextBlock.ForegroundProperty, "Fg");
            _scaleToast = new Border
            {
                Child = _scaleToastText, CornerRadius = new CornerRadius(Theme.RadCard),
                Padding = new Thickness(16, 7, 16, 7), BorderThickness = new Thickness(1),
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Top,
                Margin = new Thickness(0, 16, 0, 0), IsHitTestVisible = false,
                Visibility = Visibility.Collapsed
            };
            SetRef(_scaleToast, BackgroundProperty, "Panel");
            SetRef(_scaleToast, Border.BorderBrushProperty, "Border");
            Panel.SetZIndex(_scaleToast, 200);
            // Anchor inside the scaled root Grid so it lives above the columns (and scales with the UI,
            // which is fine — it reads as part of the same zoomed surface).
            if (_rootGrid != null)
            {
                Grid.SetColumn(_scaleToast, 0);
                Grid.SetColumnSpan(_scaleToast, System.Math.Max(1, _rootGrid.ColumnDefinitions.Count));
                _rootGrid.Children.Add(_scaleToast);
            }
        }
        _scaleToastText.Text = txt;
        _scaleToast.Visibility = Visibility.Visible;
        _scaleToast.Opacity = 1.0;
        if (_scaleToastTimer == null)
        {
            _scaleToastTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(900) };
            _scaleToastTimer.Tick += delegate
            {
                _scaleToastTimer.Stop();
                var fade = new DoubleAnimation(1.0, 0.0, new Duration(TimeSpan.FromMilliseconds(280)));
                fade.Completed += delegate { if (_scaleToast != null) _scaleToast.Visibility = Visibility.Collapsed; };
                if (_scaleToast != null) _scaleToast.BeginAnimation(UIElement.OpacityProperty, fade);
            };
        }
        _scaleToast.BeginAnimation(UIElement.OpacityProperty, null);   // cancel any in-flight fade
        _scaleToast.Opacity = 1.0;
        _scaleToastTimer.Stop(); _scaleToastTimer.Start();
    }

    // STEER-mode signal: when viewing a parallel-task conversation, anything you type interrupts
    // that running worker -- a very different action from a normal chat. Make it unmistakable by
    // tinting the composer border Accent (orange) and thickening it, so the user is never surprised
    // that their message went to a background agent. (GAP 8)
    // Now operates on _composerBorder (the outer rounded wrapper) rather than the inner transparent
    // TextBox, because Task 1 made the TextBox borderless.
    void RefreshSteerVisual()
    {
        bool steer = !string.IsNullOrEmpty(_activeFleetUrl);
        if (_composerBorder != null)
        {
            // Steer mode: 2px Accent (unmistakable orange; overrides focus/rest states).
            // Normal mode: 1px Border at rest, 1px Accent when the input has focus.
            // SetComposerRing keeps the footprint constant across all three (padding compensates).
            if (steer) SetComposerRing("steer");
            else SetComposerRing(_input != null && _input.IsKeyboardFocused ? "focus" : "rest");
        }
        // Placeholder + destination indicator follow the steer state. When the active fleet run
        // finishes (OnSelect sets _activeFleetUrl = null) or the user opens a normal local chat
        // (OpenConversation / NewChat null it out), this reconciles back to the normal placeholder
        // and hides the indicator — every steer-exit path calls RefreshSteerVisual().
        if (_inputHint != null)
        {
            if (steer)
                _inputHint.Text = _lang == 0 ? "この会話への割り込みを送信…" : "Send an interrupt to this conversation…";
            else
                _inputHint.Text = _lang == 0 ? "メッセージを入力…" : "Type a message…";
        }
        if (_steerHint != null)
        {
            if (steer)
            {
                string target = (_conv != null && !string.IsNullOrEmpty(_conv.Name)) ? _conv.Name : "";
                string dest;
                if (_lang == 0)
                    dest = string.IsNullOrEmpty(target) ? "送信先: Fleet会話" : ("送信先: " + target + " / Fleet会話");
                else
                    dest = string.IsNullOrEmpty(target) ? "To: Fleet" : ("To: " + target + " / Fleet");
                _steerHint.Text = dest;
                _steerHint.Visibility = Visibility.Visible;
            }
            else
            {
                _steerHint.Visibility = Visibility.Collapsed;
            }
        }
    }

    void UpdateChrome()
    {
        _newBtn.Content = T("newchat_btn"); _send.Content = T("send");
        if (_manageBtn != null) _manageBtn.Content = T("manage_btn");
        // Iconized footer (Wave 2): icons don't hold text, so re-localize their TOOLTIPS and refresh
        // the theme glyph on a language toggle (the glyph itself is state- not language-driven).
        if (_themeBtn != null) { _themeBtn.Content = MakeIcon(_dark ? "light_mode" : "dark_mode", 18); _themeBtn.ToolTip = T("tip_theme"); }
        if (_langBtn != null) _langBtn.ToolTip = T("tip_lang");
        if (_cockpitBtn != null) _cockpitBtn.ToolTip = T("tip_cockpit");
        if (_dotHit != null) _dotHit.ToolTip = T("dot_click_tip");   // header dot tooltip follows the language
        if (_attachBtn != null) _attachBtn.ToolTip = T("attach");   // "+" glyph is language-agnostic; re-localize tooltip only
        // Re-derive composer placeholder + steer destination indicator in the current language.
        // Routing through RefreshSteerVisual keeps the steer placeholder ("…割り込みを送信…")
        // from being clobbered by the normal placeholder during a language toggle.
        RefreshSteerVisual();
    }
}
