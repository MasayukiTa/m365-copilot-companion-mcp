// FleetCockpit.Window.cs -- window lifecycle: UI scale, DPI, scale toast, force-visible, relaunching the chat.
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

    // ── i18n ────────────────────────────────────────────────────────────────────
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool SetForegroundWindow(IntPtr hWnd);

    // Open (or foreground if already running) the main chat window -- the reverse of
    // CopilotChat.OpenCockpit, so you can return to the main screen FROM the fleet cockpit
    // (previously only main -> fleet existed).
    void OpenMain()
    {
        // Same "start it if absent, else foreground it" behaviour the card Open handlers now use;
        // the shared helper lives in ui/FleetCockpit.Commands.cs (EnsureMainChatRunning).
        EnsureMainChatRunning();
    }

    // A small centered self-fading overlay showing the current zoom % (e.g. "125%"). Built once per
    // BuildChrome; ShowScaleToast() sets the text, makes it visible, and restarts the fade timer.
    UIElement BuildScaleToast()
    {
        _scaleToast = new Border();
        _scaleToast.HorizontalAlignment = HorizontalAlignment.Center;
        _scaleToast.VerticalAlignment = VerticalAlignment.Top;
        _scaleToast.Margin = new Thickness(0, 16, 0, 0);
        _scaleToast.Padding = new Thickness(16, 7, 16, 7);
        _scaleToast.CornerRadius = new CornerRadius(Theme.RadCard);
        _scaleToast.Background = Theme.Br(Theme.Surface(_dark));
        _scaleToast.BorderBrush = Border;
        _scaleToast.BorderThickness = new Thickness(1);
        _scaleToast.IsHitTestVisible = false;
        _scaleToast.Visibility = Visibility.Collapsed;
        _scaleToast.Effect = new System.Windows.Media.Effects.DropShadowEffect
        { BlurRadius = 14, ShadowDepth = 2, Opacity = 0.22, Color = C("#000000") };
        _scaleToastText = new TextBlock();
        _scaleToastText.Foreground = Fg; _scaleToastText.FontSize = 15;
        _scaleToastText.FontWeight = FontWeights.SemiBold;
        _scaleToast.Child = _scaleToastText;
        return _scaleToast;
    }

    void ShowScaleToast() { ShowScaleToast(null); }
    // overrideText != null shows that literal (e.g. "自動" when entering auto mode); otherwise the %.
    void ShowScaleToast(string overrideText)
    {
        if (_scaleToast == null || _scaleToastText == null) return;
        _scaleToastText.Text = overrideText != null
            ? overrideText
            : T("ui_scale") + "  " + ((int)Math.Round(_uiScale * 100)) + "%";
        _scaleToast.BeginAnimation(UIElement.OpacityProperty, null);
        _scaleToast.Opacity = 1.0;
        _scaleToast.Visibility = Visibility.Visible;
        if (_scaleToastTimer == null)
        {
            _scaleToastTimer = new DispatcherTimer();
            _scaleToastTimer.Interval = TimeSpan.FromMilliseconds(900);
            _scaleToastTimer.Tick += delegate
            {
                _scaleToastTimer.Stop();
                if (_scaleToast == null) return;
                var fade = new DoubleAnimation(1.0, 0.0, new Duration(TimeSpan.FromMilliseconds(450)));
                fade.Completed += delegate { if (_scaleToast != null) _scaleToast.Visibility = Visibility.Collapsed; };
                _scaleToast.BeginAnimation(UIElement.OpacityProperty, fade);
            };
        }
        _scaleToastTimer.Stop();
        _scaleToastTimer.Start();
    }

    // Current monitor scale = this window's device pixels per DIP (DPI/96). 1.0 at 100%, 1.5 at 150%.
    // Read from the live PresentationSource; falls back to 1.0 before the window is sourced.
    double CurrentMonitorScale()
    {
        try
        {
            var src = System.Windows.PresentationSource.FromVisual(this);
            if (src != null && src.CompositionTarget != null)
            {
                double m11 = src.CompositionTarget.TransformToDevice.M11;
                if (m11 > 0.01) return m11;
            }
        }
        catch (Exception) { }
        return 1.0;
    }

    // AUTO effective scale for a given monitor scale: target physical size / monitor's own DPI scale,
    // clamped to the LayoutTransform range. monitorScale (WPF DPI) × effective ≈ _scaleTarget, i.e. the
    // physical size is constant across monitors. Clamp can cap it on extreme monitors (see numeric proof).
    double EffectiveAutoScale(double monitorScale)
    {
        if (monitorScale < 0.01) monitorScale = 1.0;
        return Math.Max(0.8, Math.Min(2.0, _scaleTarget / monitorScale));
    }

    // Push the current _uiScale onto the live LayoutTransform + gear label. Pure apply (no persist).
    void PushScaleToTransform()
    {
        if (_rootScale != null) { _rootScale.ScaleX = _uiScale; _rootScale.ScaleY = _uiScale; }
        if (_uiScaleVal != null) _uiScaleVal.Text = AutoLabelText();
    }

    // Recompute + apply the AUTO effective scale for THIS window's current monitor. Silent by default
    // (DPI-change recompute must not toast). Does NOT persist ui_scale (auto marker already persisted).
    void ApplyAutoScale(bool toast)
    {
        _uiScale = EffectiveAutoScale(CurrentMonitorScale());
        PushScaleToTransform();
        if (toast) ShowScaleToast(T("ui_auto"));
    }

    // Gear value-label text: "自動 (130%)" in auto, plain "130%" in manual.
    string AutoLabelText()
    {
        int pct = (int)Math.Round(_uiScale * 100);
        return _uiAuto ? (T("ui_auto") + " (" + pct + "%)") : (pct + "%");
    }

    // MANUAL apply: clamp 0.8–2.0, leave/enter manual mode, push to transform, persist ui_scale=number.
    void ApplyUiScale(double v, bool persist) { ApplyUiScale(v, persist, true); }
    void ApplyUiScale(double v, bool persist, bool toast)
    {
        _uiAuto = false;
        _uiScale = Math.Max(0.8, Math.Min(2.0, Math.Round(v, 2)));
        PushScaleToTransform();
        RefreshUiScaleControls();
        if (persist) { SaveKey("ui_scale", _uiScale.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture)); _uiScaleLoaded = true; }
        if (toast) ShowScaleToast();
    }

    // Switch to AUTO: persist ui_scale=auto, recompute for the current monitor, and (optionally) toast.
    void EnableAutoScale(bool toast)
    {
        _uiAuto = true;
        SaveKey("ui_scale", "auto");
        _uiScaleLoaded = true;
        ApplyAutoScale(toast);
        RefreshUiScaleControls();
    }

    // Keep the gear-popup toggle/label/steppers in sync with the current mode (called on mode changes).
    void RefreshUiScaleControls()
    {
        if (_uiScaleVal != null) _uiScaleVal.Text = AutoLabelText();
        if (_uiAutoBtn != null) _uiAutoBtn.Content = _uiAuto ? T("ui_auto_on") : T("ui_auto_off");
        // In auto mode the manual steppers are meaningless (value is monitor-derived) -> disable them.
        if (_uiScaleMinus != null) _uiScaleMinus.IsEnabled = !_uiAuto;
        if (_uiScalePlus != null) _uiScalePlus.IsEnabled = !_uiAuto;
    }

    // Window-level Ctrl+Plus / Ctrl+Minus / Ctrl+0 zoom. Handles both the main-row and NumPad keys.
    // OemPlus/OemMinus are the US-layout '=' and '-' keys; Add/Subtract are the numeric-keypad ones.
    // +/- while in auto -> switch to MANUAL at the resulting number; Ctrl+0 -> back to AUTO.
    void OnScaleKeyDown(object sender, KeyEventArgs e)
    {
        if ((Keyboard.Modifiers & ModifierKeys.Control) == 0) return;
        if (e.Key == Key.OemPlus || e.Key == Key.Add)
        { ApplyUiScale(_uiScale + 0.1, true); e.Handled = true; }
        else if (e.Key == Key.OemMinus || e.Key == Key.Subtract)
        { ApplyUiScale(_uiScale - 0.1, true); e.Handled = true; }
        else if (e.Key == Key.D0 || e.Key == Key.NumPad0)
        { EnableAutoScale(true); e.Handled = true; }   // Ctrl+0 = "back to automatic"
    }

    // Ctrl+MouseWheel = ±0.1 per notch (-> MANUAL). Swallowed only when Ctrl is held so normal list
    // scroll (no modifier) is untouched.
    void OnScaleMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if ((Keyboard.Modifiers & ModifierKeys.Control) == 0) return;
        ApplyUiScale(_uiScale + (e.Delta > 0 ? 0.1 : -0.1), true);
        e.Handled = true;
    }

    // PMv2 fires this when the window is dragged onto a differently-scaled monitor. In AUTO mode we
    // recompute the effective scale from the NEW monitor's DPI (neu.DpiScaleX = DPI/96) so the physical
    // size stays constant -- SILENTLY (an automatic recompute must not flash the % toast). Manual mode
    // is untouched: WPF has already relaid out at the new DPI and the fixed number still holds. Never
    // throw from here.
    protected override void OnDpiChanged(DpiScale oldDpi, DpiScale newDpi)
    {
        try { base.OnDpiChanged(oldDpi, newDpi); } catch (Exception) { }
        try
        {
            if (_uiAuto)
            {
                double ms = (newDpi.DpiScaleX > 0.01) ? newDpi.DpiScaleX : CurrentMonitorScale();
                _uiScale = EffectiveAutoScale(ms);
                PushScaleToTransform();   // silent: no toast on automatic DPI-change recompute
            }
        }
        catch (Exception) { }
    }

    // First run / startup apply. AUTO is the default for NEW users: seed _scaleTarget from the PRIMARY
    // monitor's scale (the "comfortable size" the user is used to) and compute the per-monitor effective
    // scale now. If a scale was already persisted (manual OR auto, from either app) honor it and just
    // reflect it on the live transform. Guarded by _uiScaleLoaded so a chosen scale is never overridden.
    // Windows API used to break out of an inherited-hidden startup state (see ForceVisibleOnce).
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool RedrawWindow(IntPtr hWnd, IntPtr lprc, IntPtr hrgn, uint flags);

    // The daily launcher chain is fully windowless: a desktop shortcut runs
    // scripts\start_all_hidden.vbs, which runs start_all.ps1 with -WindowStyle Hidden
    // (SW_HIDE), which in turn starts this app. A process started from an SW_HIDE parent
    // INHERITS that "hidden" show-state through STARTUPINFO.wShowWindow, so WPF creates
    // the HWND without a proper first paint: DWM never gets a composed surface and the
    // window renders as black / stale rectangles on screen -- even though the visual tree
    // itself is fine (PrintWindow of the same HWND returns the correct content).
    // Forcing SW_SHOW + a full redraw once, right after the HWND exists, discards the
    // inherited state and makes the window paint normally. Cheap, idempotent, and a no-op
    // when the app was launched normally (already visible).
    // AND IT DOES NOT ALWAYS WORK -- measured 2026-08-28, recorded here so the next person
    // does not spend the afternoon re-discovering it. OnSourceInitialized fires when the HWND
    // exists, BEFORE WPF applies the show-state, so an inherited hidden state overwrites this
    // call: the process then runs with a fully built window ("Fleet Cockpit", style 0x6CF0000,
    // 367 MB resident, responding) that WS_VISIBLE was never set on. Moving the call to
    // OnContentRendered does not help either, because a window that never becomes visible
    // never renders its content, so that override does not fire at all.
    //
    // The rescue therefore has to come from OUTSIDE the process, and it does: CopilotChat's
    // OpenCockpit finds this window by style rather than by MainWindowHandle (which is 0 for
    // an invisible window) and issues the SW_SHOW itself.
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

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        ForceVisibleOnce();
        try
        {
            double monitorScale = CurrentMonitorScale();
            // Seed the target from the primary monitor's scale on first run (default 1.5 fallback).
            if (!_scaleTargetLoaded)
            {
                _scaleTarget = Math.Max(0.8, Math.Min(3.0, monitorScale));
                if (_scaleTarget < 0.81) _scaleTarget = 1.5;   // 100% primary -> still target a comfy 1.5
                SaveKey("ui_scale_target", _scaleTarget.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture));
                _scaleTargetLoaded = true;
            }
            if (!_uiScaleLoaded)
            {
                // NEW user default = AUTO. Persist the marker and compute for this monitor (silent).
                EnableAutoScale(false);
            }
            else if (_uiAuto)
            {
                ApplyAutoScale(false);   // reflect auto for THIS monitor silently
            }
            else
            {
                double m11 = 1.0;
                var src = System.Windows.PresentationSource.FromVisual(this);
                if (src != null && src.CompositionTarget != null) m11 = src.CompositionTarget.TransformToDevice.M11;
                double pxWidth = SystemParameters.PrimaryScreenWidth * m11;   // DIPs -> physical pixels
                if (pxWidth >= 2560 && Math.Abs(m11 - 1.0) < 0.001)
                    ApplyUiScale(1.25, true, true);    // announce the one-time 4K bump
                else
                    ApplyUiScale(1.0, true, false);    // silent explicit default so it never re-triggers
            }
        }
        catch (Exception) { }
    }
}
