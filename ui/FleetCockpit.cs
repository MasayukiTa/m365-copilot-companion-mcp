// FleetCockpit.cs -- native Windows (WPF) LIVE cockpit for parallel execution.
//
// relay/fleet_runner.py drives N autonomous Copilot conversations at once and writes a
// live snapshot to .fleet/status.json after every round-robin sweep. This window tails
// that JSON and renders one live card per goal. You can also release a running one from
// here (writes one command file into .fleet/commands.d/, which the fleet consumes -> stops + frees its tab).
//
//   [ fleet_runner.py ] <--(commands.d/)--- [ this ]
//                       --(status.json)----->
//
// Icons are Google Material Symbols, rendered as vector geometry from
// ui/assets/material_glyphs.json (NO emoji). Palette = the sibling app slate; status shows
// as a soft band border + faint tint (not a harsh fill) plus a saturated pill. Theme and
// language follow the shared settings.txt live, so toggling the chat retints this too.
// Build: ui\build_cockpit.bat  (Windows csc.exe, legacy C# 5).
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

class CockpitProgram
{
    // Per-Monitor V2 DPI: WPF would otherwise run System-DPI-aware (render once at the PRIMARY
    // monitor's DPI, then Windows bitmap-stretches that onto a differently-scaled display like a
    // 2560x1440 external monitor -> visibly grainy text/edges). PROCESS awareness is declared in
    // app.manifest (embedded via /win32manifest; the OS loader reads it at process creation, so it
    // cannot lose the startup race a programmatic SetProcessDpiAwarenessContext call would). The
    // switch below is the WPF half: it turns on WPF's per-monitor RELAYOUT so the tree re-renders
    // vector-crisp at each monitor's native DPI. Both are needed; must precede any WPF type use.
    [STAThread]
    static void Main(string[] args)
    {
        try { AppContext.SetSwitch("Switch.System.Windows.DoNotScaleForDpiChanges", false); } catch { }
        if (args.Length >= 2 && args[0].Equals("--approval-gate", StringComparison.OrdinalIgnoreCase))
        {
            bool createdNew = false;
            bool ownsMutex = false;
            using (var mutex = new Mutex(true, "Local\\M365CompanionApprovalPrompt", out createdNew))
            {
                try
                {
                    ownsMutex = createdNew;
                    if (!ownsMutex)
                    {
                        try { ownsMutex = mutex.WaitOne(0, false); }
                        catch (AbandonedMutexException) { ownsMutex = true; }
                    }
                    // The existing prompt polls the gate directory and will surface this request.
                    if (!ownsMutex) return;
                    new Application().Run(new ApprovalPromptWindow(args[1]));
                }
                finally { if (ownsMutex) try { mutex.ReleaseMutex(); } catch { } }
            }
            return;
        }
        // THE AUTHORITY RECORD, OPENED DIRECTLY. The self-improvement dashboard already holds
        // everything a person needs when the constitution is re-signed -- what changed, the
        // ledger, the frozen-set comparison this window computes ITSELF rather than believing
        // python, and the button that withdraws the last re-signing. What it did not have was
        // a way in: the notification about such an act opened a text file of commands to paste
        // into a terminal, and the operator's reaction to that was the obvious one -- "and then
        // what am I supposed to do with it".
        //
        // Same shape as --approval-gate above, and for the same reason: a notification about a
        // decision should land the person on the control, not on a description of it.
        if (args.Length >= 1 && args[0].Equals("--authority", StringComparison.OrdinalIgnoreCase))
        {
            // SAME SHAPE MEANS THE MUTEX TOO. The paragraph above said this was built like
            // --approval-gate and then left out the one mechanism that makes that shape work,
            // so every re-signing opened its own window. A working day of them is a window
            // storm, and a storm of identical windows is worse than no notification: the
            // operator closes them all reflexively and the one that mattered goes with them.
            //
            // The second instance does NOT return silently the way --approval-gate does. That
            // path is safe because the running prompt polls the gate directory and will show
            // the new request by itself; nothing here polls. A notification whose click does
            // nothing visible is the exact complaint that started this work, so when a window
            // is already open we raise it instead.
            bool createdNew = false;
            bool ownsMutex = false;
            using (var mutex = new Mutex(true, "Local\\M365CompanionAuthorityDashboard", out createdNew))
            {
                try
                {
                    ownsMutex = createdNew;
                    if (!ownsMutex)
                    {
                        try { ownsMutex = mutex.WaitOne(0, false); }
                        catch (AbandonedMutexException) { ownsMutex = true; }
                    }
                    if (!ownsMutex) { RaiseExistingDashboard(); return; }
                    new Application().Run(new SelfImproveDashboardWindow());
                }
                finally { if (ownsMutex) try { mutex.ReleaseMutex(); } catch { } }
            }
            return;
        }
        // --selftest: construct the window the ordinary way, pump once, exit (WindowSelfTest.cs).
        // Its status.json lives in an empty scratch directory, not the real .fleet, and the
        // health poll -- which reaches the network and can start the stack -- does not start.
        if (args.Length >= 1 && args[0].Equals("--selftest", StringComparison.OrdinalIgnoreCase))
        {
            string scratch = Path.Combine(WindowSelfTest.ScratchDir("cockpit-fleet"), "status.json");
            Environment.Exit(WindowSelfTest.Run(delegate { return new CockpitWindow(scratch); }));
        }
        string path = args.Length > 0 ? args[0] : null;
        new Application().Run(new CockpitWindow(path));
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool SetForegroundWindow(IntPtr hWnd);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    const int SW_RESTORE = 9;

    // Best effort by design: if the handle cannot be resolved the window is still open, and a
    // dashboard that failed to come forward is a far smaller harm than a second one appearing.
    static void RaiseExistingDashboard()
    {
        try
        {
            var me = System.Diagnostics.Process.GetCurrentProcess();
            foreach (var p in System.Diagnostics.Process.GetProcessesByName(me.ProcessName))
            {
                if (p.Id == me.Id) continue;
                IntPtr h = p.MainWindowHandle;
                if (h == IntPtr.Zero) continue;
                // The exe also runs as the ordinary cockpit and as the approval prompt. Matching
                // the process name alone would drag one of those to the front instead.
                if (p.MainWindowTitle != SelfImproveDashboardWindow.WindowTitle) continue;
                ShowWindow(h, SW_RESTORE);
                SetForegroundWindow(h);
                return;
            }
        }
        catch { }
    }
}


// CockpitWindow is a partial class: its members are spread over ui/FleetCockpit.cs and the
// ui/FleetCockpit.<Theme>.cs parts. Every part is on the Build "FleetCockpit" line of ui/rebuild_ui.ps1.
partial class CockpitWindow : Window
{
    static Color C(string hex) { return (Color)ColorConverter.ConvertFromString(hex); }
    static double NowUnix() { return (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }

    // theme-dependent brushes
    Brush Bg, CardBg, Border, Fg, Muted, QuoteBg, BtnBg;
    Brush Accent;   // primary-action color; theme-dependent (spec), set in ApplyThemeBrushes
    // Foreground for text sitting ON a saturated fill. It has to be theme-dependent for the same
    // reason the fill is: white reads on the light theme's orange and green (5.14 / 5.02) and on
    // neither of the dark theme's (2.80 / 2.28). This window had `White` instead, hard-coded, on
    // nine buttons -- including the one that starts a run.
    Brush AccentFg;
    // The fill under that white text. Separate from Accent because in the dark theme they must
    // differ; assigned in the same place so they cannot drift apart.
    Brush AccentFill, SuccessFill;
    static readonly Brush White = new SolidColorBrush(C("#ffffff"));

    bool _dark = true;
    int _lang = 0;          // 0 = Japanese, 1 = English
    int _maxtabs = 200;
    bool _autoscale = false;   // RAM-aware autoscale on/off (NEW)
    int _autoMax = 200;       // lane ceiling (上限): goals that may run at once. DERIVED, not a literal:
                               // ceil(100 RPM x 60 s per turn / 60 x 2) = 200, the same value as
                               // tools/settings_keys.py (maxtabs / autoscale_max). Throughput is bounded by
                               // the send token bucket (rate_ceiling_rpm); free RAM only prices NEW
                               // admissions and never stops a running goal. No browser tab is involved.
    double _diskFloor = 6.0;   // admission disk floor (GB) -> settings.txt disk_floor_gb=; user-editable
                               // in the settings panel. Persisted via SaveKey AND pushed live to a
                               // running fleet via {"set_disk_floor_gb":N} (fleet_runner.py ~L561).
    //: MUST MATCH tools/settings_keys.py's declared default for ram_floor_mb. It said 2048
    //: while the coordinator used 1400 and the admission gates used 512 -- three owners of one
    //: default, none of them wrong about itself. test_a_setting_declares_when_it_takes_effect
    //: fails if this drifts again.
    double _ramFloor = 512.0;  // admission RAM floor (MB) -> settings.txt ram_floor_mb=; user-editable.
                               // The free RAM the autoscale keeps for the user (RAM analog of the disk
                               // floor). Persisted via SaveKey AND pushed live via {"set_ram_floor_mb":N}.
    string _effort = "auto";   // effort mode min|max|ultra|auto -> settings.txt effort= (NEW)
    string _effortPolicy = "off";   // effort policy off|shadow|on -> settings.txt effort_policy= (EffortPolicy.cs)
    // Split a goal into independent sub-goals, run them in parallel, and merge the answers.
    // For work whose SIZE is the problem: a goal that cannot fit in one conversation fails at
    // the conversation, not at the work. Off by default -- a goal that fits should not pay for
    // a split turn and a merge turn. Toggled with /fanout on|off, like /effort and /approval.
    // ON, matching relay.fleet_runner's own default since 2026-09-13. Every goal is still
    // judged separately (offline triage, then the agent, which may answer NO_SPLIT), so a goal
    // that fits costs nothing; this only decides whether the question is ever asked.
    bool _fanout = true;       // -> settings.txt fanout= ; absent key = ON (FanoutView.DefaultOn)
    // per-tree fan-out budget -> settings.txt fanout_max_total / _active / _turns / _wall_min
    int[] _fbVals = (int[])FanoutBudgetView.Defaults.Clone();
    TextBox[] _fbBox = new TextBox[4];
    TextBlock _fbLbl, _fbNow, _fbPending;
    TextBlock[] _fbCap = new TextBlock[4];
    // split depth 1|2|3 -> settings.txt fanout_max_depth ; absent key = 2 (FanoutDepthView.Default)
    int _fdVal = FanoutDepthView.Default;
    ComboBox _fdBox;
    TextBlock _fdLbl, _fdNow, _fdPending;
    // sibling write scope off|shadow -> settings.txt fanout_write_scope ; absent key = off (WriteScopeView.Default)
    string _wsVal = WriteScopeView.Default;
    // load-aware routing off|shadow -> settings.txt fanout_load_routing ; absent key = off (LoadRoutingView.Default)
    string _lrVal = LoadRoutingView.Default;
    ComboBox _lrBox;
    TextBlock _lrLbl, _lrNow, _lrPending;
    // team formation off|shadow -> settings.txt team_mode ; absent key = off (TeamModeView.Default)
    string _tmVal = TeamModeView.Default;
    ComboBox _tmBox;
    TextBlock _tmLbl, _tmNow, _tmPending;
    // relay findings off|shadow|on -> settings.txt relay_findings ; absent key = off (RelayFindingsView.Default)
    string _rfVal = RelayFindingsView.Default;
    ComboBox _rfBox;
    TextBlock _rfLbl, _rfNow, _rfPending;
    // campaign board off|p0 -> settings.txt agent_board_mode ; absent key = off (AgentBoardView.Default)
    string _abVal = AgentBoardView.Default;
    ComboBox _abBox;
    TextBlock _abLbl, _abNow, _abPending;
    // role and protocol version record off|on -> settings.txt org_version_record ; absent key = off (OrgVersionView.Default)
    string _ovVal = OrgVersionView.Default;
    ComboBox _ovBox;
    TextBlock _ovLbl, _ovNow, _ovPending;
    // genotype id record off|on -> settings.txt org_genotype_record ; absent key = off (OrgGenotypeView.Default)
    string _ogVal = OrgGenotypeView.Default;
    ComboBox _ogBox;
    TextBlock _ogLbl, _ogNow, _ogPending;
    // org change proposals off|on -> settings.txt org_proposals ; absent key = off (OrgProposalView.Default)
    string _opVal = OrgProposalView.Default;
    ComboBox _opBox;
    TextBlock _opLbl, _opNow, _opPending;
    // org proposal shadow evaluation off|shadow -> settings.txt org_shadow_eval ; absent key = off (OrgShadowView.Default)
    string _osVal = OrgShadowView.Default;
    string _oabVal = OrgAbView.Default;
    ComboBox _osBox;
    ComboBox _oabBox;
    TextBlock _osLbl, _osNow, _osPending;
    TextBlock _oabLbl, _oabNow, _oabPending;
    // hierarchical merge off|on -> settings.txt fanout_hierarchical_merge ; absent key = off
    string _hmVal = HierarchicalMergeView.Default;
    ComboBox _hmBox;
    TextBlock _hmLbl, _hmNow, _hmPending;
    // merge conversation fresh|parent -> settings.txt merge_conversation ; absent key = fresh
    string _mcVal = MergeConversationView.Default;
    ComboBox _mcBox;
    TextBlock _mcLbl, _mcNow, _mcPending;
    ComboBox _wsBox;
    TextBlock _wsLbl, _wsNow, _wsPending;
    // auto-resume of an interrupted run off|on -> settings.txt fleet_auto_resume ; absent key = on
    string _arVal = AutoResumeView.Default;
    ComboBox _arBox;
    TextBlock _arLbl, _arNow, _arPending;
    // supervisor self-restart off|on -> settings.txt supervisor_self_restart ; absent key = on
    string _srVal = SupervisorCodeView.Default;
    ComboBox _srBox;
    TextBlock _srLbl, _srNow;
    DateTime _srReadAt = DateTime.MinValue;   // supervisor_state.json is re-read at most every 5 s
    string _srText = null;
    // tool route connector|connector-less -> settings.txt tool_transport ; absent key = connector-less, or connector on a PC set up for it (ToolRouteView.UnsetValue)
    string _trVal = ToolRouteView.Default;
    ComboBox _trBox;
    TextBlock _trLbl;
    // consent-card auto-approval on|off -> settings.txt consent_auto_approve ; absent key = on
    string _caVal = ConsentApproveView.Default;
    ComboBox _caBox;
    TextBlock _caLbl;
    // resident tools in my own chats off|on -> settings.txt resident_tools ; absent key = off
    string _rtVal = ResidentToolsView.Default;
    ComboBox _rtBox;
    TextBlock _rtLbl;
    // read-only tool result cache off|shadow -> settings.txt result_cache ; absent key = off
    string _rcVal = ResultCacheView.Default;
    ComboBox _rcBox;
    TextBlock _rcLbl;
    // tool-call check interval 0|15|30|60 minutes -> settings.txt tool_probe_idle_min ; absent key = 30
    string _tpVal = ToolProbeView.Default;
    ComboBox _tpBox;
    TextBlock _tpLbl, _tpNow, _tpPending;
    double _tpStateMtimeTicks = -1;          // the state file is re-read only when it changed
    Dictionary<string, object> _tpState;     // .fleet/tool_probe_state.json as last read (null = none)
    string _approval = "run";  // approval mode run|plan|auto -> settings.txt approval=
    string _runtimeMode = "fleet"; // next launch: fleet | durable -> settings.txt runtime=
    bool _durableEnqueuePending = false; // one durable campaign intake process at a time
    bool _durableStartPending = false; // initial durable launch awaits LOCAL_LOOP status acceptance
    bool _fleetLaunchPending = false; // fresh Start waits for a closing prior coordinator
    bool _paused = false;      // local fleet pause/resume toggle state (NEW)
    // FIX B: optimistic "stopping" state set the instant Stop is clicked (dims non-terminal cards +
    // flips the Stop button's tooltip/icon) so the click never feels dead for the ~700ms sweep.
    // Cleared once the sweep confirms the run is no longer live, or a different run has started.
    bool _stopping = false;
    string _stopStartedMarker = "";   // `started` marker captured at Stop-click time (detects a NEW run)
    bool _autoArchive = false;  // P2: when ON, move this run's completed cards to History as soon
                                // as the RUN reaches its finished state -> settings.txt autoarchive=
    Button _autoArchiveBtn;     // gear-popup toggle for _autoArchive
    string _archivedRunStarted = "";   // `started` of the run already auto-archived (fire once/run)
    string _tailArchivedRunStarted = "";  // `started` of the run whose FINAL sweep was archived
    // P2 history search: live case-insensitive substring filter over title+result, debounced.
    TextBox _histSearchBox;
    TextBlock _histHint;   // placeholder drawn behind an empty filter box (WPF has none of its own)
    string _histQuery = "";
    DispatcherTimer _histSearchTimer;
    long _settingsMtime = 0;

    // ── UI SCALE (4K readability) ────────────────────────────────────────────────
    // A window-level zoom: a ScaleTransform is set as the LayoutTransform on the SINGLE
    // root element that hosts ALL chrome (header + pinned toolbar + health strip + list +
    // composer), so everything scales AND reflows together. Persisted as ui_scale= in the
    // SHARED settings.txt with identical semantics to the chat app, so both apps zoom in
    // lock-step (an external edit is picked up on the next mtime-triggered LoadSettings).
    double _uiScale = 1.0;            // current EFFECTIVE zoom (clamped 0.8–2.0) -> pushed to _rootScale
    bool _uiScaleLoaded = false;      // true once a ui_scale= line was read from settings.txt
    // AUTO mode: ui_scale= may hold the literal "auto" instead of a number. In auto mode the
    // effective LayoutTransform scale is derived per-monitor so the PHYSICAL size stays constant:
    //   effective = clamp(_scaleTarget / currentMonitorScale, 0.8, 2.0)
    // where currentMonitorScale = this window's DPI/96. Net physical = monitorScale × effective ≈
    // _scaleTarget on EVERY monitor (the divide-by-monitorScale exactly counteracts WPF's own DPI
    // relayout, so we never double-apply). Manual mode ignores _scaleTarget and uses a fixed number.
    bool _uiAuto = true;              // true = auto mode (default for new users / ui_scale=auto)
    double _scaleTarget = 1.5;        // desired constant physical scale (persisted as ui_scale_target)
    bool _scaleTargetLoaded = false;  // true once ui_scale_target was read (else seed from primary DPI)
    Button _uiAutoBtn;                // gear-popup "自動"/"Auto" toggle (rebuilt each open)
    Button _uiScaleMinus, _uiScalePlus;  // gear-popup steppers (disabled while auto is on)
    ScaleTransform _rootScale;        // the LayoutTransform applied on BuildChrome's root
    // Self-fading "125%" overlay shown briefly on each change (reuses no other affordance
    // because the composer note is not visible from every context, e.g. gear popup / shortcut).
    Border _scaleToast;
    TextBlock _scaleToastText;
    DispatcherTimer _scaleToastTimer;
    // gear-popup live value label + stepper refs (re-themed nowhere else; rebuilt each open)
    TextBlock _uiScaleVal;

    // _fleetDir is the .fleet state dir. It replaced _commandsPath when the cockpit stopped
    // writing commands.json directly: commands now go one-per-file into <_fleetDir>/commands.d
    // through FleetCommands.Write. Nothing here needs the old aggregate path any more.
    readonly string _statusPath, _fleetDir, _historyPath, _openPath;
    string _convsPath, _hiddenPath, _resumeDismissPath, _clearedLogPath;
    System.Collections.Generic.HashSet<string> _archivedKeys = new System.Collections.Generic.HashSet<string>();
    // Persistent "cleared" set: keys of TERMINAL cards the user dismissed via Clear. Survives
    // the runner regenerating status.json every second, so cleared cards stay gone mid-run.
    // Key is run+worker (started#name) -- same scheme as the history archive -- so a NEW run
    // (different `started`) never inherits an old hide, and a reused worker name can't collide.
    System.Collections.Generic.HashSet<string> _hiddenKeys = new System.Collections.Generic.HashSet<string>();
    List<object> _history = new List<object>();
    int _openSeq = 0;

    TextBlock _header, _sub;
    WrapPanel _subChips;   // Feature 2: discrete Pill() chips replacing _sub's single concatenated sentence
    TextBlock _workerChip;       // live "N workers" neutral chip in the header controls row
    Border _workerChipBorder;   // the Border wrapping _workerChip (for PaintChrome re-theming)
    Button _themeBtn, _langBtn, _mainBtn, _siBtn;
    Button _approvalCenterBtn;
    TextBlock _approvalCenterLabel, _approvalCenterBadgeText;
    Border _approvalCenterBadge;
    Window _approvalCenterWindow;
    StackPanel _approvalPendingHost, _approvalRecentHost;
    TextBlock _approvalCenterSummary;
    string _approvalCenterSig = "";
    List<Dictionary<string, object>> _gateCache = new List<Dictionary<string, object>>();
    double _gateCacheAt = 0;
    Button _overflowBtn;
    System.Windows.Controls.Primitives.Popup _overflowPopup;
    Border _headBar;
    ListBox _list;                 // virtualizing host for the card/history rows
    // PINNED FILTER BAR: the すべて/実行中/承認待ち/完了 segmented control used to be row 0 INSIDE
    // _list, so it scrolled with the content and — because the list is bottom-anchored to the
    // composer via a '*' spacer — its Y position drifted as tasks accumulated (user complaint:
    // "動きまくって結構うっとうしい"). It now lives in a fixed host docked ABOVE _list (never
    // inside the scroll), so it is stationary regardless of card count / scroll offset.
    Border _pinnedToolbarHost;
    string _pinnedToolbarSig = "";
    // Persistent row backing store. We bind this to _list.ItemsSource ONCE and only ever mutate
    // it in place (SetRows reconciles item-by-item). Reassigning ItemsSource with a fresh List --
    // the old behaviour -- made the VirtualizingStackPanel treat every update as a collection
    // Reset: it discarded realized containers and snapped the scroll to the top, which is exactly
    // the "expand jumps the view" / "scrolling fights the 700ms tick" complaint. In-place edits
    // raise targeted Add/Remove/Replace notifications instead, so the pixel scroll offset stays.
    readonly ObservableCollection<object> _rows = new ObservableCollection<object>();
    // The lists handed to BuildCardToolbar (all/shown) are stashed here so the
    // ItemTemplate converter can rebuild the toolbar row on demand for a recycled container.
    List<Dictionary<string, object>> _toolbarAll = new List<Dictionary<string, object>>();
    List<Dictionary<string, object>> _toolbarShown = new List<Dictionary<string, object>>();
    DispatcherTimer _timer;
    string _lastSig = "";
    // MaxJsonLength IS NOT OPTIONAL HERE. JavaScriptSerializer defaults to 2,097,152 chars,
    // and .fleet/status.json crosses that at roughly 266 workers (~7.5 KB each). The
    // 2026-09-09 run wrote 900 workers = 6,774,180 bytes, so DeserializeObject threw on every
    // tick from about a third of the way in. ReadStatus caught it and returned null, OnTick
    // read null as "idle" and returned BEFORE ArchiveTerminal / ArchiveRunTailOnce /
    // MaybeAutoArchive -- so SaveHistory never ran and .fleet/history.json was never even
    // created. A full run of finished work accumulated nowhere, silently, for twenty hours.
    //
    // This is a ceiling, not a one-off: every run past ~266 workers hits it. The same
    // instance also SERIALIZES history.json, which meets the identical limit from the other
    // side once history itself grows past 2 MB, so raising it here covers both directions.
    JavaScriptSerializer _js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

    // Cached meta string for the directive-band row Sig (avoids per-tick rebuild when nothing changed).
    string _directiveBandMeta = "";
    // The on-board (non-History) workers the directive band aggregates its goals/lane counts from.
    List<Dictionary<string, object>> _directiveBandWorkers = new List<Dictionary<string, object>>();

    // ── A2-2: Evidence Spine (left panel) ────────────────────────────────────────
    // A fixed-width ~220px left column that shows an [COMPUTED] execution timeline for
    // the run derived from transcript turn timestamps. Labeled honestly: "実行タイムライン" /
    // "Execution timeline". Only visible when a run exists (workers present).
    Border _spinePanel;              // the left column Border; col0 is zeroed when idle
    ColumnDefinition _spineCol;      // Grid col0 -- we zero its Width when idle
    string _spineSig = "";           // last rendered state; rebuild only when changed

    // ── A2-2: Composer mode ────────────────────────────────────────────────────────
    // When a run is active the bottom composer adapts: placeholder, hint, and button
    // shift from "add goals / Start" to "steer / Send". This bool tracks the last
    // rendered state so PaintComposerMode is called only when it changes.
    bool _composerRunActive = false;

    // Per-worker disclosure state (Claude-Code-style "> / v"). Collapsed is the default:
    // a collapsed card renders only a lightweight summary line and (Feature 1) a slim always-on
    // steer affordance for non-terminal workers -- the heavy tabs/logs/review detail still only
    // builds when EXPANDED. Keyed by worker name; survives re-renders.
    HashSet<string> _expanded = new HashSet<string>();

    // ── Feature 1: collapsed-card steer affordance -- keystroke-safety state ───────────────────
    // Cards are rebuilt wholesale (Card(w) called again) whenever RowSig for that worker changes,
    // which happens routinely for a LIVE worker (its `last`/`turn`/etc. stream every sweep) --
    // completely independent of whether the user is mid-type in its steer box. So a naive TextBox
    // would lose its text (rebuilt empty) and/or its focus (a brand new control instance) on the
    // very next ~700ms sweep. Three fields defend against that:
    //   _steerDraft     -- per-worker in-progress text, read back into the TextBox's Text on every
    //                      (re)build, so a torn-down-and-rebuilt box never loses what was typed.
    //   _steerFocusWorker -- which worker's steer box currently holds keyboard focus, so a rebuild
    //                      knows it needs to re-Focus() the replacement box afterward.
    //   _steerBoxRef    -- the CURRENTLY realized TextBox per worker, updated every time Card(w)
    //                      (re)builds one, so the post-render restore pass (see RenderCards) always
    //                      focuses the box that is actually still in the visual tree.
    Dictionary<string, string> _steerDraft = new Dictionary<string, string>();
    string _steerFocusWorker = null;
    Dictionary<string, TextBox> _steerBoxRef = new Dictionary<string, TextBox>();
    // last status snapshot rendered, kept so a chevron toggle can rebuild ONLY its one card
    // in place (no full 164-card re-render) -- that's what makes expand/collapse feel instant.
    Dictionary<string, object> _lastRoot;

    double _upm = 960;
    Dictionary<string, string> _glyphs = new Dictionary<string, string>();

    // 4-tab view filter: 0=All, 1=Active (non-terminal non-pending), 2=Needs input (awaiting), 3=Done.
    int _cardFilter = 0;

    // DEFAULT ON SINCE 2026-08-25, and the number that changed it: across 28 goals in six
    // runs, 25% came back with Copilot's canned "I couldn't respond to that". Not the same
    // goal twice -- the failure moved every run, was unaffected by concurrency (25% at one
    // worker, 25% at two), and the two goals that failed one run both passed when re-queued
    // unchanged. That is a transient, and a transient is exactly what a bounded retry is for:
    // at 25% independent, two attempts leave 6% and three leave 1.6%.
    //
    // Still capped, so a deterministically-failing task (the tool-denial ones) can never loop.
    // Counted by goal TEXT, so a re-queued copy -- which gets a new worker name -- shares the
    // original's budget. Manual retry is unaffected; this governs only the automatic re-queue.
    bool _autoRetry = true;
    int _autoRetryMax = 2;
    Dictionary<string, int> _autoRetryCount = new Dictionary<string, int>();
    HashSet<string> _autoRetriedWorkers = new HashSet<string>();   // RetryWorkerKey of workers already re-queued

    // Conversation retention. OWNER DECISION 2026-09-24: _retDays now DEFAULTS TO 90, not 0.
    // The store exists because history was disappearing, and a policy that started deleting
    // the day it shipped would have been that same loss arriving on a schedule -- but the
    // opt-in default meant operators who never found this dialog kept every conversation
    // forever, unbounded, on purpose. 90 is now what "never touched this control" means. An
    // operator who explicitly sets it to 0 still gets "keep everything": this field only ever
    // starts at 90 when settings.txt has no session_retention_days line at all (LoadSettings,
    // below, overwrites it -- including with 0 -- the moment the line exists). Mirrors
    // bridge/session_store.py's DEFAULT_RETENTION_DAYS and tools/settings_keys.py's declared
    // default for this key; test_the_panel_shows_the_same_default_the_fleet_uses
    // (tools/test_a_setting_declares_when_it_takes_effect.py) and ui/test_retention_settings.py
    // both fail if this literal drifts from either. _retMb is unrelated and unchanged: no
    // owner decision touched the size cap, so it keeps defaulting to "no cap".
    int _retDays = 90;         // settings.txt session_retention_days= ; 0 = keep forever, unset = 90
    int _retMb = 0;            // settings.txt session_max_mb=        ; 0 = no size cap
    TextBlock _retDaysValue, _retMbValue;

    // THE DENOMINATOR OF THE RATE STRIP, AND THE LINE ADMISSION HOLDS AT. Microsoft documents
    // 100 generative messages per minute per Dataverse environment, which is the default and
    // not the law -- refusals arriving while the strip still shows headroom mean the binding
    // limit is not the published one, and then the useful move is to lower this rather than
    // keep feeding a queue that is being refused. relay_fleet.rate_ceiling() reads the same
    // key, so the gauge and the gate can never disagree about where the line is.
    int _rateCeiling = 100;    // settings.txt rate_ceiling_rpm= ; 0 = no ceiling
    TextBlock _rateCeilingValue;

    // Working-directory retention. SEPARATE FROM THE CONVERSATION RETENTION ABOVE, and with a
    // different default on purpose: that store is history someone may want years later, this
    // one is logs of runs that have finished. These were environment variables only, which made
    // "the retention period is settable" half true -- and the half that was not was the half
    // writing 26 MB a day.
    int _fleetLogDays = 14;    // settings.txt fleet_log_days=
    int _fleetStoreDays = 30;  // settings.txt fleet_store_days=   (run transcripts)
    int _sidebarCap = 8;       // settings.txt sidebar_section_cap= (chat sidebar rows per section; 0 = all)
    TextBlock _sidebarCapValue;
    //: MUST MATCH tools/settings_keys.py's declared defaults, which
    //: test_the_panel_shows_the_same_default_the_fleet_uses pins. relay/fleet_retention.py
    //: read both of these on every run while no control here wrote either, so the panel's
    //: starting value is the only number an operator ever saw for them -- and until today
    //: there was no panel value at all.
    int _fleetScratchDays = 14;     // settings.txt fleet_scratch_days=
    int _fleetCompressHours = 6;    // settings.txt fleet_compress_hours=
    TextBlock _fleetLogDaysValue, _fleetStoreDaysValue;
    TextBlock _fleetScratchDaysValue, _fleetCompressHoursValue;
    // Index map: 0=server 1=tunnel 2=edge 3=signin 4=agent 5=tool(bridge probe).
    // SIZED BY THE COUNT, never by however many literals somebody typed. This was six
    // `new DotState()` in a row. Adding a seventh dot compiled cleanly, and the first
    // poll threw IndexOutOfRange from SetDot(6, ...) -- which took the whole window down
    // at startup, so the cockpit did not appear at all and the exit code was the only
    // evidence. A length kept in step by hand is one that will eventually not be.
    readonly DotState[] _health = NewDotStates();

    public CockpitWindow(string path)
    {
        _statusPath = ResolvePath(path);
        string dir = Path.GetDirectoryName(_statusPath);
        _fleetDir = dir;
        _historyPath = Path.Combine(dir, "history.json");
        _openPath = Path.Combine(dir, "open.json");
        _convsPath = Path.Combine(dir, "conversations.json");
        _hiddenPath = Path.Combine(dir, "cockpit_hidden.json");
        _resumeDismissPath = Path.Combine(dir, "cockpit_resume_dismissed.json");
        _clearedLogPath = Path.Combine(dir, "history_cleared.jsonl");
        LoadGlyphs();
        LoadHistory();
        LoadHidden();
        LoadSettings();
        ApplyThemeBrushes();
        Title = "Fleet Cockpit";   // also lets the taskbar / alt-tab / automation name this window
        // Clamp to the work area: a fixed 1080x760 overflows a shorter desktop (and the
        // UI scale multiplies it), pushing the footer off-screen. SystemParameters are
        // in device-independent units, the same space Width/Height use.
        Width = Math.Min(1080, Math.Max(720, SystemParameters.WorkArea.Width - 80));
        Height = Math.Min(760, Math.Max(520, SystemParameters.WorkArea.Height - 80));
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        BuildChrome();
        // ── UI-scale shortcuts (window level) ────────────────────────────────────
        // Ctrl+Plus/Minus/0 and Ctrl+MouseWheel zoom the whole window. Registered at the
        // window so they fire from anywhere; the composer's own PreviewKeyDown only handles
        // Return/arrow/Tab/Esc, so these never collide with typing (a digit/OemPlus with Ctrl
        // held is not text input). We only act when Ctrl is down and swallow the event then.
        this.PreviewKeyDown += OnScaleKeyDown;
        this.PreviewMouseWheel += OnScaleMouseWheel;
        _timer = new DispatcherTimer();
        _timer.Interval = TimeSpan.FromMilliseconds(700);
        _timer.Tick += new EventHandler(OnTick);
        _timer.Start();
        OnTick(null, null);
    }

    // Stop the background health poll on close (the thread is IsBackground so it would die with the
    // app anyway, but this makes shutdown deterministic and stops the ~15s sleep slices promptly).
    protected override void OnClosed(EventArgs e)
    {
        _healthStop = true;
        try { _healthWake.Set(); } catch (Exception) { }
        base.OnClosed(e);
    }
}
