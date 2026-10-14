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
    int _maxtabs = 3;
    bool _autoscale = false;   // RAM-aware autoscale on/off (NEW)
    int _autoMax = 100;        // ceiling (上限) tabs may grow to under autoscale. High by design:
                               // autoscale (ram_target_cap) self-limits to what free RAM allows
                               // (~3 on a 16 GB box, far more on a big-RAM machine), so this is the
                               // upper RAIL, not a hardware bound. Real limiters: free RAM + M365
                               // Copilot per-user fair-use/rate limits.
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

    // A2-2: Refresh the Evidence Spine panel on each tick. Keyed off a signature so the
    // heavy DOM rebuild (and col-width change) only happens when the run state changes.
    // Called from OnTick after RenderCards (so _toolbarAll is already up to date).
    void RefreshSpine(Dictionary<string, object> root, bool idle)
    {
        if (_spinePanel == null || _spineCol == null) return;

        // NOT-RUNNING: the left "実行タイムライン" follows an EXPANDED past/history task, so inspecting
        // a finished task shows ITS timeline on the left (works after a run ends too, not only when
        // fully idle). A RUNNING fleet always owns the spine (the live run is never hidden), so this
        // past-task focus is suppressed while running. (user choice: 走行中は現在優先)
        bool spineRunning = root != null && (!root.ContainsKey("running") || Convert.ToBoolean(root["running"]));
        if (!spineRunning && root != null && _history != null && _history.Count > 0)
        {
            // WHICH TASK THE TIMELINE IS ABOUT. Expanded wins; with nothing expanded it follows
            // the row at the TOP of the list, which is the one the eye is on. Before this, an
            // un-expanded list left the panel on the live-run spine, so every finished task
            // appeared to share the newest run's clock -- the operator saw one timeline and
            // reasonably read it as belonging to whatever they were looking at.
            Dictionary<string, object> focusEntry = null;
            if (_expanded != null && _expanded.Count > 0)
            {
                for (int hi = _history.Count - 1; hi >= 0; hi--)
                {
                    var he = _history[hi] as Dictionary<string, object>;
                    if (he == null) continue;
                    string hk = S(he, "key");
                    if (!string.IsNullOrEmpty(hk) && _expanded.Contains(hk)) { focusEntry = he; break; }
                }
            }
            if (focusEntry == null)
            {
                // The newest entry is the one rendered first. Same ordering the list uses, so
                // the panel and the top row cannot disagree about which task they mean.
                for (int hi = _history.Count - 1; hi >= 0; hi--)
                {
                    var he = _history[hi] as Dictionary<string, object>;
                    if (he != null) { focusEntry = he; break; }
                }
            }
            if (focusEntry != null)
            {
                string fkey = S(focusEntry, "key");
                string fsig = "PAST|" + fkey + "|" + (_dark ? "D" : "L") + _lang;
                if (fsig == _spineSig) return;
                _spineSig = fsig;
                _spineCol.Width = new GridLength(220);
                _spinePanel.BorderBrush = Theme.Br(Theme.Border(_dark));
                _spinePanel.Background = Theme.Br(Theme.Bg(_dark));
                var pastList = new List<Dictionary<string, object>>();
                pastList.Add(focusEntry);
                bool jaP = _lang == 0;
                var wrap = new StackPanel();
                wrap.Margin = new Thickness(12, 12, 8, 0);
                var pastTag = new TextBlock();
                pastTag.Text = jaP ? "過去のタスク" : "Past task";
                // NOT Accent. Theme.cs reserves that colour for a primary action -- "primary
                // action ONLY" is written beside its definition -- and this is a static eyebrow
                // above a read-only summary. Painting a label in the click-me colour spends the
                // one colour the eye is trained to look for on something that cannot be clicked,
                // and it is the only warm thing on that panel, so it takes the attention first.
                // The emphasis it needs is weight, which it already has.
                pastTag.Foreground = Theme.Br(Theme.Muted(_dark));
                pastTag.FontSize = 10; pastTag.FontWeight = FontWeights.SemiBold;
                pastTag.Margin = new Thickness(0, 0, 0, 2);
                wrap.Children.Add(pastTag);
                var titleTag = new TextBlock();
                string focusSummary = S(focusEntry, "goal_summary");
                titleTag.Text = !string.IsNullOrEmpty(focusSummary)
                    ? focusSummary : CardTitle(S(focusEntry, "conv_title"), S(focusEntry, "goal"));
                titleTag.Foreground = Theme.Br(Theme.Muted(_dark)); titleTag.FontSize = 10;
                titleTag.TextTrimming = TextTrimming.CharacterEllipsis;
                titleTag.Margin = new Thickness(0, 0, 0, 2);
                wrap.Children.Add(titleTag);
                wrap.Children.Add(BuildSpineContent(root, "ended", true, pastList));
                // A past task is finished, so it opens at the top: nothing new will arrive.
                _spinePanel.Child = SpineScroll(wrap, false);
                return;
            }
        }

        // Ensure _toolbarAll reflects current root (it may be empty on first tick before RenderCards).
        // If _toolbarAll is empty but root has workers, use root's workers directly for the check.
        bool hasWorkers = false;
        if (!idle)
        {
            if (_toolbarAll != null && _toolbarAll.Count > 0)
                hasWorkers = true;
            else if (root != null)
            {
                object wo2x;
                if (root.TryGetValue("workers", out wo2x) && wo2x is object[])
                    hasWorkers = ((object[])wo2x).Length > 0;
            }
        }

        // Build a lightweight signature: started + overall phase + worker count + theme/lang.
        string overallPhase = "idle";
        bool runEnded = false;
        // Collect workers from _toolbarAll or directly from root as fallback.
        var spineWorkers = new List<Dictionary<string, object>>();
        if (_toolbarAll != null && _toolbarAll.Count > 0)
            spineWorkers = _toolbarAll;
        else if (root != null)
        {
            object wox2;
            if (root.TryGetValue("workers", out wox2) && wox2 is object[])
                foreach (object ow2 in (object[])wox2)
                {
                    var ww2 = ow2 as Dictionary<string, object>;
                    if (ww2 != null) spineWorkers.Add(ww2);
                }
        }
        if (root != null && hasWorkers)
        {
            bool running = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
            runEnded = !running;
            bool anyAttn = false;
            bool allDone = true;
            foreach (Dictionary<string, object> tw in spineWorkers)
            {
                string st = S(tw, "status");
                if (!IsTerminalWorker(tw)) { allDone = false; }
                if (st == "stuck" || st == "maxturns" || st == "error") anyAttn = true;
                if (st == "verifying") overallPhase = "verifying";
                else if ((st == "researching" || st == "refuting" || st == "waiting" || st == "ready")
                         && overallPhase == "idle") overallPhase = "running";
            }
            if (anyAttn) overallPhase = "attn";
            if (runEnded || allDone) overallPhase = "ended";
        }
        string started = root != null ? S(root, "started") : "";
        // Re-key on the selected task's OPERATOR-FACING CONTENT. The old spine was a timeline,
        // so phase-event count was enough. Content details must repaint when progress/current/next/
        // waiting/artifacts change even if status and phase-event count do not.
        string primaryDetailSig = "";
        if (spineWorkers != null && spineWorkers.Count > 0)
        {
            Dictionary<string, object> primaryW = SpineFocusWorker(spineWorkers);
            if (primaryW != null) primaryDetailSig = SpineDetailSignature(primaryW);
        }
        string spineSig = (hasWorkers ? "1" : "0") + "|" + started + "|" + overallPhase
                          + "|" + (_toolbarAll != null ? _toolbarAll.Count : 0)
                          + "|" + (_dark ? "D" : "L") + _lang
                          + "|" + primaryDetailSig;
        if (spineSig == _spineSig) return;
        _spineSig = spineSig;

        if (!hasWorkers)
        {
            _spineCol.Width = new GridLength(0);
            _spinePanel.Child = null;
            return;
        }

        _spineCol.Width = new GridLength(220);
        _spinePanel.BorderBrush = Theme.Br(Theme.Border(_dark));
        _spinePanel.Background = Theme.Br(Theme.Bg(_dark));
        _spinePanel.Child = SpineScroll(
            BuildSpineContent(root, overallPhase, runEnded, spineWorkers), !runEnded);
    }

    // THE TIMELINE FOLLOWED workers[0] AND NOTHING ELSE. Open a running worker while the first
    // one has finished and the left panel keeps showing the finished one's phases -- reported
    // as "the timeline for the thing I am looking at is not shown", and that is exactly what it
    // did: five goals, w0 done at 17:50, and expanding w1 (still running) left the spine ending
    // at 完了 17:50.
    //
    // Expanding a card is how the operator says which one they are inspecting; the not-running
    // branch already honours it for past tasks. This makes the live branch honour it too, and
    // falls back to the first worker when nothing is expanded.
    Dictionary<string, object> SpineFocusWorker(List<Dictionary<string, object>> workers)
    {
        if (workers == null || workers.Count == 0) return null;
        if (_expanded != null && _expanded.Count > 0)
            foreach (var w in workers)
            {
                if (w == null) continue;
                string nm = S(w, "name");
                if (!string.IsNullOrEmpty(nm) && _expanded.Contains(nm)) return w;
            }
        return workers[0];
    }

    string SpineDetailSignature(Dictionary<string, object> w)
    {
        if (w == null) return "";
        var parts = new List<string>();
        parts.Add(S(w, "name"));
        parts.Add(S(w, "status"));
        parts.Add(I(w, "turn").ToString());
        parts.Add(I(w, "max_turns").ToString());
        parts.Add(S(w, "goal_summary"));
        parts.Add(S(w, "reason"));
        parts.Add(S(w, "outcome"));
        parts.Add(I(w, "verify_attempts").ToString());
        object phaseRaw;
        if (w.TryGetValue("phase_events", out phaseRaw) && phaseRaw is object[])
        {
            object[] phaseArr = (object[])phaseRaw;
            parts.Add(phaseArr.Length.ToString());
            if (phaseArr.Length > 0)
            {
                var lastPhase = phaseArr[phaseArr.Length - 1] as Dictionary<string, object>;
                if (lastPhase != null)
                {
                    parts.Add(S(lastPhase, "event"));
                    parts.Add(S(lastPhase, "label"));
                    parts.Add(S(lastPhase, "ts"));
                }
            }
        }
        var execution = Obj(w, "execution");
        if (execution != null)
        {
            parts.Add(S(execution, "state"));
            parts.Add(S(execution, "current_step"));
            parts.Add(S(execution, "last_progress"));
            parts.Add(S(execution, "next_step"));
            parts.Add(S(execution, "waiting_reason"));
            parts.Add(I(execution, "completed_count").ToString());
            parts.Add(I(execution, "total_steps").ToString());
            object artsRaw;
            if (execution.TryGetValue("artifacts", out artsRaw) && artsRaw is object[])
            {
                foreach (object obj in (object[])artsRaw)
                {
                    var artifact = obj as Dictionary<string, object>;
                    if (artifact == null) continue;
                    parts.Add(S(artifact, "path"));
                    parts.Add(S(artifact, "name"));
                    parts.Add(S(artifact, "uri"));
                }
            }
        }
        return string.Join("|", parts.ToArray());
    }

    // Left task-inspection spine. Content details answer "what is it doing now?"; the timeline
    // directly below answers "how did it get here?". Both follow the same selected worker, and
    // the expanded card keeps the richer evidence view rather than being the only timeline.
    // GIVE THE SPINE A VIEWPORT. The column is a fixed 220px lane whose content has NO upper
    // bound: the timeline is one entry per phase transition, and the Border it sat in simply
    // CLIPPED everything past the fold. The entries were rendered and unreachable, with no
    // scrollbar to say so -- the failure looked like "the timeline stops at 12:17" rather than
    // like a missing viewport.
    //
    // WHY IT SURVIVED THIS LONG: benchmark runs are short and uniform, so the timeline never
    // outgrew the lane. General use does, and it is general use that this is for. A fixed-size
    // container sized against the shortest input is a bug waiting for a real workload.
    //
    // LIVE RUNS RIDE THE BOTTOM. While a run is going the newest entry is the one worth seeing,
    // and this panel is rebuilt whenever the phase signature changes -- so a top-anchored
    // rebuild would hide the very event that triggered it. Keyed off ExtentHeightChange, which
    // is non-zero only when CONTENT grows, so it never fights a user who has scrolled up.
    // A finished run opens at the top, where its story starts.
    ScrollViewer SpineScroll(UIElement content, bool follow)
    {
        var sv = new ScrollViewer();
        sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        sv.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        sv.Focusable = false;
        sv.IsTabStop = false;
        sv.Content = content;
        if (follow)
            sv.ScrollChanged += delegate(object s, ScrollChangedEventArgs e)
            {
                if (e.ExtentHeightChange != 0) sv.ScrollToEnd();
            };
        return sv;
    }

    void AddSpineTimeline(StackPanel outer, Dictionary<string, object> w)
    {
        if (outer == null || w == null) return;
        bool ja = _lang == 0;

        var divider = new Border();
        divider.Height = 1;
        divider.Background = Theme.Br(Theme.Border(_dark));
        divider.Margin = new Thickness(0, 12, 0, 8);
        outer.Children.Add(divider);

        var title = new TextBlock();
        title.Text = ja ? "実行タイムライン" : "Execution timeline";
        title.Foreground = Theme.Br(Theme.Muted(_dark));
        title.FontSize = 10.5;
        title.FontWeight = FontWeights.SemiBold;
        title.Margin = new Thickness(0, 0, 0, 2);
        outer.Children.Add(title);

        bool real = false;
        object peRaw;
        if (w.TryGetValue("phase_events", out peRaw) && peRaw is object[])
            real = ((object[])peRaw).Length > 0;
        outer.Children.Add(new TextBlock {
            Text = real ? (ja ? "(フェーズ遷移)" : "(phase transitions)")
                        : (ja ? "(ターン記録から推定)" : "(estimated from turns)"),
            Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 9.5,
            Margin = new Thickness(0, 0, 0, 7) });

        // The phase name alone is not enough operationally. Surface what this selected worker
        // is doing NOW, but only from fields the runner actually published -- no invented step.
        string timelineNow = "";
        var timelineExec = Obj(w, "execution");
        if (timelineExec != null)
        {
            string current = S(timelineExec, "current_step");
            string progress = S(timelineExec, "last_progress");
            if (!string.IsNullOrEmpty(current)) timelineNow = current;
            if (!string.IsNullOrEmpty(progress) && progress != current)
                timelineNow = string.IsNullOrEmpty(timelineNow) ? progress : (timelineNow + " · " + progress);
        }
        if (string.IsNullOrEmpty(timelineNow)) timelineNow = S(w, "reason");
        timelineNow = (timelineNow ?? "").Replace("\r", " ").Replace("\n", " ").Trim();
        if (timelineNow.Length > 180) timelineNow = timelineNow.Substring(0, 179).TrimEnd() + "…";
        if (!string.IsNullOrEmpty(timelineNow))
            outer.Children.Add(new TextBlock {
                Text = (ja ? "現在: " : "Now: ") + timelineNow,
                Foreground = Muted, FontSize = 10.5, TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 0, 0, 8) });

        string status = S(w, "status");
        string outcome = S(w, "outcome");
        bool terminal = status == "done" || status == "stuck" || status == "maxturns"
                     || status == "error" || status == "cancelled";
        int reviews = I(w, "verify_attempts");
        var events = BuildTimelineEvents(S(w, "transcript"), outcome, terminal, reviews, w);
        for (int i = 0; i < events.Count; i++)
        {
            bool last = i == events.Count - 1;
            string label = events[i].Item1;
            string color = events[i].Item2;

            var row = new Grid();
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(18) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var rail = new DockPanel();
            rail.HorizontalAlignment = HorizontalAlignment.Center;
            var head = new Border { Width = 1.5, Height = 7,
                Background = i > 0 ? Theme.Br(Theme.Border(_dark)) : Brushes.Transparent,
                HorizontalAlignment = HorizontalAlignment.Center };
            DockPanel.SetDock(head, Dock.Top);
            rail.Children.Add(head);
            var dot = new System.Windows.Shapes.Ellipse { Width = 8, Height = 8,
                Fill = Theme.Br(color), HorizontalAlignment = HorizontalAlignment.Center };
            DockPanel.SetDock(dot, Dock.Top);
            rail.Children.Add(dot);
            rail.Children.Add(new Border { Width = 1.5,
                Background = last ? Brushes.Transparent : Theme.Br(Theme.Border(_dark)),
                HorizontalAlignment = HorizontalAlignment.Center });
            Grid.SetColumn(rail, 0);
            row.Children.Add(rail);

            var tb = new TextBlock { Text = label, Foreground = Theme.Br(color), FontSize = 11.0,
                FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(4, i == 0 ? 1 : 0, 0, 5) };
            Grid.SetColumn(tb, 1);
            row.Children.Add(tb);
            outer.Children.Add(row);
        }
    }

    // Derives events honestly from available data: run started ts, transcript first-turn ts,
    // current overall phase (polled), run ended state. No fabricated phase_events.
    UIElement BuildSpineContent(Dictionary<string, object> root, string overallPhase, bool runEnded,
                                List<Dictionary<string, object>> workers)
    {
        bool ja = _lang == 0;
        var outer = new StackPanel();
        outer.Margin = new Thickness(12, 12, 8, 12);

        var sectionLbl = new TextBlock();
        sectionLbl.Text = ja ? "内容詳細" : "Content details";
        sectionLbl.Foreground = Theme.Br(Theme.Muted(_dark));
        sectionLbl.FontSize = 10.5;
        sectionLbl.FontWeight = FontWeights.SemiBold;
        sectionLbl.Margin = new Thickness(0, 0, 0, 7);
        outer.Children.Add(sectionLbl);

        Dictionary<string, object> primaryWorker = SpineFocusWorker(workers);
        if (primaryWorker == null)
        {
            outer.Children.Add(new TextBlock {
                Text = ja ? "表示できるタスク情報がありません" : "No task details available",
                Foreground = Theme.Br(Theme.Muted(_dark)), FontSize = 12.0,
                TextWrapping = TextWrapping.Wrap });
            return outer;
        }

        string fullGoal = S(primaryWorker, "goal");
        string goalSummary = S(primaryWorker, "goal_summary");
        if (string.IsNullOrEmpty(goalSummary))
            goalSummary = CardTitle(S(primaryWorker, "conv_title"), fullGoal);
        if (!string.IsNullOrEmpty(goalSummary))
            outer.Children.Add(new TextBlock {
                Text = goalSummary, Foreground = Fg, FontSize = 13.0,
                FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 0, 0, 7) });

        string status = S(primaryWorker, "status");
        int turn = I(primaryWorker, "turn");
        int maxTurns = I(primaryWorker, "max_turns");
        string statusText = StatusLabel(status);
        if (string.IsNullOrEmpty(statusText)) statusText = overallPhase;
        if (turn > 0)
            statusText += maxTurns > 0 ? ("  ·  Turn " + turn + "/" + maxTurns) : ("  ·  Turn " + turn);
        if (!string.IsNullOrEmpty(statusText))
            outer.Children.Add(new TextBlock {
                Text = statusText, Foreground = Theme.Br(Theme.Secondary(_dark)), FontSize = 11.5,
                TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 7) });

        var execution = Obj(primaryWorker, "execution");
        string waiting = "";
        if (execution != null)
        {
            string state = S(execution, "state");
            string current = S(execution, "current_step");
            string progress = S(execution, "last_progress");
            string next = S(execution, "next_step");
            waiting = S(execution, "waiting_reason");

            if (!string.IsNullOrEmpty(current))
            {
                outer.Children.Add(new TextBlock { Text = ja ? "現在" : "Current",
                    Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 10.5,
                    FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 2, 0, 1) });
                string currentText = current;
                if (!string.IsNullOrEmpty(state) && state != current) currentText = state + "  ·  " + current;
                outer.Children.Add(new TextBlock { Text = currentText, Foreground = Fg, FontSize = 12.0,
                    TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 5) });
            }
            else if (!string.IsNullOrEmpty(state))
            {
                outer.Children.Add(new TextBlock { Text = (ja ? "状態: " : "State: ") + state,
                    Foreground = Fg, FontSize = 12.0, TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 2, 0, 5) });
            }

            if (!string.IsNullOrEmpty(progress))
                outer.Children.Add(new TextBlock { Text = (ja ? "進捗: " : "Progress: ") + progress,
                    Foreground = Muted, FontSize = 11.5, TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 1, 0, 4) });
            if (!string.IsNullOrEmpty(next))
                outer.Children.Add(new TextBlock { Text = (ja ? "次: " : "Next: ") + next,
                    Foreground = Muted, FontSize = 11.5, TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 1, 0, 4) });

            object artsRaw;
            if (execution.TryGetValue("artifacts", out artsRaw) && artsRaw is object[])
            {
                object[] arts = (object[])artsRaw;
                if (arts.Length > 0)
                {
                    outer.Children.Add(new TextBlock { Text = ja ? "成果物" : "Artifacts",
                        Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 10.5,
                        FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 5, 0, 1) });
                    int shown = 0;
                    foreach (object obj in arts)
                    {
                        var artifact = obj as Dictionary<string, object>;
                        if (artifact == null) continue;
                        string label = S(artifact, "path");
                        if (string.IsNullOrEmpty(label)) label = S(artifact, "name");
                        if (string.IsNullOrEmpty(label)) label = S(artifact, "uri");
                        if (string.IsNullOrEmpty(label)) continue;
                        outer.Children.Add(new TextBlock { Text = "• " + label,
                            Foreground = Muted, FontSize = 11.0, TextWrapping = TextWrapping.Wrap,
                            Margin = new Thickness(5, 1, 0, 1) });
                        shown++;
                        if (shown >= 5) break;
                    }
                    if (arts.Length > shown)
                        outer.Children.Add(new TextBlock { Text = "+" + (arts.Length - shown),
                            Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 10.5,
                            Margin = new Thickness(5, 1, 0, 2) });
                }
            }
        }

        string reason = !string.IsNullOrEmpty(waiting) ? waiting : S(primaryWorker, "reason");
        if (!string.IsNullOrEmpty(reason))
        {
            outer.Children.Add(new TextBlock { Text = ja ? "待機・補足" : "Waiting / note",
                Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 10.5,
                FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 6, 0, 1) });
            outer.Children.Add(new TextBlock { Text = reason, Foreground = Muted, FontSize = 11.5,
                TextWrapping = TextWrapping.Wrap });
        }

        AddSpineTimeline(outer, primaryWorker);
        return outer;
    }

    // ItemContainerStyle: invisible chrome. The ControlTemplate is just a ContentPresenter, so
    // there is NO selection highlight, NO mouse-over, NO focus rectangle -- it behaves like a
    // plain scrolling row, not a selectable list item.
    Style BuildItemContainerStyle()
    {
        var st = new Style(typeof(ListBoxItem));
        st.Setters.Add(new Setter(Control.BackgroundProperty, Brushes.Transparent));
        st.Setters.Add(new Setter(Control.BorderThicknessProperty, new Thickness(0)));
        st.Setters.Add(new Setter(Control.PaddingProperty, new Thickness(0)));
        st.Setters.Add(new Setter(FrameworkElement.MarginProperty, new Thickness(0)));
        st.Setters.Add(new Setter(Control.HorizontalContentAlignmentProperty, HorizontalAlignment.Stretch));
        st.Setters.Add(new Setter(UIElement.FocusableProperty, false));
        var tmpl = new ControlTemplate(typeof(ListBoxItem));
        var cp = new FrameworkElementFactory(typeof(ContentPresenter));
        cp.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Stretch);
        tmpl.VisualTree = cp;
        st.Setters.Add(new Setter(Control.TemplateProperty, tmpl));
        return st;
    }

    // ItemTemplate: a ContentControl whose Content is the item itself, run through RowConverter,
    // which calls the EXISTING builders by Row.Kind and returns the realized UIElement. Because
    // containers recycle, Convert runs for visible items only.
    DataTemplate BuildRowTemplate()
    {
        var dt = new DataTemplate();
        var cc = new FrameworkElementFactory(typeof(ContentControl));
        var b = new System.Windows.Data.Binding();
        b.Converter = new RowConverter(this);
        cc.SetBinding(ContentControl.ContentProperty, b);
        cc.SetValue(UIElement.FocusableProperty, false);
        dt.VisualTree = cc;
        return dt;
    }

    // Persistent decision entry point.  Unlike the run-mode combo, this button represents actual
    // human decisions and remains useful while the fleet is idle.
    UIElement ApprovalCenterControl()
    {
        _approvalCenterBtn = new Button();
        _approvalCenterBtn.Cursor = Cursors.Hand;
        _approvalCenterBtn.BorderThickness = new Thickness(1);
        _approvalCenterBtn.Padding = new Thickness(12, 3, 8, 3);
        _approvalCenterBtn.Margin = new Thickness(0, 0, 12, 0);
        _approvalCenterBtn.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterBtn.Template = FlatButtonTemplate();

        var row = new StackPanel(); row.Orientation = Orientation.Horizontal;
        row.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterLabel = new TextBlock();
        _approvalCenterLabel.FontSize = 12; _approvalCenterLabel.FontWeight = FontWeights.SemiBold;
        _approvalCenterLabel.VerticalAlignment = VerticalAlignment.Center;
        row.Children.Add(_approvalCenterLabel);

        _approvalCenterBadge = new Border();
        _approvalCenterBadge.CornerRadius = new CornerRadius(Theme.RadCard);
        _approvalCenterBadge.MinWidth = 18; _approvalCenterBadge.Height = 18;
        _approvalCenterBadge.Margin = new Thickness(7, 0, 0, 0);
        _approvalCenterBadge.Padding = new Thickness(4, 0, 4, 0);
        _approvalCenterBadgeText = new TextBlock();
        _approvalCenterBadgeText.FontSize = 10; _approvalCenterBadgeText.FontWeight = FontWeights.Bold;
        _approvalCenterBadgeText.HorizontalAlignment = HorizontalAlignment.Center;
        _approvalCenterBadgeText.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterBadge.Child = _approvalCenterBadgeText;
        row.Children.Add(_approvalCenterBadge);
        _approvalCenterBtn.Content = row;
        _approvalCenterBtn.Click += delegate { CloseHeaderPopups(null); ShowApprovalCenter(); };
        PaintApprovalCenterButton(0);
        return _approvalCenterBtn;
    }

    void PaintApprovalCenterButton(int pending)
    {
        if (_approvalCenterBtn == null) return;
        bool hasPending = pending > 0;
        _approvalCenterBtn.Background = hasPending ? Theme.Br(Theme.SurfaceSubtle(_dark)) : BtnBg;
        _approvalCenterBtn.BorderBrush = hasPending ? Theme.Br(Theme.Warning(_dark)) : Border;
        _approvalCenterBtn.Foreground = Fg;
        if (_approvalCenterLabel != null)
        {
            _approvalCenterLabel.Text = _lang == 0 ? "承認" : "Approvals";
            _approvalCenterLabel.Foreground = hasPending ? Theme.Br(Theme.Warning(_dark)) : Fg;
        }
        if (_approvalCenterBadge != null)
            _approvalCenterBadge.Background = hasPending ? Theme.Br(Theme.Warning(_dark)) : Theme.Br(Theme.SurfaceSubtle(_dark));
        if (_approvalCenterBadgeText != null)
        {
            _approvalCenterBadgeText.Text = pending.ToString();
            _approvalCenterBadgeText.Foreground = hasPending ? White : Muted;
        }
        string accessible = _lang == 0
            ? ("承認センター、未処理 " + pending + " 件")
            : ("Approval Center, " + pending + " pending");
        _approvalCenterBtn.ToolTip = accessible;
        System.Windows.Automation.AutomationProperties.SetName(_approvalCenterBtn, accessible);
    }

    // Fleet-wide controls: Pause/Resume toggle + Stop-all. Both send their own single-key
    // command file through FleetCommands.Write, so neither can clobber a queued close/steer/add.
    // fleet_runner._drain_commands consumes {"pause":bool}/{"stop":true} each sweep.
    UIElement FleetControls()
    {
        var group = new StackPanel(); group.Orientation = Orientation.Horizontal;
        group.VerticalAlignment = VerticalAlignment.Center; group.Margin = new Thickness(0, 0, 12, 0);

        _pauseBtn = new Button();
        _pauseBtn.ToolTip = _lang == 0 ? "新規ターン/タブを止めて凍結（再開で続行・状態は保持）" : "Freeze: no new turns/tabs (resume continues; state kept)";
        System.Windows.Automation.AutomationProperties.SetName(_pauseBtn, _lang == 0 ? "一時停止" : "Pause");
        _pauseBtn.Cursor = Cursors.Hand; _pauseBtn.BorderThickness = new Thickness(1);
        _pauseBtn.Width = 36; _pauseBtn.Height = HeaderCtrlH; _pauseBtn.Padding = new Thickness(0);
        _pauseBtn.Margin = new Thickness(0, 0, 8, 0);
        _pauseBtn.VerticalAlignment = VerticalAlignment.Center;
        // FlatButtonTemplate makes Background=BtnBg actually render (the default Aero template
        // paints its own light gradient, making a light-colored icon invisible in dark mode).
        _pauseBtn.Template = FlatButtonTemplate();
        _pauseIcon = new System.Windows.Shapes.Path { Stretch = Stretch.Uniform };
        {
            var vb = new Viewbox();
            vb.Width = 16; vb.Height = 16;
            vb.Stretch = Stretch.Uniform;
            vb.HorizontalAlignment = HorizontalAlignment.Center;
            vb.VerticalAlignment = VerticalAlignment.Center;
            vb.Child = _pauseIcon;
            _pauseBtn.Content = vb;
        }
        _pauseBtn.Click += delegate
        {
            // Pause only means something to a LIVE fleet. With no live consumer the command would
            // sit unread in commands.d/ while the label lied "Resume" -- so do nothing then (the
            // button is also disabled per-tick by RefreshPauseEnabled, this is belt-and-suspenders).
            if (!RunIsLive()) { if (_paused) { _paused = false; PaintPause(); } return; }
            _paused = !_paused;
            SendCommand(Cmd1("pause", _paused));
            PaintPause();
        };
        group.Children.Add(_pauseBtn);

        _stopBtn = new Button();
        _stopBtn.ToolTip = _lang == 0 ? "全ワーカーを停止して走行を終了" : "Cancel every worker and end the run";
        System.Windows.Automation.AutomationProperties.SetName(_stopBtn, _lang == 0 ? "停止" : "Stop");
        _stopBtn.Cursor = Cursors.Hand; _stopBtn.BorderThickness = new Thickness(1);
        _stopBtn.Width = 36; _stopBtn.Height = HeaderCtrlH; _stopBtn.Padding = new Thickness(0);
        _stopBtn.VerticalAlignment = VerticalAlignment.Center;
        // Same fix: FlatButtonTemplate so Background is honoured and the icon fill contrasts correctly.
        _stopBtn.Template = FlatButtonTemplate();
        _stopIcon = new System.Windows.Shapes.Path { Stretch = Stretch.Uniform,
            Data = Geometry.Parse("M3,3 H13 V13 H3 Z") };   // a stop square
        {
            var vb2 = new Viewbox();
            vb2.Width = 16; vb2.Height = 16;
            vb2.Stretch = Stretch.Uniform;
            vb2.HorizontalAlignment = HorizontalAlignment.Center;
            vb2.VerticalAlignment = VerticalAlignment.Center;
            vb2.Child = _stopIcon;
            _stopBtn.Content = vb2;
        }
        _stopBtn.Click += delegate
        {
            SendCommand(Cmd1("stop", true));
            // FIX B: immediate optimistic feedback -- don't wait for the ~700ms sweep to show
            // anything changed. Flip the button into its "stopping" state and dim every
            // non-terminal card NOW; RefreshStoppingState (called each OnTick) clears this the
            // moment the sweep confirms the run actually stopped (or a different run starts).
            _stopping = true;
            _stopStartedMarker = _lastRoot != null ? S(_lastRoot, "started") : "";
            PaintStop();
            ForceRender();
            // No live fleet to consume the stop (status went stale, e.g. a run was killed and its
            // last status froze with workers still shown as "running") -> clear every card NOW so the
            // button is never a no-op. Mirrors the per-card release stale path (search ArchiveAndHide).
            if (_lastRoot != null
                && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
            {
                ArchiveAllStale();
                // ArchiveAllStale only edits the LOCAL _history/_hiddenKeys lists (history.json /
                // cockpit_hidden.json) -- it never touches status.json or fleet_run_active.json, so
                // a fresh cockpit launch (or any other reader) would still see the phantom
                // running=true. Finalize the actual sidecars too. See relay/fleet_reaper.py.
                ReapStaleFleetRun();
            }
        };
        group.Children.Add(_stopBtn);

        PaintPause();
        PaintStop();
        return group;
    }
    // Icon buttons (spec): pause shows two bars; while paused it shows a play triangle (resume).
    // Quiet neutral chrome — no orange fill. The intent is carried by the icon + tooltip, not color.
    void PaintPause()
    {
        if (_pauseBtn == null) return;
        if (_pauseIcon != null)
        {
            _pauseIcon.Data = Geometry.Parse(_paused ? "M4,2 L13,8 L4,14 Z"          // play (resume)
                                                      : "M3,2 H6 V14 H3 Z M10,2 H13 V14 H10 Z"); // pause bars
            _pauseIcon.Fill = _paused ? Theme.Br(Theme.Accent(_dark)) : Fg;
        }
        _pauseBtn.ToolTip = _paused
            ? (_lang == 0 ? "再開（凍結を解除して続行）" : "Resume (unfreeze and continue)")
            : (_lang == 0 ? "新規ターン/タブを止めて凍結（再開で続行・状態は保持）" : "Freeze: no new turns/tabs (resume continues; state kept)");
        _pauseBtn.Background = BtnBg; _pauseBtn.Foreground = Fg; _pauseBtn.BorderBrush = Border;
        if (_stopIcon != null && !_stopping) _stopIcon.Fill = Fg;
    }
    // FIX B: paints the Stop button's optimistic "stopping" state (accent-colored icon + tooltip/
    // automation-name swap), mirroring PaintPause's icon-swap pattern for Pause/Resume. Called from
    // the same spots PaintPause is (theme/lang refresh + the click handler) so it never goes stale.
    void PaintStop()
    {
        if (_stopBtn == null) return;
        if (_stopIcon != null) _stopIcon.Fill = _stopping ? Theme.Br(Theme.Accent(_dark)) : Fg;
        _stopBtn.ToolTip = _stopping
            ? T("stop_stopping")
            : (_lang == 0 ? "全ワーカーを停止して走行を終了" : "Cancel every worker and end the run");
        System.Windows.Automation.AutomationProperties.SetName(_stopBtn,
            _stopping ? T("stop_stopping") : (_lang == 0 ? "停止" : "Stop"));
    }
    // Per-tick: Pause is enabled only when a run is LIVE (something to pause). When no run is live we
    // also drop a stale "paused" state so the label can never sit on "Resume" over a dead/absent run.
    void RefreshPauseEnabled(Dictionary<string, object> root)
    {
        bool live = Liveness(root) == 1;
        if (_pauseBtn != null)
        {
            _pauseBtn.IsEnabled = live;
            _pauseBtn.Opacity = live ? 1.0 : 0.5;
            if (!live && _paused) { _paused = false; PaintPause(); }
        }
        // Codex P2 ④: Stop is a strong action; when no run is live it should read as inert, not
        // "still need to stop something?". Disable + dim it (re-enabled the moment a run goes live).
        if (_stopBtn != null)
        {
            _stopBtn.IsEnabled = live;
            _stopBtn.Opacity = live ? 1.0 : 0.5;
            _stopBtn.Cursor = live ? Cursors.Hand : Cursors.Arrow;
        }
    }

    // FIX B: resolve the optimistic "stopping" state set by the Stop click handler. Cleared (and a
    // re-render forced via the Sig() dependency below) once the sweep confirms the run is no longer
    // LIVE -- i.e. actually stopped -- OR a different run has since started (a new `started` marker),
    // so the dimmed cards/button never stay stuck once the real signal arrives.
    void RefreshStoppingState(Dictionary<string, object> root)
    {
        if (!_stopping) return;
        bool live = Liveness(root) == 1;
        string startedNow = root != null ? S(root, "started") : "";
        bool newRun = !string.IsNullOrEmpty(_stopStartedMarker) && !string.IsNullOrEmpty(startedNow)
                      && startedNow != _stopStartedMarker;
        if (!live || newRun)
        {
            _stopping = false;
            PaintStop();
        }
    }

    // MaxTabsStepper() removed: the 開始(デフォルト) stepper now lives only in the settings panel
    // (BuildSettingsPanel builds it inline and assigns _maxMinus/_maxPlus/_maxValue).
    TextBlock _maxValue;

    // Autoscale toggle chip -- the SAME success/neutral treatment as PaintAutoRetryBtn and
    // PaintAutoArchiveBtn, which is the settings panel's rule for a status toggle.
    //
    // This one used the accent (orange) while the other two ONs beside it were green, so three
    // chips that mean the same thing -- this option is on -- read as three different kinds of
    // thing, and the odd one read as a warning. Accent fill is reserved for the primary Start
    // action; PaintAutoRetryBtn already said so and this chip was the exception to it.
    void PaintAutoToggle()
    {
        if (_autoToggle == null) return;
        _autoToggle.Content = T("autoscale") + ": " + (_autoscale ? "ON" : "OFF");
        if (_autoscale)
        {
            _autoToggle.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            _autoToggle.Foreground = Theme.Br(Theme.Success(_dark));
            _autoToggle.BorderBrush = Theme.Br(Theme.Success(_dark));
        }
        else
        {
            _autoToggle.Background = BtnBg;
            _autoToggle.Foreground = Muted;
            _autoToggle.BorderBrush = Border;
        }
    }

    // Ceiling stepper only matters under autoscale: grey/disable it when autoscale is off.
    void UpdateAutoEnabled()
    {
        bool on = _autoscale;
        if (_autoMinus != null) _autoMinus.IsEnabled = on;
        if (_autoPlus != null) _autoPlus.IsEnabled = on;
        double op = on ? 1.0 : 0.45;
        if (_autoMinus != null) _autoMinus.Opacity = op;
        if (_autoPlus != null) _autoPlus.Opacity = op;
        if (_autoValue != null) _autoValue.Opacity = op;
        if (_autoLbl != null) _autoLbl.Opacity = op;
    }

    void SetAutoMax(int v)
    {
        _autoMax = Math.Max(1, Math.Min(100, v));
        SaveKey("autoscale_max", _autoMax.ToString());
        if (_autoValue != null) _autoValue.Text = _autoMax.ToString();
        // live-apply only while autoscale is on (ceiling is meaningless otherwise).
        if (_autoscale) RequestSetAutoscaleIfLive();
    }

    // status.json freshness thresholds, mirrored from fleet_runner so the cockpit agrees with the
    // fleet's OWN watchdog on what "the driver is gone" means. A LIVE fleet legitimately freezes
    // status.json while a worker is blocked in a bounded acceptance eval -- up to EVAL_STALL_CEILING_S
    // when a worker is "verifying", otherwise up to the plain stall window. Treating that as dead would
    // be the WORSE bug (double-launch on Retry, dropped Steer), so we only call a run dead past these.
    const double DEAD_STALL_SECS = 150.0;        // == fleet_runner --stall-s default
    const double EVAL_STALL_CEILING_S = 1500.0;  // == relay_fleet.EVAL_STALL_CEILING_S

    // 0 = idle / no run, 1 = LIVE (fresh, or legitimately frozen inside a bounded eval),
    // 2 = DEAD (status claims running but has frozen past the stall window with no eval in flight,
    //     i.e. the fleet_runner process is gone). Reads status.json ONCE. Pure given the file.
    int Liveness(Dictionary<string, object> st)
    {
        if (st == null) return 0;
        bool running = !st.ContainsKey("running") || Convert.ToBoolean(st["running"]);
        bool idle = st.ContainsKey("idle") && Convert.ToBoolean(st["idle"]);
        if (!running || idle) return 0;
        double age = NowUnix() - Dbl(st, "updated");
        if (age <= DEAD_STALL_SECS) return 1;             // fresh enough -> live
        // frozen past the stall window: still LIVE iff a worker is in a bounded eval (mirrors
        // fleet_runner._watchdog_should_reset), bounded by the eval ceiling as the failsafe.
        bool evalInFlight = false;
        if (st.ContainsKey("workers") && st["workers"] is object[])
            foreach (object o in (object[])st["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                if (NowUnix() < Dbl(w, "eval_busy_until")) { evalInFlight = true; break; }
                if (S(w, "status") == "verifying") { evalInFlight = true; break; }
            }
        if (evalInFlight) return age > EVAL_STALL_CEILING_S ? 2 : 1;
        return 2;                                          // frozen, nothing in eval -> driver gone
    }

    // true iff a run is currently LIVE: running, not idle, AND its status is fresh enough that the
    // fleet_runner is provably still consuming commands.d/ (so steer/retry/pause actually land).
    bool RunIsLive()
    {
        try { return Liveness(ReadStatus()) == 1; }
        catch (Exception) { return false; }
    }
    // true iff a run claims to be running but its driver has died (frozen status). Used to switch
    // controls to their honest local fallback instead of writing commands nobody will ever read.
    bool RunIsDead()
    {
        try { return Liveness(ReadStatus()) == 2; }
        catch (Exception) { return false; }
    }
    void RequestSetAutoscaleIfLive()
    {
        if (RunIsLive()) RequestSetAutoscale(true, Math.Min(_maxtabs, _autoMax), _autoMax);
    }
    void RequestSetMaxtabsIfLive()
    {
        if (RunIsLive()) RequestSetMaxtabs(_maxtabs);
    }
    void SetMaxTabs(int v)
    {
        _maxtabs = Math.Max(1, Math.Min(100, v));
        SaveKey("maxtabs", _maxtabs.ToString());
        if (_maxValue != null) _maxValue.Text = _maxtabs.ToString();
        // FIX D: the same stepper gesture must behave the same whether autoscale is ON or OFF.
        // RequestSetMaxtabs writes a command file via the SAME live-apply mechanism
        // RequestSetAutoscale (and the old "Apply now" banner button) already used -- there is no
        // technical reason the non-autoscale path needs a separate negotiation banner, so both
        // paths now apply immediately and show the identical lightweight auto-dismissing toast
        // (ShowScaleToast -- the same self-fading overlay already used for UI-zoom feedback).
        bool running = RunIsLive();
        if (running)
        {
            if (_autoscale) RequestSetAutoscale(true, Math.Min(_maxtabs, _autoMax), _autoMax);
            else RequestSetMaxtabs(_maxtabs);
            if (_mtBanner != null) _mtBanner.Visibility = Visibility.Collapsed;
            ShowScaleToast(T("maxtabs_toast_prefix") + " " + _maxtabs);
        }
        else if (_mtBanner != null) _mtBanner.Visibility = Visibility.Collapsed;
    }

    Border _mtBanner;
    TextBlock _mtBannerLbl;

    // B: changing max-tabs during a live run -> apply now or next run.
    UIElement BuildMtBanner()
    {
        _mtBanner = new Border();
        _mtBanner.Visibility = Visibility.Collapsed;
        _mtBanner.CornerRadius = new CornerRadius(Theme.RadPopover);
        _mtBanner.BorderThickness = new Thickness(1);
        _mtBanner.Padding = new Thickness(16, 8, 12, 8);
        _mtBanner.Margin = new Thickness(24, 0, 16, 6);
        DockPanel.SetDock(_mtBanner, Dock.Top);
        var dp = new DockPanel();
        var btns = new StackPanel(); btns.Orientation = Orientation.Horizontal;
        btns.HorizontalAlignment = HorizontalAlignment.Right;
        DockPanel.SetDock(btns, Dock.Right);
        _mtApplyNow = new Button();
        _mtApplyNow.Cursor = Cursors.Hand; _mtApplyNow.BorderThickness = new Thickness(0);
        _mtApplyNow.Padding = new Thickness(12, 4, 12, 4); _mtApplyNow.FontWeight = FontWeights.SemiBold;
        _mtApplyNow.Click += delegate
        {
            if (_autoscale) RequestSetAutoscale(true, Math.Min(_maxtabs, _autoMax), _autoMax);
            else RequestSetMaxtabs(_maxtabs);
            _mtBanner.Visibility = Visibility.Collapsed;
        };
        btns.Children.Add(_mtApplyNow);
        _mtLater = new Button();
        _mtLater.Cursor = Cursors.Hand; _mtLater.BorderThickness = new Thickness(1);
        _mtLater.Padding = new Thickness(12, 4, 12, 4); _mtLater.Margin = new Thickness(8, 0, 0, 0);
        _mtLater.Click += delegate { _mtBanner.Visibility = Visibility.Collapsed; };
        btns.Children.Add(_mtLater);
        dp.Children.Add(btns);
        _mtBannerLbl = new TextBlock();
        _mtBannerLbl.VerticalAlignment = VerticalAlignment.Center; _mtBannerLbl.FontSize = 13;
        dp.Children.Add(_mtBannerLbl);
        _mtBanner.Child = dp;
        return _mtBanner;
    }
    Button _mtApplyNow, _mtLater;

    // ── TASK 1+2: capacity-wait banner + 強制開始 (force-start) ──────────────────────
    // The runner's admission gate holds new workers in status=="pending"/pill=="待機列" whenever
    // C: free drops below the reserved disk floor (or free RAM is too tight) -- but nothing told the
    // user WHY a task just sits queued. This banner surfaces that condition reactively each OnTick
    // and offers 強制開始, which disables the disk gate live via {"set_disk_floor_gb":0.0}
    // (consumed by fleet_runner._drain_commands, fleet_runner.py ~L561-563). It's reversible: we
    // capture the floor we zeroed and offer 床を戻す to write it back.
    Border _capBanner;
    TextBlock _capBannerLbl;
    Button _capForceBtn, _capRestoreBtn;
    bool _diskFloorForced = false;      // true once the user hit 強制開始 (gate disabled live)
    double _diskFloorPrev = 0.0;        // the floor we zeroed, so 床を戻す can restore it

    // ── TASK 2 (Bucket C): pending-gate approval banner ──────────────────────────────────
    // When status.json has a non-empty `pending_gates` array, a worker is blocked waiting for
    // human approval. This banner surfaces the gate prominently so it cannot be missed.
    Border _gateBanner;
    ScrollViewer _gateScroll;           // bounds the banner so it cannot starve the rest of the window
    StackPanel _gateCardsPanel;         // holds one card per pending gate (or first gate + count)
    string _gateSig = "";               // last rendered gate set (by token); rebuild only on change
    // COLLAPSED BY DEFAULT. Expanded, three Skill cards -- each carrying a question, a path, a
    // digest and an Approve/Deny row -- measured taller than the run list they sit above, so the
    // window was mostly approval and the work underneath was unreadable. Capping the height was
    // not enough: the cap is a fraction of the window, so it scales with it and still crowds out
    // the list. The banner now opens as one summary line and shows cards only when asked.
    bool _gateExpanded = false;
    Dictionary<string, object> _gateRootCache;   // so the toggle can repaint without waiting a tick

    // RAM admission headroom (mirrors relay_fleet.auto_concurrency headroom_mb=2048): when free
    // physical RAM is around/under this the runner can't open another tab. We additionally surface
    // RAM in the banner when avail_mb is conspicuously low, even if disk is fine.
    const int RAM_FLOOR_MB = 2048;

    UIElement BuildCapBanner()
    {
        _capBanner = new Border();
        _capBanner.Visibility = Visibility.Collapsed;
        _capBanner.CornerRadius = new CornerRadius(Theme.RadPopover);
        _capBanner.BorderThickness = new Thickness(1);
        _capBanner.Padding = new Thickness(16, 8, 12, 8);
        _capBanner.Margin = new Thickness(24, 0, 16, 6);
        DockPanel.SetDock(_capBanner, Dock.Top);
        var dp = new DockPanel();
        var btns = new StackPanel(); btns.Orientation = Orientation.Horizontal;
        btns.HorizontalAlignment = HorizontalAlignment.Right;
        DockPanel.SetDock(btns, Dock.Right);
        _capForceBtn = new Button();
        _capForceBtn.Cursor = Cursors.Hand; _capForceBtn.BorderThickness = new Thickness(0);
        _capForceBtn.Padding = new Thickness(12, 4, 12, 4); _capForceBtn.FontWeight = FontWeights.SemiBold;
        _capForceBtn.Template = FlatButtonTemplate();
        _capForceBtn.Click += delegate { ForceStart(); };
        btns.Children.Add(_capForceBtn);
        _capRestoreBtn = new Button();
        _capRestoreBtn.Cursor = Cursors.Hand; _capRestoreBtn.BorderThickness = new Thickness(1);
        _capRestoreBtn.Padding = new Thickness(12, 4, 12, 4); _capRestoreBtn.Margin = new Thickness(8, 0, 0, 0);
        _capRestoreBtn.Template = FlatButtonTemplate();
        _capRestoreBtn.Visibility = Visibility.Collapsed;
        _capRestoreBtn.Click += delegate { RestoreFloor(); };
        btns.Children.Add(_capRestoreBtn);
        dp.Children.Add(btns);
        _capBannerLbl = new TextBlock();
        _capBannerLbl.VerticalAlignment = VerticalAlignment.Center; _capBannerLbl.FontSize = 13;
        _capBannerLbl.TextWrapping = TextWrapping.Wrap;
        dp.Children.Add(_capBannerLbl);
        _capBanner.Child = dp;
        return _capBanner;
    }

    // ── command_rejections banner ─────────────────────────────────────────────────────────
    // status.json's `command_rejections` (relay/fleet_runner.py, e822fb6): every fleet command
    // (from this cockpit, CopilotChat, or a stale/foreign writer) that validate_command refused
    // is recorded here, newest last, up to MAX_REJECTIONS_KEPT=20. Before this the only place a
    // refusal was visible was the fleet's own console log and a landing receipt nobody watching
    // the cockpit would think to open -- an operator could send a bad set_disk_floor_gb or
    // add_goal and see nothing happen, with no clue why. Plain, one line, not a modal: this is
    // information about something that already happened and needs no decision from the operator.
    Border _rejBanner;
    TextBlock _rejBannerLbl;

    UIElement BuildRejectionBanner()
    {
        _rejBanner = new Border();
        _rejBanner.Visibility = Visibility.Collapsed;
        _rejBanner.CornerRadius = new CornerRadius(Theme.RadPopover);
        _rejBanner.BorderThickness = new Thickness(1);
        _rejBanner.Padding = new Thickness(16, 8, 12, 8);
        _rejBanner.Margin = new Thickness(24, 0, 16, 6);
        DockPanel.SetDock(_rejBanner, Dock.Top);
        _rejBannerLbl = new TextBlock();
        _rejBannerLbl.VerticalAlignment = VerticalAlignment.Center; _rejBannerLbl.FontSize = 13;
        _rejBannerLbl.TextWrapping = TextWrapping.Wrap;
        _rejBanner.Child = _rejBannerLbl;
        return _rejBanner;
    }

    // Reactively show the NEWEST rejection each tick (rows are append-only, newest last -- see
    // record_command_rejection). Hides when the array is empty or unreadable. `keys`/`errors`
    // are exactly what relay/fleet_runner.py's record_command_rejection recorded: the refused
    // command's KEYS and validate_command's reasons, deliberately never the values a steer or a
    // goal carried.
    void UpdateRejectionBanner(Dictionary<string, object> root)
    {
        if (_rejBanner == null) return;
        object ro;
        object[] rows = (root != null && root.TryGetValue("command_rejections", out ro) && ro is object[])
                        ? (object[])ro : null;
        Dictionary<string, object> last = null;
        if (rows != null)
            for (int i = rows.Length - 1; i >= 0 && last == null; i--)
                last = rows[i] as Dictionary<string, object>;
        if (last == null)
        {
            _rejBanner.Visibility = Visibility.Collapsed;
            return;
        }
        string keys = "";
        object ko;
        if (last.TryGetValue("keys", out ko) && ko is object[])
            keys = string.Join(",", Array.ConvertAll((object[])ko, x => x == null ? "" : x.ToString()));
        string errs = "";
        object eo;
        if (last.TryGetValue("errors", out eo) && eo is object[])
            errs = string.Join("; ", Array.ConvertAll((object[])eo, x => x == null ? "" : x.ToString()));
        string when = "";
        if (last.ContainsKey("ts"))
        {
            try { when = AgeMinutesText((NowUnix() - Convert.ToDouble(last["ts"])) / 60.0); }
            catch (Exception) { }
        }
        string msg = L(
            "コマンドが拒否されました" + (when.Length == 0 ? "" : "（" + when + "）") + "：" +
            (keys.Length == 0 ? "(キーなし)" : keys) + (errs.Length == 0 ? "" : " — " + errs),
            "Command rejected" + (when.Length == 0 ? "" : " (" + when + ")") + ": " +
            (keys.Length == 0 ? "(no keys)" : keys) + (errs.Length == 0 ? "" : " — " + errs));
        _rejBannerLbl.Text = msg;
        _rejBanner.Visibility = Visibility.Visible;
    }

    // Reactively show/hide the capacity-wait banner each tick. Condition: a LIVE run with at least
    // one worker held at "pending"/"待機列" AND (disk below floor OR RAM conspicuously low). Once
    // the gate clears (or the run ends) the banner hides itself -- it never sticks.
    void UpdateCapBanner(Dictionary<string, object> root)
    {
        if (_capBanner == null) return;

        // The 床OFF (force-running) indicator is independent of the queue: while the user has the
        // gate disabled, keep showing it (with 床を戻す) so the override is never silent.
        if (_diskFloorForced)
        {
            _capBanner.Visibility = Visibility.Visible;
            _capBannerLbl.Text = T("floor_off");
            _capForceBtn.Visibility = Visibility.Collapsed;
            _capRestoreBtn.Visibility = Visibility.Visible;
            _capRestoreBtn.Content = T("floor_restore");
            return;
        }

        bool running = root != null && (!root.ContainsKey("running") || Convert.ToBoolean(root["running"]));
        bool pendingHeld = false;
        if (running && root.ContainsKey("workers") && root["workers"] is object[])
            foreach (object o in (object[])root["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w != null && S(w, "status") == "pending") { pendingHeld = true; break; }
            }

        double freeDisk = Dbl(root ?? new Dictionary<string, object>(), "free_disk_gb");
        double floor = Dbl(root ?? new Dictionary<string, object>(), "disk_floor_gb");
        int availMb = I(root ?? new Dictionary<string, object>(), "avail_mb");
        bool diskGated = floor > 0 && freeDisk > 0 && freeDisk < floor;
        // gate on the live status' ram_floor_mb if present, else the cockpit's configurable _ramFloor
        // (no longer the hardcoded RAM_FLOOR_MB const).
        double ramFloorNow = Dbl(root ?? new Dictionary<string, object>(), "ram_floor_mb");
        if (ramFloorNow <= 0) ramFloorNow = _ramFloor;
        bool ramGated = availMb > 0 && ramFloorNow > 0 && availMb < ramFloorNow;

        if (running && pendingHeld && (diskGated || ramGated))
        {
            _capForceBtn.Visibility = Visibility.Visible;
            _capForceBtn.Content = T("force_start");
            _capRestoreBtn.Visibility = Visibility.Collapsed;
            string msg;
            if (diskGated)
            {
                int need = (int)Math.Ceiling(floor - freeDisk);
                msg = _lang == 0
                    ? "ディスクの空きが少ないため、新しいタブを開かずに待機しています（空き " + freeDisk + "GB / 確保 " + floor + "GB）。"
                      + need + "GB 空けると自動で再開します。"
                    : "Disk space is low, so new tabs are paused (free " + freeDisk + "GB / floor " + floor + "GB). "
                      + "Freeing " + need + "GB resumes automatically.";
                if (ramGated)
                    msg += _lang == 0 ? "（空きRAMも " + availMb + "MB と少なめ）"
                                      : " (free RAM is also low at " + availMb + "MB)";
            }
            else
            {
                msg = _lang == 0
                    ? "RAMが少ないため、新しいタブを開かずに待機しています（空き " + availMb + "MB）。"
                    : "RAM is low, so new tabs are paused (free " + availMb + "MB).";
            }
            _capBannerLbl.Text = msg;
            _capBanner.Visibility = Visibility.Visible;
        }
        else
        {
            _capBanner.Visibility = Visibility.Collapsed;
        }
    }

    // 強制開始: disable the disk gate live. Capture the current floor first so 床を戻す can restore
    // it. Writes {"set_disk_floor_gb":0.0} via the SAME one-file-per-command writer Pause uses, so a
    // queued close/steer/set_maxtabs isn't clobbered. Consumed by fleet_runner.py ~L561.
    void ForceStart()
    {
        if (!RunIsLive()) return;     // nothing alive to consume the command
        var root = ReadStatus();
        _diskFloorPrev = Dbl(root ?? new Dictionary<string, object>(), "disk_floor_gb");
        SendCommand(Cmd1("set_disk_floor_gb", 0.0));
        _diskFloorForced = true;
        UpdateCapBanner(root);
    }

    // 床を戻す: write the previously-captured floor back, re-arming the disk gate.
    void RestoreFloor()
    {
        SendCommand(Cmd1("set_disk_floor_gb", _diskFloorPrev));
        _diskFloorForced = false;
        UpdateCapBanner(ReadStatus());
    }

    // ── APPROVAL CENTER: durable gates, available even while the fleet is idle ───────────
    //
    // ONE RESOLUTION, USED BY BOTH CLASSES. ApprovalPromptWindow cannot call an instance method
    // of CockpitWindow, so it had its own rule -- "the gate's folder must be named
    // .companion_gates" -- while the WRITER (tools/gate_ops.py) resolves
    // `MCP_GATE_DIR or ALLOWED_BASE/.companion_gates`. Whenever MCP_GATE_DIR pointed elsewhere
    // the two disagreed, the prompt threw "invalid approval-gate path", and the window closed
    // itself in `Loaded` before painting anything: the operator clicked a notification and
    // nothing happened, with no message anywhere. Two places deciding where gates live, in one
    // file, because one of them could not reach the other's method.
    string GateDirectory() { return ResolveGateDirectory(EnvValue("MCP_ALLOWED_BASE"),
                                                         EnvValue("MCP_GATE_DIR")); }

    //: `fileBase` / `fileGateDir` are what the .env says; the process environment wins, exactly
    //: as it does in gate_ops.py. Static so the approval window can ask the same question.
    internal static string ResolveGateDirectory(string fileBase, string fileGateDir)
    {
        try
        {
            // MCP_GATE_DIR FIRST, because that is the order the writer uses. It is what the
            // test suite sets to keep its gates out of the operator's queue, and a cockpit that
            // ignores it can never open a gate written under it.
            string over = Environment.GetEnvironmentVariable("MCP_GATE_DIR") ?? "";
            if (string.IsNullOrWhiteSpace(over)) over = fileGateDir ?? "";
            over = (over ?? "").Trim().Trim('"');
            if (over.Length > 0) return Path.GetFullPath(over);

            string raw = Environment.GetEnvironmentVariable("MCP_ALLOWED_BASE") ?? "";
            if (string.IsNullOrWhiteSpace(raw)) raw = fileBase ?? "";
            raw = (raw ?? "").Trim();
            string basePath;
            if (raw.Length == 0 || raw == "*")
                basePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            else
            {
                string[] roots = raw.Split(Path.PathSeparator);
                basePath = roots.Length > 0 ? roots[0].Trim().Trim('"') : "";
                if (basePath == "~") basePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                else if (basePath.StartsWith("~" + Path.DirectorySeparatorChar) || basePath.StartsWith("~/"))
                    basePath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), basePath.Substring(2));
                if (basePath.Length == 2 && basePath[1] == ':') basePath += Path.DirectorySeparatorChar;
                if (basePath.Length == 0) basePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            }
            return Path.Combine(Path.GetFullPath(basePath), ".companion_gates");
        }
        catch (Exception)
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".companion_gates");
        }
    }

    List<Dictionary<string, object>> ReadAllGates(Dictionary<string, object> statusRoot)
    {
        // A 1.5-second cache keeps the 700ms UI tick from repeatedly parsing a large audit history,
        // while still surfacing new approvals promptly. AnswerGate invalidates it immediately.
        double now = NowUnix();
        if (now - _gateCacheAt < 1.5) return new List<Dictionary<string, object>>(_gateCache);
        var gates = new List<Dictionary<string, object>>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            string dir = GateDirectory();
            if (Directory.Exists(dir))
            {
                foreach (string p in Directory.GetFiles(dir, "gate_*.json"))
                {
                    try
                    {
                        string text;
                        using (var fs = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                        using (var sr = new StreamReader(fs, Encoding.UTF8)) text = sr.ReadToEnd();
                        var g = _js.DeserializeObject(text) as Dictionary<string, object>;
                        if (g == null) continue;
                        string token = S(g, "token");
                        if (token.Length == 0) token = Path.GetFileNameWithoutExtension(p);
                        g["token"] = token; g["path"] = p;
                        if (seen.Add(token)) gates.Add(g);
                    }
                    catch (Exception) { }
                }
            }
        }
        catch (Exception) { }

        gates.Sort(delegate (Dictionary<string, object> a, Dictionary<string, object> b)
        { return Dbl(b, "asked_at").CompareTo(Dbl(a, "asked_at")); });
        _gateCache = gates; _gateCacheAt = now;
        return new List<Dictionary<string, object>>(_gateCache);
    }

    static bool GateAnswered(Dictionary<string, object> gate)
    {
        try { return gate.ContainsKey("answered") && Convert.ToBoolean(gate["answered"]); }
        catch (Exception) { return false; }
    }

    static bool GateExpired(Dictionary<string, object> gate)
    {
        double expiry = Dbl(gate, "expires_at");
        return expiry > 0 && expiry < NowUnix();
    }

    string GateKind(Dictionary<string, object> gate)
    {
        string token = S(gate, "token").ToLowerInvariant();
        string hay = (S(gate, "context") + " " + S(gate, "question")).ToLowerInvariant();
        if (token.StartsWith("gate_skill_") || hay.Contains("skill approval")) return _lang == 0 ? "Skill" : "Skill";
        if (hay.Contains("contract gate: delete") || hay.Contains(" delete")) return _lang == 0 ? "削除" : "Delete";
        if (hay.Contains("outbound") || hay.Contains("send_immediately")) return _lang == 0 ? "外部送信" : "External send";
        if (hay.Contains("shell_destructive") || hay.Contains("destructive shell")) return _lang == 0 ? "破壊的shell" : "Destructive shell";
        if (hay.Contains("task_router job class")) return _lang == 0 ? "ジョブ" : "Job";
        return _lang == 0 ? "確認" : "Decision";
    }

    bool GateNeedsSecondConfirmation(Dictionary<string, object> gate)
    {
        string kind = GateKind(gate);
        return kind == "削除" || kind == "Delete" || kind == "外部送信" ||
               kind == "External send" || kind == "破壊的shell" || kind == "Destructive shell";
    }

    string GateAge(Dictionary<string, object> gate)
    {
        double age = Math.Max(0, NowUnix() - Dbl(gate, "asked_at"));
        if (age < 60) return _lang == 0 ? "たった今" : "just now";
        if (age < 3600) return ((int)(age / 60)).ToString() + (_lang == 0 ? "分前" : " min ago");
        if (age < 86400) return ((int)(age / 3600)).ToString() + (_lang == 0 ? "時間前" : " hr ago");
        return ((int)(age / 86400)).ToString() + (_lang == 0 ? "日前" : " days ago");
    }

    List<Dictionary<string, object>> PendingGates(Dictionary<string, object> root)
    {
        var pending = new List<Dictionary<string, object>>();
        foreach (var gate in ReadAllGates(root)) if (!GateAnswered(gate)) pending.Add(gate);
        return pending;
    }

    // First two lines of a gate context, ellipsised. Keeps the banner card a fixed, predictable
    // height so the Approve/Deny row below it stays on screen no matter how long the context is.
    // lang is passed in because this is static: the counter below is user-visible text
    // and was pinned to Japanese, so it needs to know the mode like every other label.
    static string GateContextPreview(string context, int lang)
    {
        if (string.IsNullOrEmpty(context)) return "";
        string[] lines = context.Replace("\r\n", "\n").Split('\n');
        var kept = new List<string>();
        foreach (string line in lines)
        {
            if (line.Trim().Length == 0) continue;
            kept.Add(line.Length > 110 ? line.Substring(0, 110) + "..." : line);
            if (kept.Count == 2) break;
        }
        string text = string.Join("\n", kept.ToArray());
        int shown = kept.Count, total = 0;
        foreach (string line in lines) if (line.Trim().Length > 0) total++;
        // 助数詞が日本語固定だった。英語表示でも「行)」が出ていた。
        if (total > shown)
            text += "\n... (+" + (total - shown) + (lang == 0 ? " 行)" : " more)");
        return text;
    }

    // Builds the outer gate banner container (always Collapsed until a gate appears).
    UIElement BuildGateBanner()
    {
        _gateBanner = new Border();
        _gateBanner.Visibility = Visibility.Collapsed;
        _gateBanner.CornerRadius = new CornerRadius(Theme.RadPopover);
        // Uniform, not a thicker left edge: a one-sided stripe reads as sticky-note
        // decoration. The banner's border colour already carries the urgency.
        _gateBanner.BorderThickness = new Thickness(1);
        _gateBanner.Padding = new Thickness(16, 12, 12, 12);
        // 18px left gutter: the same one _pinnedToolbarHost and the card list use, so inside the
        // run column the banner shares its left edge with the cards it is about.
        _gateBanner.Margin = new Thickness(16, 6, 16, 6);
        DockPanel.SetDock(_gateBanner, Dock.Top);
        _gateCardsPanel = new StackPanel();
        // The banner is DOCKED, so it sits outside the card list's ScrollViewer -- the only
        // scroller in the window. Left unbounded it takes whatever height it wants: with three
        // pending Skill gates at 1.5x zoom the health strip and counters were measured at zero
        // height, the list's viewport collapsed to nothing, and controls landed at y=1199 on a
        // 1080-tall screen. On a shorter display the Approve/Deny rows themselves cross the
        // bottom edge, and with the list scroller gone there is nothing left to scroll -- the
        // request becomes impossible to answer. Its own scroller bounds it instead.
        _gateScroll = new ScrollViewer();
        _gateScroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _gateScroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        _gateScroll.Content = _gateCardsPanel;
        _gateBanner.Child = _gateScroll;
        return _gateBanner;
    }

    // Called each OnTick. Rebuilds the gate banner cards ONLY when the pending token set changes
    // (sig check), so the buttons don't flicker every 700ms while a gate is active.
    void UpdateGateBanner(Dictionary<string, object> root)
    {
        if (_gateBanner == null || _gateCardsPanel == null) return;
        _gateRootCache = root;

        // Read the durable gate directory directly.  This is deliberately independent of
        // status.json so Skill/import approvals remain visible before and after a fleet run.
        List<Dictionary<string, object>> gates = PendingGates(root);
        PaintApprovalCenterButton(gates.Count);

        // Re-cap on every tick: the window can be resized and the zoom changed at any time.
        // ActualHeight is in physical units while the banner lives inside the scaled root, so
        // divide by the zoom to get the units this MaxHeight is actually measured in.
        if (_gateScroll != null)
        {
            double zoom = (_rootScale != null && _rootScale.ScaleY > 0.1) ? _rootScale.ScaleY : 1.0;
            double usable = (ActualHeight > 0 ? ActualHeight : 760) / zoom;
            // Collapsed the banner is a single summary line, so it needs no fraction of the
            // window at all. Expanded, a quarter -- 0.45 left the run list at a few dozen px on
            // a 760-tall window once the header, health strip and composer took their share.
            _gateScroll.MaxHeight = _gateExpanded ? Math.Max(150, usable * 0.25) : 72;
        }
        if (_approvalCenterWindow != null && _approvalCenterWindow.IsVisible)
            RefreshApprovalCenter();

        // Build a signature from the current token set (order-insensitive for stability).
        var sb2 = new StringBuilder();
        foreach (var g in gates) sb2.Append(S(g, "token")).Append(';');
        // The expand state is part of what is rendered, so it belongs in the signature -- without
        // it the toggle changes a bool and the sig check returns before anything is redrawn.
        sb2.Append(_gateExpanded ? "|open" : "|shut");
        string newSig = sb2.ToString();
        if (newSig == _gateSig) return;   // nothing changed; skip rebuild to avoid flicker
        _gateSig = newSig;

        _gateCardsPanel.Children.Clear();

        if (gates.Count == 0)
        {
            _gateBanner.Visibility = Visibility.Collapsed;
            return;
        }

        // Heading row: a single line carrying the count and the expand toggle. Collapsed, this row
        // IS the whole banner -- the count is what the owner needs at a glance, and the cards are
        // one click away either here or in the Approval Center.
        bool ja3 = _lang == 0;
        var headRow = new StackPanel();
        headRow.Orientation = Orientation.Horizontal;
        headRow.Margin = new Thickness(0, 0, 0, _gateExpanded ? 8 : 0);

        var headTb = new TextBlock();
        headTb.Text = ja3
            ? ("承認待ち " + gates.Count + " 件 / Approval needed")
            : ("Approval needed: " + gates.Count + " / 承認待ち");
        headTb.FontSize = 13;
        headTb.FontWeight = FontWeights.SemiBold;
        headTb.Foreground = Theme.Br(Theme.Warning(_dark));
        headTb.VerticalAlignment = VerticalAlignment.Center;
        headRow.Children.Add(headTb);

        var toggleBtn = new Button();
        toggleBtn.Cursor = Cursors.Hand;
        toggleBtn.FontSize = 11;
        toggleBtn.BorderThickness = new Thickness(0);
        toggleBtn.Background = Brushes.Transparent;
        toggleBtn.Foreground = Theme.Br(Theme.Warning(_dark));
        toggleBtn.Padding = new Thickness(0);
        toggleBtn.Margin = new Thickness(12, 0, 0, 0);
        toggleBtn.VerticalAlignment = VerticalAlignment.Center;
        toggleBtn.Content = _gateExpanded ? (ja3 ? "たたむ" : "Collapse") : (ja3 ? "開く" : "Expand");
        toggleBtn.Click += delegate (object s3, RoutedEventArgs e3)
        {
            e3.Handled = true;
            _gateExpanded = !_gateExpanded;
            // Repaint now rather than on the next 700ms tick, so the click feels like it did
            // something. The cached root is the one this method was last called with.
            if (_gateRootCache != null) UpdateGateBanner(_gateRootCache);
        };
        headRow.Children.Add(toggleBtn);

        var centerBtn = new Button();
        centerBtn.Cursor = Cursors.Hand;
        centerBtn.FontSize = 11;
        centerBtn.BorderThickness = new Thickness(0);
        centerBtn.Background = Brushes.Transparent;
        centerBtn.Foreground = Theme.Br(Theme.Muted(_dark));
        centerBtn.Padding = new Thickness(0);
        centerBtn.Margin = new Thickness(12, 0, 0, 0);
        centerBtn.VerticalAlignment = VerticalAlignment.Center;
        centerBtn.Content = ja3 ? "承認センター" : "Approval Center";
        centerBtn.Click += delegate (object s3, RoutedEventArgs e3) { e3.Handled = true; ShowApprovalCenter(); };
        headRow.Children.Add(centerBtn);

        _gateCardsPanel.Children.Add(headRow);

        if (!_gateExpanded)
        {
            _gateBanner.Visibility = Visibility.Visible;
            return;
        }

        // Show first gate (or all). If more than 1, show a count note below.
        int showCount = gates.Count > 3 ? 3 : gates.Count;
        for (int gi = 0; gi < showCount; gi++)
        {
            var g2 = gates[gi];
            string question = S(g2, "question");
            string context2 = S(g2, "context");
            string gatePath = S(g2, "path");

            // Question card.
            var gCard = new Border();
            gCard.BorderBrush = Theme.Br(Theme.Warning(_dark));
            gCard.BorderThickness = new Thickness(1);
            gCard.CornerRadius = new CornerRadius(Theme.RadSmall);
            gCard.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            gCard.Padding = new Thickness(12, 8, 12, 8);
            gCard.Margin = new Thickness(0, 0, 0, gi < showCount - 1 ? 8 : 0);

            var gInner = new StackPanel();

            var qTb = new TextBlock();
            qTb.Text = question;
            qTb.FontSize = 13;
            qTb.Foreground = Theme.Br(Theme.Text(_dark));
            qTb.TextWrapping = TextWrapping.Wrap;
            gInner.Children.Add(qTb);

            if (!string.IsNullOrEmpty(context2))
            {
                var ctxTb = new TextBlock();
                // Preview only. A Skill gate's context is a multi-line file/hash manifest; printed
                // in full it pushed the Approve/Deny row below the fold. The complete text is one
                // click away in the Approval Center, which scrolls.
                ctxTb.Text = GateContextPreview(context2, _lang);
                ctxTb.ToolTip = context2;
                ctxTb.FontSize = 11;
                ctxTb.Foreground = Theme.Br(Theme.Muted(_dark));
                ctxTb.TextWrapping = TextWrapping.Wrap;
                ctxTb.Margin = new Thickness(0, 3, 0, 0);
                gInner.Children.Add(ctxTb);
            }

            // Approve / Deny buttons.
            var btnRow = new StackPanel();
            btnRow.Orientation = Orientation.Horizontal;
            btnRow.Margin = new Thickness(0, 8, 0, 0);

            var approveBtn = new Button();
            approveBtn.Cursor = Cursors.Hand;
            approveBtn.BorderThickness = new Thickness(0);
            approveBtn.Padding = new Thickness(16, 4, 16, 4);
            approveBtn.FontWeight = FontWeights.SemiBold;
            approveBtn.FontSize = 12;
            approveBtn.Background = Theme.Br(Theme.AccentFill(_dark));
            approveBtn.Foreground = AccentFg;
            bool reviewFirst = GateNeedsSecondConfirmation(g2) || GateKind(g2) == "Skill";
            approveBtn.Content = reviewFirst
                ? (ja3 ? "詳細を確認" : "Review details")
                : (ja3 ? "承認 / Approve" : "Approve / 承認");
            approveBtn.IsEnabled = !GateExpired(g2);
            if (!approveBtn.IsEnabled)
                approveBtn.ToolTip = ja3 ? "期限切れです。元の操作から承認を再要求してください。" : "Expired. Request approval again from the originating action.";
            string gPath2 = gatePath;
            Dictionary<string, object> capturedGate = g2;
            approveBtn.Click += delegate (object s2, RoutedEventArgs e2)
            {
                e2.Handled = true;
                // High-impact operations require their exact scope to be reviewed in the
                // Approval Center before the second confirmation is offered.
                if (GateNeedsSecondConfirmation(capturedGate) || GateKind(capturedGate) == "Skill") ShowApprovalCenter();
                else AnswerGate(gPath2, "approved");
            };
            btnRow.Children.Add(approveBtn);

            var denyBtn = new Button();
            denyBtn.Cursor = Cursors.Hand;
            denyBtn.BorderThickness = new Thickness(1);
            denyBtn.Padding = new Thickness(16, 4, 16, 4);
            denyBtn.FontSize = 12;
            denyBtn.Margin = new Thickness(8, 0, 0, 0);
            denyBtn.Background = Brushes.Transparent;
            denyBtn.Foreground = Theme.Br(Theme.Danger(_dark));
            denyBtn.BorderBrush = Theme.Br(Theme.Danger(_dark));
            denyBtn.Content = ja3 ? "拒否 / Deny" : "Deny / 拒否";
            string gPath3 = gatePath;
            denyBtn.Click += delegate (object s2, RoutedEventArgs e2)
            {
                e2.Handled = true;
                AnswerGate(gPath3, "denied");
            };
            btnRow.Children.Add(denyBtn);

            gInner.Children.Add(btnRow);
            gCard.Child = gInner;
            _gateCardsPanel.Children.Add(gCard);
        }

        if (gates.Count > showCount)
        {
            var moreBtn = new Button();
            moreBtn.Content = (ja3
                ? ("+ さらに " + (gates.Count - showCount) + " 件の承認待ち")
                : ("+ " + (gates.Count - showCount) + " more gate(s) pending"));
            moreBtn.FontSize = 11; moreBtn.Cursor = Cursors.Hand;
            moreBtn.Foreground = Theme.Br(Theme.Warning(_dark));
            moreBtn.Background = Brushes.Transparent; moreBtn.BorderThickness = new Thickness(0);
            moreBtn.HorizontalAlignment = HorizontalAlignment.Left;
            moreBtn.Padding = new Thickness(0); moreBtn.Margin = new Thickness(0, 6, 0, 0);
            moreBtn.Click += delegate { ShowApprovalCenter(); };
            _gateCardsPanel.Children.Add(moreBtn);
        }

        _gateBanner.Visibility = Visibility.Visible;
    }

    void ShowApprovalCenter()
    {
        if (_approvalCenterWindow != null && _approvalCenterWindow.IsVisible)
        {
            _approvalCenterWindow.Activate();
            return;
        }
        var w = new Window();
        _approvalCenterWindow = w;
        w.Title = _lang == 0 ? "承認センター" : "Approval Center";
        // Clamp to the work area. A fixed 760x720 already filled a 1080-tall screen once
        // the UI scale (1.5 by default) was applied, pushing the Approve/Deny row off the
        // bottom -- and because the footer is docked rather than inside the scroller,
        // there was nothing to scroll to reach it: the request became undecidable.
        double waW = SystemParameters.WorkArea.Width;
        double waH = SystemParameters.WorkArea.Height;
        double scale = (_rootScale != null && _rootScale.ScaleY > 0.1) ? _rootScale.ScaleY : 1.0;
        w.Width = Math.Min(760, Math.Max(480, waW / scale - 60));
        w.Height = Math.Min(720, Math.Max(380, waH / scale - 60));
        w.MinWidth = Math.Min(560, w.Width);
        w.MinHeight = Math.Min(460, w.Height);
        w.MaxHeight = waH;
        w.WindowStartupLocation = WindowStartupLocation.CenterOwner; w.Owner = this;
        w.Background = Bg;
        w.KeyDown += delegate (object sender, KeyEventArgs e)
        { if (e.Key == Key.Escape) { w.Close(); e.Handled = true; } };
        w.Closed += delegate
        {
            _approvalCenterWindow = null; _approvalPendingHost = null; _approvalRecentHost = null;
            _approvalCenterSummary = null; _approvalCenterSig = "";
        };

        var root = new DockPanel();
        var head = new Border();
        head.Background = CardBg; head.BorderBrush = Border;
        head.BorderThickness = new Thickness(0, 0, 0, 1);
        head.Padding = new Thickness(24, 16, 24, 16);
        DockPanel.SetDock(head, Dock.Top);
        var headGrid = new Grid();
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var titleCol = new StackPanel();
        var title = new TextBlock(); title.Text = w.Title; title.Foreground = Fg;
        title.FontSize = 20; title.FontWeight = FontWeights.SemiBold;
        titleCol.Children.Add(title);
        var note = new TextBlock();
        note.Text = _lang == 0
            ? "実際の操作許可をここで判断します。run / plan / auto の実行方式とは別です。"
            : "Decide actual operation permissions here. This is separate from run / plan / auto mode.";
        note.Foreground = Muted; note.FontSize = 12; note.Margin = new Thickness(0, 4, 0, 0);
        note.TextWrapping = TextWrapping.Wrap; titleCol.Children.Add(note);
        headGrid.Children.Add(titleCol);
        _approvalCenterSummary = new TextBlock();
        _approvalCenterSummary.Foreground = Theme.Br(Theme.Warning(_dark));
        _approvalCenterSummary.FontWeight = FontWeights.SemiBold;
        _approvalCenterSummary.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterSummary.Margin = new Thickness(16, 0, 0, 0);
        Grid.SetColumn(_approvalCenterSummary, 1); headGrid.Children.Add(_approvalCenterSummary);
        head.Child = headGrid; root.Children.Add(head);

        var scroll = new ScrollViewer(); scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        scroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        var body = new StackPanel(); body.Margin = new Thickness(24, 20, 24, 28);
        body.Children.Add(BuildJobApprovalPolicyPanel());
        var pendingTitle = new TextBlock();
        pendingTitle.Text = _lang == 0 ? "判断が必要" : "Needs your decision";
        pendingTitle.Foreground = Fg; pendingTitle.FontSize = 14; pendingTitle.FontWeight = FontWeights.SemiBold;
        pendingTitle.Margin = new Thickness(0, 0, 0, 12); body.Children.Add(pendingTitle);
        _approvalPendingHost = new StackPanel(); body.Children.Add(_approvalPendingHost);

        var recentTitle = new TextBlock();
        recentTitle.Text = _lang == 0 ? "最近の判断" : "Recent decisions";
        recentTitle.Foreground = Fg; recentTitle.FontSize = 14; recentTitle.FontWeight = FontWeights.SemiBold;
        recentTitle.Margin = new Thickness(0, 24, 0, 12); body.Children.Add(recentTitle);
        _approvalRecentHost = new StackPanel(); body.Children.Add(_approvalRecentHost);
        scroll.Content = body; root.Children.Add(scroll);
        w.Content = root;
        _approvalCenterSig = "";
        RefreshApprovalCenter();
        w.ShowDialog();
    }

    UIElement BuildJobApprovalPolicyPanel()
    {
        var box = new Border(); box.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        box.BorderBrush = Border; box.BorderThickness = new Thickness(1);
        box.CornerRadius = new CornerRadius(Theme.RadCard); box.Padding = new Thickness(16, 12, 16, 12);
        box.Margin = new Thickness(0, 0, 0, 20);
        var col = new StackPanel();
        var title = new TextBlock(); title.Text = _lang == 0 ? "操作承認ポリシー" : "Operation approval policy";
        title.Foreground = Fg; title.FontSize = 13; title.FontWeight = FontWeights.SemiBold;
        col.Children.Add(title);
        var note = new TextBlock(); note.Foreground = Muted; note.FontSize = 11;
        note.TextWrapping = TextWrapping.Wrap; note.Margin = new Thickness(0, 4, 0, 0);
        note.Text = _lang == 0
            ? "ローカルジョブと自律契約に即時反映。STOP・パス制限・外部Skillの初回/変更承認は常に残ります。"
            : "Applies live to local jobs and autonomy contracts. STOP, path limits, and external-Skill trust remain enforced.";
        col.Children.Add(note);

        var selector = new ComboBox(); selector.MinWidth = 230; selector.HorizontalAlignment = HorizontalAlignment.Left;
        selector.Margin = new Thickness(0, 12, 0, 0); selector.Background = CardBg; selector.Foreground = Fg;
        // SAME ORDER AND SAME LABELS AS ApprovalPromptWindow's list. This is the second
        // selector for one setting, each with its own hard-coded array, and a test caught the
        // two disagreeing about which mode is recommended after only one was changed -- on
        // the surface an operator actually opens to change it.
        var labels = _lang == 0
            ? new string[] { "自動（推奨）", "バイパス", "毎回確認（非推奨）" }
            : new string[] { "Auto (recommended)", "Bypass", "Confirm every time (not recommended)" };
        var values = new string[] { "auto", "bypass", "default" };
        string current = ApprovalPromptWindow.ReadPolicy();
        for (int i = 0; i < values.Length; i++)
        {
            var item = new ComboBoxItem(); item.Content = labels[i]; item.Tag = values[i];
            selector.Items.Add(item); if (values[i] == current) selector.SelectedItem = item;
        }
        StyleFlatCombo(selector); col.Children.Add(selector);
        var help = new TextBlock(); help.Foreground = Muted; help.FontSize = 11;
        help.TextWrapping = TextWrapping.Wrap; help.Margin = new Thickness(0, 7, 0, 0);
        col.Children.Add(help);
        Action updateHelp = delegate
        {
            var selected = selector.SelectedItem as ComboBoxItem;
            string mode = selected == null ? "auto" : selected.Tag as string;
            if (mode == "auto") help.Text = _lang == 0
                ? "安全な操作は自動実行、要確認は承認待ち、禁止判定は拒否。"
                : "Safe operations run automatically; risky ones ask; prohibited ones are denied.";
            else if (mode == "bypass") help.Text = _lang == 0
                ? "手動確認を省略。常時有効な安全境界は解除しません。"
                : "Skip manual confirmations. Always-on safety boundaries remain.";
            else help.Text = _lang == 0
                ? "初回クラスを毎回確認。非推奨: 確認が多すぎると内容が読まれなくなります。"
                : "Asks about every first-seen class. Not recommended: an approval that is always there stops being read.";
        };
        updateHelp();
        bool reverting = false;
        selector.SelectionChanged += delegate
        {
            if (reverting) return;
            var item = selector.SelectedItem as ComboBoxItem; if (item == null) return;
            string next = item.Tag as string; string previous = ApprovalPromptWindow.ReadPolicy();
            if (next == "bypass")
            {
                string warning = _lang == 0
                    ? "バイパスではローカルジョブと自律契約の手動確認を省略します。\nSTOP条件、ファイル範囲、外部Skill承認は残ります。\n\n有効にしますか？"
                    : "Bypass skips manual approval for local jobs and autonomy contracts.\nSTOP rules, path limits, and external-Skill approval remain.\n\nEnable it?";
                if (MessageBox.Show(_approvalCenterWindow, warning, _lang == 0 ? "バイパスを有効化" : "Enable bypass",
                    MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes)
                {
                    reverting = true;
                    foreach (object obj in selector.Items)
                    { var old = obj as ComboBoxItem; if (old != null && (old.Tag as string) == previous) selector.SelectedItem = old; }
                    reverting = false; updateHelp(); return;
                }
            }
            ApprovalPromptWindow.SavePolicy(next); updateHelp();
        };
        box.Child = col; return box;
    }

    void RefreshApprovalCenter()
    {
        if (_approvalCenterWindow == null || _approvalPendingHost == null || _approvalRecentHost == null) return;
        var all = ReadAllGates(ReadStatus());
        var sig = new StringBuilder();
        foreach (var gate in all)
        {
            // Expiry belongs in the signature. Without it a card drawn while the request
            // was still valid kept its Approve button ENABLED after the deadline passed:
            // the click then wrote "approved" that the trust store refused, so the user
            // saw an approval that never took effect. Redraw when the deadline crosses.
            sig.Append(S(gate, "token")).Append(':')
               .Append(GateAnswered(gate) ? S(gate, "answer") : "open").Append(':')
               .Append(GateExpired(gate) ? "exp" : "live").Append(';');
        }
        string currentSig = sig.ToString() + (_dark ? "D" : "L") + _lang;
        if (currentSig == _approvalCenterSig) return;
        _approvalCenterSig = currentSig;

        _approvalPendingHost.Children.Clear(); _approvalRecentHost.Children.Clear();
        var pending = new List<Dictionary<string, object>>();
        var recent = new List<Dictionary<string, object>>();
        // RECENT DECISIONS ARE DE-DUPLICATED BY CONTENT, NOT DELETED FROM DISK. A gate answered
        // under `auto`/`bypass` job policy (see task_router.job_gate) never reaches this list to
        // begin with -- those modes return ALLOW without ever writing a gate file, so there is
        // nothing here to hide for them. What DOES flood this list in practice is the SAME
        // decision recorded over and over: an external Skill's approval expires (APPROVAL_TTL in
        // relay/skills.py) and gets re-approved against the identical, unchanged bundle, or a
        // background loop repeats the identical request. Measured on one machine: 2169 of 2175
        // gate files were Skill approvals, and 2167 of those were the SAME skill at the SAME
        // content digest -- one signature, answered thousands of times, burying everything else
        // under the 20-card cap below.
        //
        // `all` is sorted newest-first (ReadAllGates), so keeping the FIRST occurrence of a key
        // keeps the MOST RECENT instance of that decision as the list's representative. A Skill's
        // first-ever approval, or its re-approval after the bundle CHANGED, carries a different
        // digest and therefore a different question string -- a different key -- so it is never
        // folded away here. Nothing is written back to the gate file or deleted: the on-disk
        // gate_*.json is the durable log this collapsing must not touch.
        var seenRecentKeys = new HashSet<string>(StringComparer.Ordinal);
        foreach (var gate in all)
        {
            if (!GateAnswered(gate)) { pending.Add(gate); continue; }
            string recentKey = GateKind(gate) + "" + S(gate, "question") + "" + S(gate, "answer");
            if (seenRecentKeys.Add(recentKey)) recent.Add(gate);
        }
        if (_approvalCenterSummary != null)
            _approvalCenterSummary.Text = _lang == 0 ? (pending.Count + " 件 未処理") : (pending.Count + " pending");
        PaintApprovalCenterButton(pending.Count);

        if (pending.Count == 0)
        {
            var empty = new Border(); empty.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            empty.BorderBrush = Border; empty.BorderThickness = new Thickness(1);
            empty.CornerRadius = new CornerRadius(Theme.RadCard); empty.Padding = new Thickness(16, 16, 16, 16);
            var text = new TextBlock();
            text.Text = _lang == 0 ? "未処理の承認はありません。" : "No approvals are waiting.";
            text.Foreground = Muted; text.FontSize = 13; empty.Child = text;
            _approvalPendingHost.Children.Add(empty);
        }
        else foreach (var gate in pending) _approvalPendingHost.Children.Add(BuildGateDecisionCard(gate, true));

        int limit = Math.Min(20, recent.Count);
        if (limit == 0)
        {
            var none = new TextBlock(); none.Text = _lang == 0 ? "履歴はまだありません。" : "No decision history yet.";
            none.Foreground = Muted; none.FontSize = 12; _approvalRecentHost.Children.Add(none);
        }
        else for (int i = 0; i < limit; i++) _approvalRecentHost.Children.Add(BuildGateDecisionCard(recent[i], false));
    }

    UIElement BuildGateDecisionCard(Dictionary<string, object> gate, bool actionable)
    {
        bool expired = GateExpired(gate);
        var card = new Border();
        card.Background = CardBg;
        card.BorderBrush = actionable ? Theme.Br(expired ? Theme.Danger(_dark) : Theme.Warning(_dark)) : Border;
        // Actionable cards are marked by border COLOUR, not by a fatter left edge --
        // a one-sided stripe reads as sticky-note decoration.
        card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(Theme.RadCard); card.Padding = new Thickness(16, 12, 16, 12);
        card.Margin = new Thickness(0, 0, 0, 12);
        var col = new StackPanel();

        var meta = new DockPanel(); meta.LastChildFill = true;
        var chip = new Border(); chip.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        chip.BorderBrush = actionable ? Theme.Br(Theme.Warning(_dark)) : Border;
        chip.BorderThickness = new Thickness(1); chip.CornerRadius = new CornerRadius(Theme.RadCard);
        chip.Padding = new Thickness(7, 2, 7, 2); chip.Margin = new Thickness(0, 0, 8, 0);
        var chipText = new TextBlock(); chipText.Text = GateKind(gate); chipText.FontSize = 10;
        chipText.FontWeight = FontWeights.SemiBold; chipText.Foreground = actionable ? Theme.Br(Theme.Warning(_dark)) : Muted;
        chip.Child = chipText; DockPanel.SetDock(chip, Dock.Left); meta.Children.Add(chip);
        var age = new TextBlock();
        age.Text = (expired ? (_lang == 0 ? "期限切れ · " : "Expired · ") : "") + GateAge(gate);
        age.Foreground = expired ? Theme.Br(Theme.Danger(_dark)) : Muted; age.FontSize = 11;
        age.VerticalAlignment = VerticalAlignment.Center; meta.Children.Add(age);
        col.Children.Add(meta);

        var question = new TextBlock(); question.Text = S(gate, "question");
        question.Foreground = Fg; question.FontSize = 13; question.FontWeight = FontWeights.SemiBold;
        question.TextWrapping = TextWrapping.Wrap; question.Margin = new Thickness(0, 8, 0, 0);
        col.Children.Add(question);

        string context = S(gate, "context");
        if (context.Length > 0)
        {
            var details = new Expander(); details.Header = _lang == 0 ? "対象と詳細を確認" : "Review scope and details";
            details.Foreground = Muted; details.FontSize = 11; details.Margin = new Thickness(0, 7, 0, 0);
            details.IsExpanded = actionable && (GateNeedsSecondConfirmation(gate) || GateKind(gate) == "Skill");
            var detailText = new TextBlock(); detailText.Text = context; detailText.Foreground = Muted;
            detailText.FontFamily = new FontFamily("Cascadia Mono, Consolas"); detailText.FontSize = 11;
            detailText.TextWrapping = TextWrapping.Wrap; detailText.Margin = new Thickness(12, 6, 0, 0);
            details.Content = detailText; col.Children.Add(details);
        }

        if (actionable)
        {
            var row = new StackPanel(); row.Orientation = Orientation.Horizontal; row.Margin = new Thickness(0, 12, 0, 0);
            var approve = new Button(); approve.Content = _lang == 0 ? "承認" : "Approve";
            approve.Cursor = Cursors.Hand; approve.Padding = new Thickness(16, 6, 16, 6);
            approve.BorderThickness = new Thickness(0); approve.Background = AccentFill; approve.Foreground = AccentFg;
            approve.FontWeight = FontWeights.SemiBold; approve.IsEnabled = !expired;
            System.Windows.Automation.AutomationProperties.SetName(approve, _lang == 0 ? "この操作を承認" : "Approve this operation");
            Dictionary<string, object> captured = gate;
            approve.Click += delegate
            {
                if (GateNeedsSecondConfirmation(captured))
                {
                    string msg = (_lang == 0 ? "影響の大きい操作です。対象を確認したうえで本当に承認しますか？\n\n" :
                        "This is a high-impact operation. Approve after reviewing the exact scope?\n\n") + S(captured, "question");
                    if (MessageBox.Show(_approvalCenterWindow, msg, _lang == 0 ? "最終確認" : "Final confirmation",
                        MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes) return;
                }
                AnswerGate(S(captured, "path"), "approved"); RefreshApprovalCenter();
            };
            row.Children.Add(approve);

            var deny = new Button(); deny.Content = _lang == 0 ? "拒否" : "Deny";
            deny.Cursor = Cursors.Hand; deny.Padding = new Thickness(16, 6, 16, 6);
            deny.Margin = new Thickness(8, 0, 0, 0); deny.Background = Brushes.Transparent;
            deny.Foreground = Theme.Br(Theme.Danger(_dark)); deny.BorderBrush = Theme.Br(Theme.Danger(_dark));
            deny.BorderThickness = new Thickness(1);
            System.Windows.Automation.AutomationProperties.SetName(deny, _lang == 0 ? "この操作を拒否" : "Deny this operation");
            deny.Click += delegate { AnswerGate(S(captured, "path"), "denied"); RefreshApprovalCenter(); };
            row.Children.Add(deny); col.Children.Add(row);

            if (expired)
            {
                var expiredNote = new TextBlock();
                expiredNote.Text = _lang == 0 ? "期限切れのため承認できません。拒否するか、元の操作から再要求してください。" :
                    "This request expired. Deny it or request approval again from the originating action.";
                expiredNote.Foreground = Theme.Br(Theme.Danger(_dark)); expiredNote.FontSize = 11;
                expiredNote.Margin = new Thickness(0, 7, 0, 0); expiredNote.TextWrapping = TextWrapping.Wrap;
                col.Children.Add(expiredNote);
            }
        }
        else
        {
            var verdict = new TextBlock();
            string answer = S(gate, "answer");
            bool approved = answer.Equals("approved", StringComparison.OrdinalIgnoreCase);
            verdict.Text = approved ? (_lang == 0 ? "承認済み" : "Approved") : (_lang == 0 ? "拒否済み" : "Denied");
            verdict.Foreground = Theme.Br(approved ? Theme.Success(_dark) : Theme.Danger(_dark));
            verdict.FontSize = 11; verdict.FontWeight = FontWeights.SemiBold; verdict.Margin = new Thickness(0, 8, 0, 0);
            col.Children.Add(verdict);
        }
        card.Child = col; return card;
    }

    // Atomic gate answer: read the file at `path`, set answered=true + answer=<verdict>, write back.
    // Uses temp-file + rename for atomicity (consistent with the relay's reader expectation).
    void AnswerGate(string path, string verdict)
    {
        if (string.IsNullOrEmpty(path)) return;
        try
        {
            // Normalize: forward slashes may be in the path from status.json.
            string localPath = Path.GetFullPath(path.Replace('/', Path.DirectorySeparatorChar));
            string gateRoot = Path.GetFullPath(GateDirectory()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!localPath.StartsWith(gateRoot, StringComparison.OrdinalIgnoreCase)) return;
            if (!File.Exists(localPath)) return;

            string text;
            using (var fs2 = new FileStream(localPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr2 = new System.IO.StreamReader(fs2, Encoding.UTF8))
                text = sr2.ReadToEnd();

            var gd = _js.DeserializeObject(text) as Dictionary<string, object>;
            if (gd == null) gd = new Dictionary<string, object>();
            gd["answered"] = true;
            gd["answer"] = verdict;
            gd["answered_at"] = NowUnix();

            string updated = _js.Serialize(gd);
            // Atomic: write to a temp file in the same directory, then rename.
            string dir2 = Path.GetDirectoryName(localPath);
            string tmp = Path.Combine(dir2, Path.GetFileName(localPath) + ".tmp");
            File.WriteAllText(tmp, updated, new UTF8Encoding(false));
            try { File.Replace(tmp, localPath, null); }
            catch (Exception)
            {
                // Older/non-NTFS environments may not implement Replace; keep the same-directory
                // fallback for compatibility, then remove the temporary file.
                File.Copy(tmp, localPath, true);
                try { File.Delete(tmp); } catch (Exception) { }
            }

            // Force a sig reset so the banner refreshes on the next tick (the relay
            // will drop this gate from pending_gates once it sees answered=true).
            _gateSig = "";
            _approvalCenterSig = "";
            _gateCacheAt = 0;
        }
        catch (Exception) { }
    }

    // ── poll loop ─────────────────────────────────────────────────────────────────
    void OnTick(object sender, EventArgs e)
    {
        // follow external theme/lang/maxtabs edits (e.g. the chat toggled the theme)
        try
        {
            if (File.Exists(SettingsFile))
            {
                long m = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
                if (m != _settingsMtime)
                {
                    bool d0 = _dark; int l0 = _lang; double s0 = _uiScale;
                    bool a0 = _uiAuto; double t0 = _scaleTarget;
                    LoadSettings();
                    // The fan-out / effort-policy controls live in the gear popup, which may not
                    // exist yet (every Paint* is null-safe) and is rebuilt on each open. Repaint
                    // whatever is there so an external settings.txt edit shows without reopening.
                    // Paint* only assigns when different, so SelectionChanged does not re-fire.
                    PaintEffort();
                    PaintEffortPolicy();
                    PaintFanout();
                    if (d0 != _dark) { ApplyThemeBrushes(); PaintChrome(); _lastSig = ""; }
                    else if (l0 != _lang) { RebuildChrome(); }
                    // External ui_scale edit (e.g. the chat app zoomed / switched to auto): apply it live
                    // so both apps stay in lock-step. In AUTO the effective scale is per-monitor, so we
                    // recompute for THIS window's monitor (not the other app's) — silently. In MANUAL we
                    // push the shared number and flash the %. RebuildChrome above (if lang changed) already
                    // re-anchored the transform on the new root.
                    if (_uiAuto)
                    {
                        // Recompute if we just entered auto, or the target changed, so the transform tracks.
                        if (!a0 || Math.Abs(t0 - _scaleTarget) > 0.001 || Math.Abs(s0 - EffectiveAutoScale(CurrentMonitorScale())) > 0.001)
                            ApplyAutoScale(false);
                        RefreshUiScaleControls();
                    }
                    else if (a0 || Math.Abs(s0 - _uiScale) > 0.001)
                    {
                        // Other app set a manual number (or switched auto->manual): reflect + toast.
                        _uiScale = Math.Max(0.8, Math.Min(2.0, _uiScale));
                        PushScaleToTransform(); RefreshUiScaleControls(); ShowScaleToast();
                    }
                }
            }
        }
        catch (Exception) { }

        Dictionary<string, object> root = ReadStatus();
        RefreshSubmitted(root);             // the "submitted, not picked up yet" group (top of the list)
        RefreshPauseEnabled(root);          // Pause is only meaningful for a live run; grey it out otherwise
        RefreshStoppingState(root);         // FIX B: resolve the optimistic "stopping" state once the sweep confirms it
        UpdateGateBanner(root);             // Bucket C TASK 2: show pending approval gates (blocks worker until answered)
        UpdateCapBanner(root);              // TASK 1: surface the admission-gate wait reactively each tick
        UpdateRejectionBanner(root);        // show the newest refused fleet command (SEC-08, e822fb6)
        // (the re-unlock receipt used to be turned into a panel note here; the harness now
        //  delivers unlocks itself and the receipt is read from status.json by status.py)
        bool idle = root == null || I(root, "total") == 0
                    || (root.ContainsKey("idle") && Convert.ToBoolean(root["idle"]));
        if (idle)
        {
            _header.Text = "Fleet";
            _sub.Text = "";                // the empty-state block in the body carries the message now
            if (_subChips != null) _subChips.Children.Clear();   // Feature 2: no chips while idle either
            UpdateWorkerChip(0, 0, false); // configuration lives in Settings while idle
            // A2-2: collapse spine and idle composer when no run
            RefreshSpine(root, true);
            PaintComposerMode(false);
            // Idle: only history (or the empty state) is shown — no live board to filter, so the
            // pinned filter bar is hidden here just as the toolbar row was never emitted when idle.
            if (_pinnedToolbarHost != null)
            {
                _pinnedToolbarHost.Visibility = Visibility.Collapsed;
                _pinnedToolbarHost.Child = null;
                _pinnedToolbarSig = "";
            }
            // P2 RESUME affordance: only meaningful when NO run is live (this idle branch). Compute the
            // unfinished count from the last run's durable ledger; hide when 0.
            int resumeN = UnfinishedResumeCount();
            string resumeSig = resumeN > 0 ? ResumeStateSignature() : "";
            bool resumeDismissed = resumeN > 0 && ResumeStateDismissed(resumeSig);
            string isig = "IDLE" + _history.Count + (_dark ? "D" : "L") + _lang + "|q" + (_histQuery ?? "")
                          + "|r" + resumeN + "|rs" + resumeSig + "|rd" + (resumeDismissed ? "1" : "0")
                          + "|s" + _submittedSig;
            if (_lastSig != isig)
            {
                _lastRoot = null;
                var rows = new List<object>();
                // FIRST, in the idle state too: a submission made while nothing runs (StartFleet
                // before the run's first snapshot, fleet_submit, a for_fleet hand-off) is on top.
                AppendSubmittedRows(rows);
                int beforeRest = rows.Count;
                if (resumeN > 0 && !resumeDismissed)
                {
                    var rd = new Dictionary<string, object>();
                    rd["n"] = resumeN; rd["signature"] = resumeSig;
                    rows.Add(MkRow(8, null, rd));   // resume affordance (idle + N>0 only)
                }
                AppendHistoryRows(rows, null);   // idle: no live run on board -> show all history
                // empty state when nothing but (possibly) submitted rows is shown
                if (rows.Count == beforeRest) rows.Add(MkRow(5, null, null));
                SetRows(rows);
                _lastSig = isig;
            }
            return;
        }
        UpdateHeader(root);                 // live elapsed every tick
        bool runningNow = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
        // Snapshot exposes actual browser tabs and the current live concurrency cap. This is
        // operational status, not another place to edit those settings.
        {
            int openTabs = I(root, "open_tabs");
            int liveCap = I(root, "max_concurrent");
            UpdateWorkerChip(openTabs, liveCap, runningNow);
        }
        // only archive while the run is LIVE -- otherwise the finished run's final
        // snapshot would re-add cleared tasks every tick (Clear would never stick).
        //
        // AND ONCE MORE WHEN IT STOPS, WHICH IS THE WHOLE BUG. `running` is false as soon as
        // every worker is terminal, so the sweep in which the LAST worker finishes is the
        // sweep that fails this test -- that worker was never terminal on a tick where this
        // line ran, and it never reaches History.
        //
        // MEASURED 2026-08-28 over the eight most recent runs: seven lost at least one
        // worker, the missing one was the highest-numbered in five, and a ONE-GOAL run
        // archived nothing at all -- 0 of 1, because its only worker finished on the sweep
        // that flipped running to false.
        //
        // Fired once per run, keyed on `started`, so the objection above still holds: a
        // finished run cannot re-add cleared cards every tick, because it archives on one
        // tick only. Independent of the auto-archive setting and of which cards are on
        // screen -- MaybeAutoArchive walks _toolbarShown, so with a tab filter active it
        // archives only what the filter left visible, and this walks the snapshot.
        if (runningNow) ArchiveTerminal(root);
        else ArchiveRunTailOnce(root);
        // opt-in auto-retry runs every tick (before the sig short-circuit) so it catches a
        // stopped goal even when nothing else changed. Bounded by _autoRetryMax per goal text.
        if (runningNow && _autoRetry) AutoRetryScan(root);
        // P2 (c) AUTO-ARCHIVE: when the RUN has finished (not running AND every worker terminal),
        // move its completed cards to History -- reusing the exact "すべて履歴へ" code path
        // (ArchiveAllTerminal). Fires ONCE per run, keyed on `started`, and only when enabled.
        if (_autoArchive && !runningNow) MaybeAutoArchive(root);
        // A2-2: spine refresh runs every tick (keyed off its own sig, cheap when unchanged).
        // _toolbarAll is populated by the last RenderCards/BuildRows; for the first tick it may
        // be empty, but RefreshSpine re-reads workers from root directly so that's fine.
        // PaintComposerMode: show "steer" surface while run is live.
        RefreshSpine(root, false);
        PaintComposerMode(runningNow);
        string sig = Sig(root) + "|s" + _submittedSig;
        if (sig == _lastSig) return;
        _lastSig = sig;
        RenderCards(root);
    }

    // P2 (c): if the run has finished and every on-board worker is terminal, archive them ALL to
    // History via the same path the "すべて履歴へ" button uses. Guarded to fire once per run by
    // the run's `started` identity (a new run resets it) so a finished snapshot re-read each tick
    // doesn't re-archive (and Clear can still stick).
    void MaybeAutoArchive(Dictionary<string, object> root)
    {
        if (root == null) return;
        string started = S(root, "started");
        if (string.IsNullOrEmpty(started)) return;
        if (_archivedRunStarted == started) return;            // already handled this run

        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        var wArr = (object[])wo;
        if (wArr.Length == 0) return;
        foreach (object o in wArr)
        {
            var w = o as Dictionary<string, object>;
            if (w == null) continue;
            if (IsInterruptedWorker(w)) continue;              // resumable: never archived, never blocks
            if (!IsTerminalWorker(w)) return;                  // not fully finished yet -> wait
        }
        // _toolbarShown is populated by the last RenderCards; on the finished tick it holds this
        // run's terminal workers. If it hasn't been built yet (empty), defer to a later tick rather
        // than marking the run handled with nothing archived.
        if (_toolbarShown == null || _toolbarShown.Count == 0) return;
        _archivedRunStarted = started;                         // mark BEFORE archiving (idempotent)
        ArchiveAllTerminal();                                   // reuse the exact bulk-archive path
    }

    // Opt-in, CAPPED auto-retry. For each STOPPED non-DONE goal, re-queue it at most
    // _autoRetryMax times (counted by goal text). The cap is the safety rail: a goal that
    // keeps failing stops being re-queued once it hits the budget -- never an infinite loop.
    // Only acts while a run is live (add_goal is consumed by the running fleet).
    void AutoRetryScan(Dictionary<string, object> root)
    {
        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        foreach (object o in (object[])wo)
        {
            var w = o as Dictionary<string, object>;
            if (w == null) continue;
            if (!IsTerminalWorker(w)) continue;
            // THE SAME CLOSED SET PYTHON USES, not the one outcome that came to mind.
            //
            // This skipped only DONE, so every other outcome was retryable here -- including
            // FANOUT, which relay/outcomes.py classifies as done and explicitly NOT
            // retryable. relay/fleet_runner.py says why that matters in as many words: two
            // retry policies that can disagree is worse than either one alone.
            //
            // What the disagreement cost, measured 2026-08-28 over 100 coordinator logs: a
            // fan-out parent ends terminal with outcome FANOUT, so this re-queued it. The
            // duplicate carries the SAME GOAL TEXT and a higher worker index, and the fleet
            // keys delivered results by goal text -- so the duplicate overwrote the parent
            // that the merged answer had just been written onto. Of 24 runs that split, 22
            // delivered a duplicate instead of the merge. One run split 8 goals into 63
            // workers, merged all 8, and delivered none of them.
            //
            // So the fan-out was not broken. It ran, it split, it merged, and then this
            // line threw the answer away.
            if (!IsRetryableWorker(w)) continue;
            string goal = S(w, "goal");
            if (string.IsNullOrEmpty(goal)) continue;
            int n = 0;
            if (_autoRetryCount.ContainsKey(goal)) n = _autoRetryCount[goal];
            if (n >= _autoRetryMax) continue;          // budget spent -> never loop
            // ONE RE-QUEUE PER WORKER. The terminal worker stays terminal, so without this every
            // tick re-queued the same STUCK worker until the per-goal budget ran out (2026-10-01:
            // two extra copies on top of the runner's own retry).
            if (RetryAlreadyCovered(root, w)) continue;
            _autoRetriedWorkers.Add(RetryWorkerKey(w));
            _autoRetryCount[goal] = n + 1;             // count BEFORE re-queue (idempotent per tick)
            RetryGoal(w);
        }
    }

    Dictionary<string, object> ReadStatus()
    {
        try
        {
            if (!File.Exists(_statusPath)) return null;
            string text;
            using (var fs = new FileStream(_statusPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fs, Encoding.UTF8))
                text = sr.ReadToEnd();
            if (string.IsNullOrEmpty(text)) return null;
            return (Dictionary<string, object>)_js.DeserializeObject(text);
        }
        catch (Exception ex) { NoteStatusReadFailure(ex); return null; }
    }

    bool _statusReadFailLogged;

    // A SWALLOWED EXCEPTION WITH NO TRACE IS WHY THE BUG ABOVE TOOK TWENTY HOURS TO FIND.
    // The catch itself has to stay -- a cockpit that dies on a half-written status.json is
    // worse than one that skips a tick -- but "returns null on any failure" and "logs
    // nothing on any failure" are separate decisions, and only the first one was wanted.
    // Recording the FIRST failure per process is enough: the failure is structural, so the
    // thousandth line would say what the first one says, and an unbounded log on a
    // per-tick path is its own disk problem. Size is recorded because it is the field that
    // identifies this failure at a glance.
    void NoteStatusReadFailure(Exception ex)
    {
        if (_statusReadFailLogged) return;
        _statusReadFailLogged = true;
        try
        {
            long size = -1;
            try { size = new FileInfo(_statusPath).Length; } catch (Exception) { }
            string line = "{\"ts\":" + DateTimeOffset.UtcNow.ToUnixTimeSeconds()
                        + ",\"what\":\"status_read_failed\""
                        + ",\"bytes\":" + size
                        + ",\"type\":\"" + ex.GetType().Name + "\""
                        + ",\"detail\":\"" + (ex.Message ?? "").Replace("\\", "/").Replace("\"", "'") + "\"}";
            string path = Path.Combine(Path.GetDirectoryName(ResolvePath(null)), "ui_errors.jsonl");
            File.AppendAllText(path, line + Environment.NewLine, NoBomUtf8);
        }
        catch (Exception) { }   // a trace that can break the cockpit is worse than no trace
    }

    void UpdateHeader(Dictionary<string, object> root)
    {
        int total = I(root, "total");
        int done = I(root, "done_count");
        bool running = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
        double started = Dbl(root, "started");
        double updated = Dbl(root, "updated");
        double elapsed = running ? (started > 0 ? NowUnix() - started : 0)
                                 : (root.ContainsKey("elapsed_s") ? Dbl(root, "elapsed_s")
                                    : (updated > 0 && started > 0 ? updated - started : 0));

        // Spec: header is always "Fleet" (product surface name, both languages).
        _header.Text = "Fleet";

        // Compute running/queued/done counts from the workers array.
        int cntRunning = 0, cntQueued = 0, cntDoneW = 0, cntIntr = 0;
        object wo2;
        if (root.TryGetValue("workers", out wo2) && wo2 is object[])
        {
            foreach (object ow in (object[])wo2)
            {
                var ww = ow as Dictionary<string, object>;
                if (ww == null) continue;
                string wst = S(ww, "status");
                if (IsTerminalWorker(ww)) cntDoneW++;
                else if (IsInterruptedWorker(ww)) cntIntr++;   // not running, not done, not a failure
                else if (wst == "pending") cntQueued++;
                else cntRunning++;
            }
        }

        // Sub: "N running · M queued · K done" triple + elapsed
        bool ja2 = _lang == 0;

        // TASK 5: run-ended state — when running==false AND every worker is terminal, show ended summary.
        bool allTerminal = false;
        int cntAttn = 0;
        object wo3;
        if (root.TryGetValue("workers", out wo3) && wo3 is object[])
        {
            var wArr = (object[])wo3;
            if (wArr.Length > 0)
            {
                allTerminal = true;
                foreach (object ow2 in wArr)
                {
                    var ww2 = ow2 as Dictionary<string, object>;
                    if (ww2 == null) continue;
                    if (!IsTerminalWorker(ww2) && !IsInterruptedWorker(ww2)) { allTerminal = false; }
                    if (IsOperatorAttention(ww2)) cntAttn++;
                }
            }
        }

        string triple;
        if (!running && allTerminal)
        {
            // Run-ended header: "{done} done · {attn} needs attention · run ended"
            if (ja2)
            {
                triple = cntDoneW + " 完了 · " + cntAttn + " 要対応 · " + (cntIntr > 0 ? cntIntr + " 中断 · " : "") + "終了";
            }
            else
            {
                triple = cntDoneW + " done · " + cntAttn + " needs attention · " + (cntIntr > 0 ? cntIntr + " interrupted · " : "") + "run ended";
            }
        }
        else
        {
            triple = ja2
                ? (cntRunning + " 実行中 · " + cntQueued + " 待機 · " + cntDoneW + " 完了")
                : (cntRunning + " running · " + cntQueued + " queued · " + cntDoneW + " done");
        }

        // Live ETA: project remaining work from the throughput observed SO FAR (done/elapsed).
        string eta = "";
        if (running && total > 0 && done < total)
        {
            if (done > 0 && elapsed > 1.0)
            {
                double etaS = (total - done) * (elapsed / done);
                double perH = done / elapsed * 3600.0;
                eta = "    " + T("eta") + " " + DateTime.Now.AddSeconds(etaS).ToString("HH:mm")
                      + " (" + T("eta_in") + " " + Fmt(etaS) + " · "
                      + perH.ToString("0.#") + " " + T("rate") + ")";
            }
            else
            {
                eta = "    " + T("eta") + " " + T("eta_calc");
            }
        }

        // Two-stage stale indicator: soft (45 s) shows a calm wait hint; hard (90 s) shows a stop warning.
        const double STALE_SOFT = 45.0;
        const double STALE_HARD = 90.0;
        if (running && updated > 0)
        {
            double staleAge = NowUnix() - updated;
            if (staleAge > STALE_HARD)
                triple = triple + " — " + T("stale");
            else if (staleAge > STALE_SOFT)
                triple = triple + " — " + T("stale_wait");
        }

        // TASK 4: evidence freshness — "· evidence {N}s/m ago" from top-level `updated` field [COMPUTED].
        // Omit entirely if updated is missing (zero) — never show "unknown".
        string freshness = "";
        if (updated > 0)
        {
            double ageS = NowUnix() - updated;
            if (ageS >= 0)
            {
                string ageFmt;
                if (ageS < 60.0)
                    ageFmt = ((int)ageS).ToString() + (ja2 ? "秒" : "s");
                else
                    ageFmt = ((int)(ageS / 60.0)).ToString() + (ja2 ? "分" : "m");
                freshness = " · " + (ja2 ? "最終更新 " : "updated ") + ageFmt + (ja2 ? "前" : " ago");
            }
        }

        _sub.Text = triple + freshness + "    " + T("elapsed") + " " + Fmt(elapsed) + eta;
        _sub.ToolTip = _sub.Text;   // _sub itself is Collapsed (see BuildChrome) -- kept alive purely
                                    // as the plain-text fallback for any future consumer of its Text/
                                    // ToolTip; nothing else in this file reads them today (audited).

        // Feature 2: the single concatenated sentence above is what actually got read as a
        // fallback; what's actually DISPLAYED is a WrapPanel of discrete Pill() chips (same helper
        // HistoryRow etc. already use), one per fact, rebuilt from the SAME source variables (not
        // parsed back out of the sentence) so wording/pluralization can't drift between the two.
        if (_subChips != null)
        {
            _subChips.Children.Clear();
            if (!running && allTerminal)
            {
                _subChips.Children.Add(ChipMargin(Pill(cntDoneW + " " + (ja2 ? "完了" : "done"), "success")));
                _subChips.Children.Add(ChipMargin(Pill(cntAttn + " " + (ja2 ? "要対応" : "needs attention"),
                    cntAttn > 0 ? "warning" : "neutral")));
                if (cntIntr > 0)
                    _subChips.Children.Add(ChipMargin(Pill(cntIntr + " " + (ja2 ? "中断" : "interrupted"), "warning")));
                _subChips.Children.Add(ChipMargin(Pill(ja2 ? "終了" : "run ended", "neutral")));
            }
            else
            {
                _subChips.Children.Add(ChipMargin(Pill(cntRunning + " " + (ja2 ? "実行中" : "running"), "info")));
                _subChips.Children.Add(ChipMargin(Pill(cntQueued + " " + (ja2 ? "待機" : "queued"), "neutral")));
                _subChips.Children.Add(ChipMargin(Pill(cntDoneW + " " + (ja2 ? "完了" : "done"), "success")));
            }
            if (running && updated > 0)
            {
                double staleAge2 = NowUnix() - updated;
                if (staleAge2 > STALE_HARD)
                    _subChips.Children.Add(ChipMargin(Pill(T("stale"), "danger")));
                else if (staleAge2 > STALE_SOFT)
                    _subChips.Children.Add(ChipMargin(Pill(T("stale_wait"), "warning")));
            }
            // NOT CHIPS. A chip in this app means a state something is IN -- running, done,
            // needs attention -- and these three are clocks. Drawn in the same outlined pill as
            // the counts they were indistinguishable from them, so "0 needs attention" and
            // "elapsed 8m16s" arrived with equal weight and the row read as six alarms. They are
            // background facts; background facts are text.
            var meta = new List<string>();
            if (!string.IsNullOrEmpty(freshness)) meta.Add(freshness.TrimStart(' ', '·'));
            meta.Add(T("elapsed") + " " + Fmt(elapsed));
            if (!string.IsNullOrEmpty(eta)) meta.Add(eta.Trim());
            var metaTb = new TextBlock();
            metaTb.Text = string.Join("  ·  ", meta.ToArray());
            metaTb.Foreground = Theme.Br(Theme.Faint(_dark));
            metaTb.FontSize = 11;
            metaTb.VerticalAlignment = VerticalAlignment.Center;
            metaTb.Margin = new Thickness(4, 0, 0, 0);
            _subChips.Children.Add(metaTb);
            // THE THIRD GATE, ON THE ROW WHERE THE OTHER FACTS ALREADY LIVE. Disk and RAM have
            // had gauges for months. The limit that actually stopped a run -- 217 turns refused
            // out of 237 -- had none, so a fleet grinding to a halt looked like a fleet being
            // slow and the stop had no explanation anyone could point at.
            var rate = BuildRateStrip(root, ja2);
            if (rate != null) _subChips.Children.Add(rate);
            _subChips.ToolTip = _sub.Text;   // same fallback sentence, shown on hover over the chip row
        }
    }



    // ── Graduated severity colour ─────────────────────────────────────────────────────────
    //
    // WHY A GRADIENT AND NOT THREE BUCKETS. The first version of the rate strip used discrete
    // zones -- green under 60%, amber to 85%, red above -- which tells you WHICH BUCKET you are
    // in and not HOW CLOSE you are. 61% and 84% drew the same colour, so the whole span where
    // an operator could still act looked identical.
    //
    // The four anchors are the Atlassian Statuspage / status.claude.com uptime-bar ramp, taken
    // from that bar's own SVG rather than invented:
    //     green #76AD2A -> yellow #FAA72A -> orange #E86235 -> red #E04343
    // Interpolating a continuous severity across them means "degraded but nearly critical"
    // has its own colour, which is exactly the state worth seeing early.
    static readonly double[] _SEV_STOPS = { 0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0 };
    static readonly byte[][] _SEV_RGB = {
        new byte[] { 0x76, 0xAD, 0x2A },
        new byte[] { 0xFA, 0xA7, 0x2A },
        new byte[] { 0xE8, 0x62, 0x35 },
        new byte[] { 0xE0, 0x43, 0x43 },
    };

    // severity [0,1] -> colour, linearly interpolated between the anchors.
    static Color SeverityColor(double sev)
    {
        if (sev <= 0.0) return Color.FromRgb(_SEV_RGB[0][0], _SEV_RGB[0][1], _SEV_RGB[0][2]);
        if (sev >= 1.0) return Color.FromRgb(_SEV_RGB[3][0], _SEV_RGB[3][1], _SEV_RGB[3][2]);
        for (int i = 0; i < _SEV_STOPS.Length - 1; i++)
        {
            double s0 = _SEV_STOPS[i], s1 = _SEV_STOPS[i + 1];
            if (sev <= s1)
            {
                double t = (s1 > s0) ? (sev - s0) / (s1 - s0) : 0.0;
                byte[] c0 = _SEV_RGB[i], c1 = _SEV_RGB[i + 1];
                return Color.FromRgb(
                    (byte)Math.Round(c0[0] + (c1[0] - c0[0]) * t),
                    (byte)Math.Round(c0[1] + (c1[1] - c0[1]) * t),
                    (byte)Math.Round(c0[2] + (c1[2] - c0[2]) * t));
            }
        }
        return Color.FromRgb(_SEV_RGB[3][0], _SEV_RGB[3][1], _SEV_RGB[3][2]);
    }

    // A raw metric -> severity, piecewise linear through the same three thresholds the sibling
    // status page uses: at or below `green` it is 0; `deg` lands on the yellow anchor; `crit`
    // and beyond is 1. The deg..crit leg ramps continuously so approaching the ceiling darkens
    // steadily instead of flipping at a boundary.
    static double PiecewiseSeverity(double x, double green, double deg, double crit)
    {
        const double Y = 1.0 / 3.0;
        if (x <= green) return 0.0;
        if (x <= deg) return (deg > green) ? Y * (x - green) / (deg - green) : Y;
        if (x <= crit) return (crit > deg) ? Y + (1.0 - Y) * (x - deg) / (crit - deg) : 1.0;
        return 1.0;
    }

    // The rate gauge's own mapping. Half the published ceiling is unambiguously fine; from
    // there it ramps, and 100% of the published figure is full red -- not because the run
    // stops there, but because that is the last point at which the number still means anything.
    static double RateSeverity(double pct) { return PiecewiseSeverity(pct, 50.0, 75.0, 100.0); }

    // The turn meter, read straight off disk so the gauge does not depend on a fleet running.
    // JSONL, one record per turn or refusal; bucketed here into the same shape fleet_runner
    // publishes so the strip has one code path whichever source it came from. Never throws --
    // a gauge that can take down the header is worse than no gauge.
    Dictionary<string, object> ReadQuotaMeter()
    {
        try
        {
            // Derived from the status path we already hold. The first version named a
            // _fleetDir field that does not exist -- the third invented identifier tonight,
            // and the third caught by the compiler rather than by review.
            string path = Path.Combine(Path.GetDirectoryName(_statusPath), "quota_meter.jsonl");
            if (!File.Exists(path)) return null;
            double now = NowUnix();
            var minutes = new int[60];
            int rpm = 0, rph = 0, refusals = 0;
            bool any = false;
            foreach (string line in File.ReadAllLines(path, Encoding.UTF8))
            {
                string t = line.Trim();
                if (t.Length == 0) continue;
                Dictionary<string, object> row = null;
                try { row = _js.DeserializeObject(t) as Dictionary<string, object>; } catch { }
                if (row == null) continue;
                double ts = 0;
                object tsRaw;
                if (row.TryGetValue("ts", out tsRaw) && tsRaw != null)
                { try { ts = Convert.ToDouble(tsRaw); } catch { } }
                double age = now - ts;
                if (age < 0 || age >= 3600) continue;
                string ev = S(row, "event");
                if (ev == "turn")
                {
                    any = true; rph++;
                    if (age < 60) rpm++;
                    int idx = 59 - (int)(age / 60.0);
                    if (idx >= 0 && idx < 60) minutes[idx]++;
                }
                else if (ev == "refusal" && age < 300)
                {
                    any = true; refusals++;
                }
            }
            if (!any) return null;
            // The USER's ceiling, not the published one. A strip drawn against 100 while
            // admission holds at 40 would show comfortable headroom at the exact moment the
            // fleet stopped admitting -- the gauge would be explaining the opposite of what
            // was happening.
            double limitRpm = _rateCeiling > 0 ? (double)_rateCeiling : 100.0;
            var series = new object[60];
            for (int i = 0; i < 60; i++) series[i] = minutes[i];
            var q = new Dictionary<string, object>();
            q["rpm"] = rpm; q["rph"] = rph;
            q["limit_rpm"] = limitRpm;
            q["pct_rpm"] = 100.0 * rpm / limitRpm;
            q["refusals_5m"] = refusals;
            q["series_rpm"] = series;
            q["measured"] = true;
            return q;
        }
        catch { return null; }
    }

    // ── The Copilot rate-limit strip ──────────────────────────────────────────────────────
    //
    // WHY IT EXISTS. A dense run had 217 of its 237 turns refused and nothing on this screen
    // said so. Disk and RAM gates have been drawn for months; the quota that actually stopped
    // the run was invisible, so "the fleet is slow" and "the fleet is being refused" looked
    // identical to whoever was watching. This sits on the header row beside the counts and the
    // clocks, because that is where someone already looks to ask how the run is going.
    //
    // WHAT IT DRAWS, AND THE UNIT MATTERS. Turns per minute -- generative messages, which is
    // what Microsoft publishes a limit for. NOT the MCP tool calls our own gateway sees: a turn
    // may make none of those or several, and measuring the number we happened to have is how
    // the first headroom estimate came out wrong by an unknown factor.
    //
    // THE LINE IS A REFERENCE, NOT A PROMISE. 100/minute is the documented Microsoft 365
    // Copilot row, scoped per Dataverse environment, and Microsoft says downstream services may
    // impose lower ones. Refusals are therefore shown BESIDE the bar and never derived from it:
    // refusals arriving while the bar looks comfortable mean the line is the wrong line, which
    // is a finding rather than a contradiction the panel should hide.
    UIElement BuildRateStrip(Dictionary<string, object> root, bool ja)
    {
        // READ THE METER DIRECTLY, and fall back to whatever the run put in status.json.
        //
        // The first version only read status.json's "quota", which meant the gauge appeared
        // solely while a fleet was running and writing that file -- and vanished the moment it
        // was most wanted. The rate limit is a property of the TENANT, not of one run: "am I
        // still being refused, or has it recovered" is a question asked with nothing running.
        // It also meant a status.json written before this key existed showed no gauge at all,
        // which is exactly what happened on the first build.
        var q = ReadQuotaMeter();
        if (q == null && root != null)
        {
            object qRaw;
            if (root.TryGetValue("quota", out qRaw)) q = qRaw as Dictionary<string, object>;
        }
        if (q == null) return null;

        int rpm = I(q, "rpm");
        int rph = I(q, "rph");
        int refusals = I(q, "refusals_5m");
        double limitRpm = _rateCeiling > 0 ? (double)_rateCeiling : 100.0;
        double pct = 0.0;
        try { limitRpm = Convert.ToDouble(q["limit_rpm"]); } catch { }
        // The setting wins over whatever the writer stamped: fleet_runner records limit_rpm
        // from the module default, and the operator may have set a lower line since.
        if (_rateCeiling > 0) limitRpm = _rateCeiling;
        pct = 100.0 * rpm / Math.Max(1.0, limitRpm);
        // pct is derived from the ceiling above, NOT read back from the file: the writer
        // computed its percentage against the default limit and using it here would draw the
        // bar against one line while the number beside it counts against another.
        bool measured = false;
        object mRaw;
        if (q.TryGetValue("measured", out mRaw) && mRaw != null)
        { try { measured = Convert.ToBoolean(mRaw); } catch { } }

        var row = new StackPanel();
        row.Orientation = Orientation.Horizontal;
        row.VerticalAlignment = VerticalAlignment.Center;
        row.Margin = new Thickness(20, 0, 0, 0);

        var lbl = new TextBlock();
        lbl.Text = ja ? "レート" : "rate";
        lbl.Foreground = Theme.Br(Theme.Faint(_dark));
        lbl.FontSize = 11;
        lbl.VerticalAlignment = VerticalAlignment.Center;
        lbl.Margin = new Thickness(0, 0, 6, 0);
        row.Children.Add(lbl);

        // The reading. "--" when nothing has been measured: an empty meter and a quiet one look
        // identical as a number, and only one of them means there is headroom.
        var val = new TextBlock();
        val.FontSize = 11.5;
        val.FontWeight = FontWeights.SemiBold;
        val.Foreground = Theme.Br(Theme.Text(_dark));
        val.VerticalAlignment = VerticalAlignment.Center;
        val.Text = measured ? (rpm + "/" + ((int)limitRpm)) : "--";
        row.Children.Add(val);

        var per = new TextBlock();
        per.Text = ja ? " /分" : " /min";
        per.Foreground = Theme.Br(Theme.Faint(_dark));
        per.FontSize = 10;
        per.VerticalAlignment = VerticalAlignment.Center;
        per.Margin = new Thickness(0, 0, 8, 0);
        row.Children.Add(per);

        // A short bar so the proportion reads without arithmetic.
        var track = new Border();
        track.Width = 54; track.Height = 5;
        track.CornerRadius = new CornerRadius(2.5);
        track.Background = Theme.Br(Theme.Border(_dark));
        track.VerticalAlignment = VerticalAlignment.Center;
        track.Margin = new Thickness(0, 0, 8, 0);
        var holder = new Grid();
        holder.HorizontalAlignment = HorizontalAlignment.Left;
        var fill = new Border();
        fill.Height = 5;
        fill.CornerRadius = new CornerRadius(2.5);
        fill.HorizontalAlignment = HorizontalAlignment.Left;
        fill.Background = new SolidColorBrush(SeverityColor(RateSeverity(pct)));
        fill.Width = measured ? Math.Max(1.5, Math.Min(54.0, 54.0 * pct / 100.0)) : 0.0;
        holder.Children.Add(fill);
        track.Child = holder;
        row.Children.Add(track);

        // THE LAST HOUR, so a BURST is visible. A single number cannot show one, and a burst is
        // exactly what trips a per-minute limit while the hourly total still looks fine.
        object sRaw;
        if (q.TryGetValue("series_rpm", out sRaw) && sRaw is object[])
        {
            object[] series = (object[])sRaw;
            int peak = 1;
            for (int i = 0; i < series.Length; i++)
            {
                int v = 0;
                try { v = Convert.ToInt32(series[i]); } catch { }
                if (v > peak) peak = v;
            }
            double scale = Math.Max((double)peak, limitRpm);
            var spark = new StackPanel();
            spark.Orientation = Orientation.Horizontal;
            spark.Height = 14;
            spark.VerticalAlignment = VerticalAlignment.Center;
            spark.Margin = new Thickness(0, 0, 8, 0);
            for (int i = 0; i < series.Length; i++)
            {
                int v = 0;
                try { v = Convert.ToInt32(series[i]); } catch { }
                var bar = new Border();
                bar.Width = 1.6;
                bar.Margin = new Thickness(0, 0, 0.6, 0);
                bar.VerticalAlignment = VerticalAlignment.Bottom;
                bar.Height = (v <= 0) ? 1.0 : Math.Max(1.5, 14.0 * v / scale);
                // EACH MINUTE KEEPS ITS OWN COLOUR. A burst twenty minutes ago stays red in
                // the history even though the current reading is green -- which is the whole
                // reason to draw an hour instead of a number.
                bar.Background = (v <= 0)
                    ? Theme.Br(Theme.Border(_dark))
                    : new SolidColorBrush(SeverityColor(RateSeverity(100.0 * v / Math.Max(1.0, limitRpm))));
                spark.Children.Add(bar);
            }
            row.Children.Add(spark);
        }

        // REFUSALS, BESIDE THE BAR AND NEVER DERIVED FROM IT. This is the line that explains a
        // stop: if it is non-zero the fleet is being refused, whatever the bar says.
        if (refusals > 0)
        {
            var warn = new TextBlock();
            warn.Text = ja ? ("拒否 " + refusals + " /5分") : (refusals + " refused /5m");
            warn.Foreground = new SolidColorBrush(SeverityColor(1.0));
            warn.FontSize = 11;
            warn.FontWeight = FontWeights.SemiBold;
            warn.VerticalAlignment = VerticalAlignment.Center;
            row.Children.Add(warn);
        }

        string tip = measured
            ? (ja
               ? ("生成メッセージ " + rpm + " /分（公開上限 " + ((int)limitRpm) + "）、直近1時間 " + rph + " 件。\n"
                  + "上限は Dataverse 環境単位の公開値で、下位サービスがより低い制限を課すことがあります。\n"
                  + "余裕があるのに拒否が出ている場合、効いている制限は公開値ではありません。")
               : ("generative messages " + rpm + "/min (published limit " + ((int)limitRpm) + "), "
                  + rph + " in the last hour.\nThe limit is the published per-Dataverse-environment "
                  + "figure; downstream services may impose lower ones.\nRefusals with headroom to "
                  + "spare mean the binding limit is not the published one."))
            : (ja ? "まだ1ターンも計測していません。" : "no turns measured yet.");
        row.ToolTip = tip;
        return row;
    }

    // Roll seconds up through y/d/h/m/s so a long run reads "1h39m27s" / "1d1h1m1s" instead of
    // an unbounded "99m27s". Show every unit from the highest non-zero down to seconds (so
    // ordering is unambiguous). Month is intentionally omitted: its token would clash with
    // minutes ('m') and its length is variable; a 365-day year is a good-enough cap for run time.
    static string Fmt(double sec)
    {
        if (sec < 0) sec = 0;
        long s = (long)sec;
        long y = s / 31536000; s -= y * 31536000;
        long d = s / 86400;    s -= d * 86400;
        long h = s / 3600;     s -= h * 3600;
        long m = s / 60;       s -= m * 60;
        var sb = new StringBuilder();
        if (y > 0)                  sb.Append(y).Append('y');
        if (sb.Length > 0 || d > 0) sb.Append(d).Append('d');
        if (sb.Length > 0 || h > 0) sb.Append(h).Append('h');
        if (sb.Length > 0 || m > 0) sb.Append(m).Append('m');
        sb.Append(s).Append('s');
        return sb.ToString();
    }

    void RenderCards(Dictionary<string, object> root)
    {
        _lastRoot = root;               // cache for single-card toggles
        PaintEffortPolicyInEffect(root);   // what the runner reports is in effect (effort policy)
        PaintFanoutInEffect(root);         // what the coordinator was started with (fan-out)
        PaintFanoutBudgetInEffect(root);   // the per-tree limits the coordinator applies
        PaintFanoutDepthInEffect(root);    // the split depth the coordinator applies
        PaintHierarchicalMergeInEffect(root);   // the hierarchical-merge state the coordinator applies
        PaintMergeConversationInEffect(root);   // the merge-conversation state and the savings so far
        PaintWriteScopeInEffect(root);     // the sibling write-scope mode the coordinator applies
        PaintAutoResumeInEffect(root);     // the auto-resume setting and the gate's last decision
        PaintSupervisorCodeNote();         // "supervisor is running older code" (supervisor_state.json)
        PaintToolProbeInEffect();          // why the tool-call check did or did not send a message
        // Preserve scroll position across the rebuild. Without this, every worker update
        // (status/turn change) reset the list and snapped the view back to the TOP -- which is
        // exactly why scrolling "didn't work" while tasks were live: the user scrolled down, a
        // tick fired, and they were yanked up again. Capture now, restore after the new content
        // is laid out (read the ListBox's internal ScrollViewer, which #ListScroller finds).
        ScrollViewer sc = ListScroller();
        double off = (sc != null) ? sc.VerticalOffset : 0.0;

        SetRows(BuildRows(root));
        RefreshPinnedToolbar();     // rebuild the pinned filter bar (only when its signature changed)

        if (off > 0.0)
        {
            double target = off;
            Dispatcher.BeginInvoke(new Action(delegate
            {
                ScrollViewer s2 = ListScroller();
                if (s2 != null) s2.ScrollToVerticalOffset(target);
            }), System.Windows.Threading.DispatcherPriority.Loaded);
        }

        // Feature 1 keystroke-safety: mirrors the scroll-restore pass just above, same Loaded
        // priority (i.e. runs after this sweep's layout has realized whatever containers SetRows
        // just replaced). If the user was mid-type in some worker's steer box, its card may have
        // just been rebuilt (RowSig changed for reasons unrelated to steering -- a LIVE worker's
        // `last`/`turn` streams every sweep) into a BRAND NEW TextBox instance. The new instance
        // already has the right text (CollapsedSteerRow/SteerRow seed it from _steerDraft on every
        // build) -- what it does NOT have yet is focus, so re-apply it here and seat the caret at
        // the end. Runs unconditionally (not gated on scroll offset) since a steer rebuild can
        // happen with zero scroll.
        string focusWorker = _steerFocusWorker;
        if (!string.IsNullOrEmpty(focusWorker))
        {
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (_steerFocusWorker != focusWorker) return;   // focus moved on before this ran
                TextBox tb2;
                if (_steerBoxRef.TryGetValue(focusWorker, out tb2) && tb2 != null)
                {
                    try { tb2.Focus(); tb2.CaretIndex = tb2.Text != null ? tb2.Text.Length : 0; }
                    catch (Exception) { }
                }
            }), System.Windows.Threading.DispatcherPriority.Loaded);
        }
    }

    // Rebuild the PINNED filter bar into its fixed host above the list. Called after BuildRows (so
    // _toolbarAll/_toolbarShown are current). Uses the same signature the toolbar row used (case 0
    // in RowSig) so the heavy Border is only re-created when a count/filter/theme actually changes —
    // otherwise we leave the existing element in place (no flicker, no needless layout). The bar is
    // hidden in the empty state (no workers AND no history), matching the old behavior where the
    // toolbar row was simply not emitted.
    void RefreshPinnedToolbar()
    {
        if (_pinnedToolbarHost == null) return;
        bool empty = (_toolbarAll == null || _toolbarAll.Count == 0) && (_history == null || _history.Count == 0);
        if (empty)
        {
            _pinnedToolbarHost.Visibility = Visibility.Collapsed;
            _pinnedToolbarHost.Child = null;
            _pinnedToolbarSig = "";
            return;
        }
        // Signature identical to RowSig(case 0) so we rebuild on exactly the same triggers.
        int[] tc = ToolbarCounts(_toolbarAll);
        string g = (_dark ? "D" : "L") + _lang.ToString();
        string sig = "T|" + g + "|" + _toolbarShown.Count + "/" + _toolbarAll.Count
                     + "|all" + tc[0] + ":act" + tc[1] + ":need" + tc[2] + ":done" + tc[3]
                     + ":max" + tc[5] + ":bad" + tc[6] + ":hid" + tc[7] + ":int" + tc[8]
                     + "|ar" + (_autoRetry ? 1 : 0) + ":" + _autoRetryMax + "|f" + _cardFilter;
        _pinnedToolbarHost.Visibility = Visibility.Visible;
        if (sig == _pinnedToolbarSig && _pinnedToolbarHost.Child != null) return;   // unchanged -> keep as-is
        _pinnedToolbarSig = sig;
        _pinnedToolbarHost.Child = BuildCardToolbar(_toolbarAll, _toolbarShown);
    }

    // Build the lightweight row-model list for the virtualizing ListBox: a toolbar row, one card
    // row per filtered/sorted worker, then (if history non-empty) a history-header row + one
    // history row per entry. The heavy UIElements are NOT built here -- the ItemTemplate's
    // converter builds them lazily for visible rows only.
    List<object> BuildRows(Dictionary<string, object> root)
    {
        // gather workers in natural order
        var workers = new List<Dictionary<string, object>>();
        object wo;
        if (root.TryGetValue("workers", out wo) && wo is object[])
            foreach (object o in (object[])wo)
                workers.Add((Dictionary<string, object>)o);

        // Feature B: apply the view filter. Outcome is null/"" while running, so only terminal
        // workers ever have outcome=="DONE"; filter 1 hides DONE (keeps failures + running),
        // filter 2 shows only DONE.
        // Persistent hide: skip TERMINAL cards the user cleared during a live run (their key is
        // in _hiddenKeys). Non-terminal workers are never hidden, and a fresh run gets new keys
        // (started changes), so this only suppresses exactly the cards the user dismissed.
        string startedRoot = S(root, "started");
        var shown = new List<Dictionary<string, object>>();
        foreach (Dictionary<string, object> w in workers)
        {
            if (_hiddenKeys.Count > 0 && IsTerminalWorker(w) && _hiddenKeys.Contains(WorkerKey(startedRoot, w)))
                continue;
            string oc = S(w, "outcome");
            string st = S(w, "status");
            // Tab 1 = Active: non-terminal AND not pending (actively working statuses)
            if (_cardFilter == 1 && (IsTerminalWorker(w) || IsInterruptedWorker(w) || st == "pending")) continue;
            // Tab 2 = Needs input: awaiting only
            if (_cardFilter == 2 && st != "awaiting") continue;
            // Tab 3 = Done: outcome == DONE
            if (_cardFilter == 3 && oc != "DONE") continue;
            // Tab 4 = Interrupted (coordinator died, resumable); the tab only exists while one is on the board
            if (_cardFilter == 4 && !IsInterruptedWorker(w)) continue;
            shown.Add(w);
        }
        // THE LIST'S ORDER, decided in one place (SubmittedTasks.Compose, executed by
        // ui/test_a_submitted_task_is_on_top_at_once.py): the "submitted, not picked up yet"
        // group first, then fresh pending workers (the newest tasks), active, older pending,
        // terminal. The submitted group is shown on the All and Active tabs -- a task that was
        // just sent is new work, not an approval request or a finished result.
        List<SubmittedView> subs = (_cardFilter == 0 || _cardFilter == 1)
            ? ReadQueuedJobs() : new List<SubmittedView>();
        List<SubmittedDisplayItem> order = SubmittedTasks.Compose(subs, shown, NowUnix(),
                                                                  IsTerminalForOrder);
        shown = new List<Dictionary<string, object>>();
        foreach (SubmittedDisplayItem it in order)
            if (it.Worker != null) shown.Add(it.Worker);

        // stash for the converter (it rebuilds the toolbar row from these when a container recycles)
        _toolbarAll = workers;
        _toolbarShown = shown;

        var rows = new List<object>();
        // Empty state (spec): no run yet and no history -> a calm centered suggestion block instead
        // of a blank workspace (the big top textarea is already gone -- it's the bottom composer now).
        // Anything submitted still goes ABOVE it.
        if (workers.Count == 0 && _history.Count == 0)
        {
            AppendSubmittedRows(rows, order);
            rows.Add(MkRow(5, null, null));
            return rows;
        }
        // The submitted group: the first rows of the list, above the directive band.
        AppendSubmittedRows(rows, order);
        // NOTE: the toolbar (すべて/実行中/承認待ち/完了 filter) is NO LONGER a scrolling row. It is
        // pinned in _pinnedToolbarHost above the list (see RefreshPinnedToolbar, called from
        // RenderCards). Row kind 0 is retained in the converter for safety but never emitted here.

        // TASK 3: Directive band — show only while there is at least one ON-BOARD worker (i.e. not
        // every card has been sent to History). Once the whole run is cleared to History the band
        // disappears (user request 2026-06-28). On-board = workers minus _hiddenKeys (History);
        // NOT filtered by the active tab, so the band is independent of which tab is selected.
        var onBoard = new List<Dictionary<string, object>>();
        foreach (Dictionary<string, object> ow in workers)
        {
            if (_hiddenKeys.Count > 0 && IsTerminalWorker(ow) && _hiddenKeys.Contains(WorkerKey(startedRoot, ow)))
                continue;   // in History -> not on the board
            onBoard.Add(ow);
        }
        // Compute _directiveBandMeta here so the Sig captures the live elapsed/lane counts.
        if (onBoard.Count > 0)
        {
            // Compute meta: "started HH:MM · {elapsed} · {active}/{total} lanes active" [COMPUTED]
            double dbStarted = Dbl(root, "started");
            double dbNow = NowUnix();
            bool dbRunning = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
            double dbElapsed = dbRunning
                ? (dbStarted > 0 ? dbNow - dbStarted : 0)
                : (root.ContainsKey("elapsed_s") ? Dbl(root, "elapsed_s")
                   : (Dbl(root, "updated") > 0 && dbStarted > 0 ? Dbl(root, "updated") - dbStarted : 0));
            int dbActive = 0;
            foreach (Dictionary<string, object> dw in onBoard)
                if (!IsTerminalWorker(dw) && !IsInterruptedWorker(dw) && S(dw, "status") != "pending") dbActive++;
            bool dbJa = _lang == 0;
            var dbMeta = new StringBuilder();
            if (dbStarted > 0)
            {
                string startHM = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)
                    .AddSeconds(dbStarted).ToLocalTime().ToString("HH:mm");
                dbMeta.Append(dbJa ? "開始 " : "started ").Append(startHM);
                dbMeta.Append(" · ").Append(Fmt(dbElapsed));
            }
            if (dbElapsed > 0 || dbStarted == 0)
            {
                if (dbMeta.Length > 0) dbMeta.Append(" · ");
                if (!dbRunning && dbActive == 0)
                {
                    // Run is done: show completed count instead of active/total lane status
                    int dbDone = 0;
                    foreach (Dictionary<string, object> dw2 in onBoard)
                        if (S(dw2, "outcome") == "DONE") dbDone++;
                    dbMeta.Append(dbDone);
                    dbMeta.Append(dbJa ? "件完了" : " done");
                }
                else
                {
                    dbMeta.Append(dbActive).Append("/").Append(onBoard.Count);
                    dbMeta.Append(dbJa ? " lane 稼働中" : " lanes active");
                }
            }
            _directiveBandMeta = dbMeta.ToString();

            // DirectiveBand aggregates goals from the on-board set (so lanes moved to History also
            // drop out of the "Goals (N)" count), not the full _toolbarAll.
            _directiveBandWorkers = onBoard;
            rows.Add(MkRow(6, onBoard[0], null));      // directive band: first on-board worker's goal [REAL]
        }
        // Tab 0 (All): partition active/queued vs terminal with a divider.
        // Tabs 1-3 are explicit single-criterion views -- no re-partition needed.
        if (_cardFilter == 0)
        {
            var active = new List<Dictionary<string, object>>();
            var done = new List<Dictionary<string, object>>();
            foreach (Dictionary<string, object> w in shown)
                (IsTerminalWorker(w) ? done : active).Add(w);
            foreach (Dictionary<string, object> w in active)
                rows.Add(MkRow(1, w, null));
            if (done.Count > 0)
            {
                rows.Add(MkRow(4, null, null));       // "完了 (this run)" divider
                foreach (Dictionary<string, object> w in done)
                    rows.Add(MkRow(1, w, null));
            }
        }
        else
        {
            foreach (Dictionary<string, object> w in shown)
                rows.Add(MkRow(1, w, null));          // one card per worker
        }
        // ③ Dedup: a terminal worker is archived into history the moment it finishes, but its card
        // also stays ON-BOARD (in `完了`) until the user moves it to History. During that window the
        // same started#name shows in BOTH places. Compute the on-board key set and skip history rows
        // that match it -- so a finished task appears once (on board) and only drops into History
        // once it actually leaves the board (すべて履歴へ / →履歴).
        var onBoardKeys = new System.Collections.Generic.HashSet<string>();
        foreach (Dictionary<string, object> bw in onBoard)
            onBoardKeys.Add(WorkerKey(startedRoot, bw));
        AppendHistoryRows(rows, onBoardKeys);
        return rows;
    }

    // Append a history-header row + one history row per entry (newest first) to the row model.
    void AppendHistoryRows(List<object> rows, System.Collections.Generic.HashSet<string> onBoardKeys)
    {
        if (_history.Count == 0) return;
        // ③ Build the visible (deduped) entry list first: skip any history entry whose key
        // (started#name) is still ON-BOARD, so the same finished task is not shown twice.
        var visible = new List<Dictionary<string, object>>();
        for (int i = _history.Count - 1; i >= 0; i--)
        {
            var e = _history[i] as Dictionary<string, object>;
            if (e == null) continue;
            string ek = S(e, "key");
            if (onBoardKeys != null && !string.IsNullOrEmpty(ek) && onBoardKeys.Contains(ek))
                continue;   // still on the board -> don't duplicate it in History
            visible.Add(e);
        }
        if (visible.Count == 0) return;               // everything is still on-board -> no History section yet
        rows.Add(MkRow(2, null, null));               // history header (carries the search box)

        // P2 (b) SEARCH: case-insensitive substring filter over title + result/goal text.
        string q = (_histQuery ?? "").Trim();
        List<Dictionary<string, object>> filtered = visible;
        if (q.Length > 0)
        {
            string ql = q.ToLowerInvariant();
            filtered = new List<Dictionary<string, object>>();
            foreach (Dictionary<string, object> e in visible)
            {
                string hay = ((string.IsNullOrEmpty(S(e, "goal_summary"))
                    ? CardTitle(S(e, "conv_title"), S(e, "goal")) : S(e, "goal_summary")) + " "
                              + S(e, "goal") + " " + S(e, "display_result") + " "
                              + S(e, "last") + " " + S(e, "outcome")).ToLowerInvariant();
                if (hay.IndexOf(ql, StringComparison.Ordinal) >= 0) filtered.Add(e);
            }
        }
        if (filtered.Count == 0) return;              // header still shown so the search box stays reachable

        // P2 (a) DATE GROUPS: emit a subheader (今日 / 昨日 / YYYY-MM-DD / その他) whenever the day
        // bucket changes. `visible` is already newest-first, so groups come out most-recent-first.
        string lastGroup = null;
        foreach (Dictionary<string, object> e in filtered)
        {
            string grp = HistoryGroupLabel(e);
            if (grp != lastGroup)
            {
                var gd = new Dictionary<string, object>();
                gd["label"] = grp;
                rows.Add(MkRow(7, null, gd));         // date-group subheader
                lastGroup = grp;
            }
            rows.Add(MkRow(3, null, e));             // one history row per visible entry
        }
    }

    // The date-group bucket label for a history entry, derived from its `ts` (archived-at unix
    // seconds). Entries archived before `ts` existed (old history) have no timestamp -> その他 /
    // Earlier. Today / Yesterday are relative to local midnight; older days show YYYY-MM-DD.
    string HistoryGroupLabel(Dictionary<string, object> e)
    {
        double ts = Dbl(e, "ts");
        if (ts <= 0) return T("hist_earlier");
        DateTime day;
        try { day = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(ts).ToLocalTime().Date; }
        catch (Exception) { return T("hist_earlier"); }
        DateTime today = DateTime.Now.Date;
        if (day == today) return T("hist_today");
        if (day == today.AddDays(-1)) return T("hist_yesterday");
        return day.ToString("yyyy-MM-dd");
    }

    // The date-group subheader UIElement (Kind==7). Small muted caption, matching the History chrome.
    UIElement HistoryGroupHeader(string label)
    {
        return new TextBlock {
            Text = label, Foreground = Muted, FontSize = 11.5, FontWeight = FontWeights.SemiBold,
            Margin = new Thickness(12, 12, 8, 2) };
    }

    // Build one row model and freeze its render signature (so SetRows can diff old-vs-new rows
    // with a plain string compare). Replaces the old `new Row(...)` call sites.
    Row MkRow(int kind, Dictionary<string, object> w, Dictionary<string, object> hist)
    {
        var r = new Row(kind, w, hist);
        r.Sig = RowSig(kind, w, hist);
        return r;
    }

    // Per-row render signature: captures every bit of state the row's builder reads, evaluated at
    // build time. Equal Sig => identical UIElement => SetRows can leave that container untouched.
    string RowSig(int kind, Dictionary<string, object> w, Dictionary<string, object> hist)
    {
        string g = (_dark ? "D" : "L") + _lang.ToString();   // theme/lang re-chrome every row
        switch (kind)
        {
            case 0:  // toolbar: reflects per-category counts (same helper the toolbar renders from,
                     // so a status-only reclassification like pending->done flips the signature)
                int[] tc0 = ToolbarCounts(_toolbarAll);
                return "T|" + g + "|" + _toolbarShown.Count + "/" + _toolbarAll.Count
                       + "|all" + tc0[0] + ":act" + tc0[1] + ":need" + tc0[2] + ":done" + tc0[3]
                       + ":max" + tc0[5] + ":bad" + tc0[6] + ":hid" + tc0[7] + ":int" + tc0[8]
                       + "|ar" + (_autoRetry ? 1 : 0) + ":" + _autoRetryMax + "|f" + _cardFilter;
            case 2: return "HH|" + g;                          // history header (static chrome; search box preserved across renders)
            case 7:                                            // date-group subheader: keyed on its label
                return "HG|" + g + "|" + (hist != null ? S(hist, "label") : "");
            case 8:                                            // resume affordance: keyed on N
                return "RA|" + g + "|" + (hist != null ? I(hist, "n") : 0)
                       + "|" + (hist != null ? S(hist, "signature") : "");
            case 9:                                            // submitted, not picked up yet: key + label + dismiss
            {
                var sv = hist != null && hist.ContainsKey("view") ? hist["view"] as SubmittedView : null;
                if (sv == null) return "SB|" + g;
                return "SB|" + g + "|" + StableShortHash(sv.Key) + "|" + SubmittedTasks.Label(sv, _lang == 0)
                       + "|" + (sv.Dismissable ? "x" : "-");
            }
            case 4: return "DV|" + g;                          // "完了 (this run)" divider
            case 5: return "ES|" + g + "|" + ReadQueuedJobs().Count;   // empty state: its headline counts the submitted group
            case 6:                                            // directive band: keyed on first-worker goal + started
                return "DB|" + g + "|" + (w != null ? S(w, "goal") + "|" + S(w, "name") : "")
                       + "|tc" + (_toolbarAll.Count) + "|" + _directiveBandMeta;
            case 3:                                            // history row: stable per entry, but expand state changes the render
            {
                string hk3 = hist != null ? S(hist, "key") : "";
                bool hOpen = !string.IsNullOrEmpty(hk3) && _expanded.Contains(hk3);
                return "h|" + g + "|" + (hist != null ? RuntimeHelpers.GetHashCode(hist) : 0)
                       + "|" + (hOpen ? "E" : "C");
            }
            default:                                           // kind 1: worker card
                string nm = S(w, "name");
                var sb = new StringBuilder("c|");
                sb.Append(g).Append('|').Append(nm)
                  .Append(S(w, "status")).Append(S(w, "turn")).Append(S(w, "outcome"))
                  .Append(S(w, "closed"));   // closed flips the left rail to neutral -> must re-render
                // The collapsed card now shows a result line + meta (turn/reviews/verified), so its
                // signature must track `last`/reviews/verified too -- otherwise the at-a-glance line
                // would freeze while the worker streams. Hash (not length) catches same-length content changes.
                string lastVal = S(w, "last");
                string drVal = S(w, "display_result");
                sb.Append(_expanded.Contains(nm) ? "#E" : "#C")
                  .Append(StableShortHash(lastVal)).Append(':').Append(StableShortHash(drVal))
                  .Append(':').Append(S(w, "verify_attempts")).Append(S(w, "verified"));
                // P0: conv_url drives the agent badge (agent-bound vs 既定Copilot); reason drives the
                // INFRA_STUCK row text — track both so those cards re-render when they change.
                sb.Append(':').Append(StableShortHash(S(w, "conv_url")))
                  .Append(':').Append(StableShortHash(S(w, "reason")))
                  // P1: the card headline prefers conv_title (Copilot auto-title) -> re-render when it arrives
                  .Append(':').Append(StableShortHash(S(w, "conv_title")));
                // TASK 3 (Bucket C): track next_step + self_confidence so the collapsed row re-renders.
                sb.Append('|').Append(S(w, "next_step").Length).Append(':').Append(S(w, "self_confidence"));
                // effort badge fields: re-render when the level/source/last switch change
                sb.Append("|ef:").Append(S(w, "effort_level")).Append(S(w, "effort_source"))
                  .Append(StableShortHash(S(Obj(w, "effort_last_switch") ?? new Dictionary<string, object>(), "reason")))
                  .Append(S(Obj(w, "effort_last_switch") ?? new Dictionary<string, object>(), "turn"));
                var ex = Obj(w, "execution");
                if (ex != null)
                    sb.Append("|exec:").Append(S(ex, "state"))
                      .Append(':').Append(S(ex, "current_step_index"))
                      .Append(':').Append(S(ex, "total_steps"))
                      .Append(':').Append(S(ex, "completed_count"))
                      .Append(':').Append(ArrCount(ex, "artifacts"))
                      .Append(':').Append(StableShortHash(S(ex, "current_step")))
                      .Append(':').Append(StableShortHash(S(ex, "last_progress")))
                      .Append(':').Append(S(ex, "last_progress_at"));
                // FIX B: _stopping dims non-terminal cards -- track it so a Stop click (or its
                // resolution) re-templates this card instead of reusing the old realized element.
                sb.Append('|').Append(_stopping ? "1" : "0");
                // Feature 1: fold whether THIS card's collapsed steer box currently has a non-empty
                // draft and/or keyboard focus into the signature. This does not, by itself, protect
                // the draft/focus (that's _steerDraft + _steerBoxRef + the RenderCards restore pass,
                // which work regardless of Sig) -- it exists so that starting/ending a steer draft
                // or focus on this specific card is itself a tracked, deterministic state transition
                // rather than invisible to the diff, matching how every other read field here is
                // captured. Booleans only (not the draft text itself) -- empty-to-non-empty and
                // unfocused-to-focused are each a single transition, so this does NOT force a
                // rebuild on every keystroke the way hashing the live draft text would.
                string draftSt;
                bool hasDraft = _steerDraft.TryGetValue(nm, out draftSt) && !string.IsNullOrEmpty(draftSt);
                bool hasFocus = _steerFocusWorker == nm;
                sb.Append('|').Append(hasDraft ? "d1" : "d0").Append(hasFocus ? "f1" : "f0");
                sb.Append("|grp:").Append(StableShortHash(GroupLineText(w) + GroupExtraSig(w) + "|" + S(w, "display_label")));
                return sb.ToString();
        }
    }

    // Reconcile the persistent _rows collection toward `rows` IN PLACE. Item-by-item Replace plus
    // tail Add/Remove -- never Clear() and never a fresh ItemsSource -- so the VirtualizingStackPanel
    // never sees a collection Reset and the pixel scroll offset is preserved. Only rows whose Sig
    // changed are swapped, so unchanged cards keep their realized containers (no flicker, and a
    // chevron toggle re-templates exactly the one card that changed).
    void SetRows(List<object> rows)
    {
        if (_list == null) return;
        int n = rows.Count, m = _rows.Count;
        int common = n < m ? n : m;
        for (int i = 0; i < common; i++)
        {
            var nw = rows[i] as Row;
            var od = _rows[i] as Row;
            if (od == null || nw == null || od.Sig != nw.Sig)
                _rows[i] = rows[i];        // targeted Replace -> only this container re-templates
        }
        if (n > m) { for (int i = m; i < n; i++) _rows.Add(rows[i]); }
        else if (m > n) { for (int i = m - 1; i >= n; i--) _rows.RemoveAt(i); }
    }

    // Walk the visual tree from _list to find its internal ScrollViewer (present once the
    // template is applied). Used to capture/restore scroll offset across a re-render. Returns
    // null before the first layout pass.
    ScrollViewer ListScroller()
    {
        if (_list == null) return null;
        return FindScrollViewer(_list);
    }
    static ScrollViewer FindScrollViewer(DependencyObject root)
    {
        if (root == null) return null;
        int n = VisualTreeHelper.GetChildrenCount(root);
        for (int i = 0; i < n; i++)
        {
            DependencyObject ch = VisualTreeHelper.GetChild(root, i);
            ScrollViewer sv = ch as ScrollViewer;
            if (sv != null) return sv;
            ScrollViewer deep = FindScrollViewer(ch);
            if (deep != null) return deep;
        }
        return null;
    }

    // Tiny per-row model. Kind: 0=toolbar, 1=card, 2=history-header, 3=history-row, 4=completed-divider,
    // 5=empty state, 6=directive band, 7=date-group header, 8=resume affordance,
    // 9=submitted-not-picked-up-yet (Hist["view"] is its SubmittedView).
    class Row
    {
        public int Kind;
        public Dictionary<string, object> Worker;
        public Dictionary<string, object> Hist;
        // Render signature, computed at build time (MkRow). Two rows with equal Sig produce an
        // identical UIElement, so SetRows skips re-templating them -- and a later compare of an
        // old row vs a freshly-built one is just a string compare (the state is frozen in here).
        public string Sig;
        public Row(int kind, Dictionary<string, object> worker, Dictionary<string, object> hist)
        { Kind = kind; Worker = worker; Hist = hist; }
    }

    // Converts a Row model into its realized UIElement by dispatching on Row.Kind to the EXISTING
    // builders. Holds a back-reference to the window (C# 5: no lambdas-as-closures over fields in
    // a converter, so pass `this`). Called for visible rows only because containers recycle.
    class RowConverter : IValueConverter
    {
        readonly CockpitWindow _w;
        public RowConverter(CockpitWindow w) { _w = w; }
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        {
            var r = value as Row;
            if (r == null) return null;
            if (r.Kind == 0) return _w.BuildCardToolbar(_w._toolbarAll, _w._toolbarShown);
            if (r.Kind == 1) return _w.Card(r.Worker);
            if (r.Kind == 2) return _w.HistoryHeader();
            if (r.Kind == 3) return _w.HistoryRow(r.Hist);
            if (r.Kind == 4) return _w.CompletedDivider();
            if (r.Kind == 5) return _w.EmptyState();
            if (r.Kind == 6) return _w.DirectiveBand(r.Worker);
            if (r.Kind == 7) return _w.HistoryGroupHeader(r.Hist != null ? S(r.Hist, "label") : "");
            if (r.Kind == 9) return _w.SubmittedRow(
                r.Hist != null && r.Hist.ContainsKey("view") ? r.Hist["view"] as SubmittedView : null);
            if (r.Kind == 8) return _w.ResumeAffordance(
                r.Hist != null ? I(r.Hist, "n") : 0,
                r.Hist != null ? S(r.Hist, "signature") : "");
            return null;
        }
        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        { return Binding.DoNothing; }
    }

    // Stable sort by severity rank (C# 5: List.Sort is NOT stable, so do a manual stable sort
    // by bucketing in rank order while preserving original index order within each rank).
    static List<Dictionary<string, object>> StableBySeverity(List<Dictionary<string, object>> src)
    {
        var outp = new List<Dictionary<string, object>>();
        for (int rank = 0; rank <= 3; rank++)
            foreach (Dictionary<string, object> w in src)
                if (SeverityRank(w) == rank) outp.Add(w);
        return outp;
    }

    // Default-view order: what is HAPPENING now floats to the top, finished work sinks to the
    // bottom. Stable within each bucket so a worker keeps its place (and its W-name identity)
    // and cards do not jump around as unrelated workers tick. status.json lists workers in launch
    // order, so without this the earliest-launched (now-completed) workers stay pinned at the top.
    //
    // The order itself now lives in SubmittedTasks.Compose, where a test executes it: submitted
    // (not picked up yet) -> fresh pending worker (the newest task; FRESH_PENDING_S says what
    // "fresh" is) -> active -> older pending -> terminal. A pending worker used to sort below
    // every active card however new it was, so the task just submitted was never the one on top.
    // This is the terminal test that order uses; "freed" sinks with the finished ones, as before.
    static bool IsTerminalForOrder(Dictionary<string, object> w)
    {
        return IsTerminalWorker(w) || S(w, "status") == "freed";
    }

    void AppendSubmittedRows(List<object> rows)
    {
        AppendSubmittedRows(rows, SubmittedTasks.Compose(ReadQueuedJobs(), null, NowUnix(),
                                                         IsTerminalForOrder));
    }

    // One row per submitted entry, in the order Compose gave them.
    void AppendSubmittedRows(List<object> rows, List<SubmittedDisplayItem> order)
    {
        if (order == null) return;
        foreach (SubmittedDisplayItem it in order)
        {
            if (it.Submitted == null) continue;
            var d = new Dictionary<string, object>();
            d["view"] = it.Submitted;
            rows.Add(MkRow(9, null, d));
        }
    }

    // A "submitted, not picked up yet" row: the label (state + age + where it is), then the goal
    // on one line, trimmed like a card title. Drawn as a quiet bordered row, not a card: there is
    // no worker behind it yet, so there is nothing to expand, steer or retry. The only action is
    // the person's own dismiss, offered once SubmittedTasks says the entry may be dismissed (no
    // file behind it, past the unconfirmed threshold) -- never on a fresh entry that the fleet
    // may still pick up, and never on a timer.
    UIElement SubmittedRow(SubmittedView v)
    {
        bool ja = _lang == 0;
        var row = new Border {
            BorderThickness = new Thickness(1), BorderBrush = Border, Background = BtnBg,
            CornerRadius = new CornerRadius(Theme.RadCard),
            Padding = new Thickness(16, 8, 16, 8), Margin = new Thickness(8, 8, 8, 4) };
        var dp = new DockPanel { LastChildFill = true };
        if (v != null && v.Dismissable)
        {
            var close = IconButton("close", 14, ja ? "この投入済みタスクの表示を消す" : "Dismiss this submitted task");
            close.Width = 28; close.Height = 28; close.Margin = new Thickness(8, 0, 0, 0);
            close.Padding = new Thickness(0); close.BorderThickness = new Thickness(0);
            close.Background = Brushes.Transparent; close.Foreground = Muted;
            close.Template = FlatButtonTemplate();
            close.ToolTip = ja ? "消す（フリートの作業は取り消しません）"
                               : "Dismiss (does not cancel anything in the fleet)";
            string key = v.Key;
            close.Click += delegate (object sender, RoutedEventArgs e)
            {
                e.Handled = true;
                if (_submitted.Dismiss(key, NowUnix())) ForceRender();
            };
            DockPanel.SetDock(close, Dock.Right); dp.Children.Add(close);
        }
        var stack = new StackPanel();
        stack.Children.Add(new TextBlock {
            Text = SubmittedTasks.Label(v, ja),
            // Full-strength text once an unconfirmed entry is stale: that is the one worth a look.
            Foreground = (v != null && v.Unconfirmed && v.Stale) ? Fg : Muted,
            FontSize = 11.5, FontWeight = FontWeights.SemiBold,
            TextTrimming = TextTrimming.CharacterEllipsis });
        stack.Children.Add(new TextBlock {
            Text = OneLine(v != null ? v.Goal : "", 140),
            Foreground = Fg, FontSize = 13, Margin = new Thickness(0, 2, 0, 0),
            TextTrimming = TextTrimming.CharacterEllipsis,
            ToolTip = v != null ? v.Goal : "" });
        dp.Children.Add(stack);
        row.Child = dp;
        return row;
    }

    // Feature B/C toolbar: filter selector + outcome summary + bulk-retry. Rebuilt each
    // RenderCards as the first row of _cards (no worker Tag, so ToggleExpand skips it).
    // Empty state (spec): a centered narrow block with quiet suggestion chips, shown when there is
    // no run and no history. Suggestions just pre-fill the bottom composer -- they don't launch.
    UIElement EmptyState()
    {
        // A SUBMITTED JOB IS NOT "NO TASKS". status.json describes workers, and a job that has
        // been queued has none until a coordinator starts -- so this said "no fleet tasks"
        // while the queue held work, and an operator could not tell a submission that landed
        // from one that went nowhere. The list is the merged "submitted" group, the same one
        // the rows above this block are drawn from.
        var queued = ReadQueuedJobs();
        // Closer to the rows when there are submitted rows above it to read with.
        var outer = new Border { Margin = new Thickness(0, queued.Count > 0 ? 24 : 80, 0, 0) };
        var block = new StackPanel { MaxWidth = 520, HorizontalAlignment = HorizontalAlignment.Center };

        if (queued.Count > 0)
        {
            block.Children.Add(new TextBlock {
                Text = _lang == 0
                    ? string.Format("投入済み {0} 件 — まだどのワーカーも受け取っていません", queued.Count)
                    : string.Format("{0} submitted — no worker has picked them up yet", queued.Count),
                Foreground = Fg, FontSize = 15, FontWeight = FontWeights.SemiBold,
                HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 0, 0, 6) });
            block.Children.Add(new TextBlock {
                Text = _lang == 0
                    ? "キューには入っています。コーディネータが起動すると進捗がここに出ます。"
                    : "They are on the queue. Progress appears here once a coordinator starts.",
                Foreground = Muted, FontSize = 12.5, TextWrapping = TextWrapping.Wrap,
                TextAlignment = TextAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center,
                Margin = new Thickness(0, 0, 0, 12) });
            // The entries themselves are NOT listed again here: they are the rows directly
            // above this block (AppendSubmittedRows), drawn from this same list, so the count
            // in the headline and the rows on screen cannot disagree.
        }
        else
        {
            block.Children.Add(new TextBlock {
                Text = _lang == 0 ? "タスクはまだありません" : "No fleet tasks",
                Foreground = Fg, FontSize = 15, FontWeight = FontWeights.SemiBold,
                HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 0, 0, 6) });
            block.Children.Add(new TextBlock {
                Text = _lang == 0 ? "複数のタスクを並行で走らせ、ここで進捗を確認します。"
                                  : "Run several tasks in parallel, then monitor progress here.",
                Foreground = Muted, FontSize = 12.5, TextWrapping = TextWrapping.Wrap,
                TextAlignment = TextAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center,
                Margin = new Thickness(0, 0, 0, 16) });
        }

        string[] suggestions = _lang == 0
            ? new string[] { "失敗テストを修正", "UIの問題をレビュー", "READMEを更新", "フォルダのタスクを実行" }
            : new string[] { "Fix failing tests", "Review UI issues", "Update README", "Run folder task" };
        var wrap = new WrapPanel { HorizontalAlignment = HorizontalAlignment.Center };
        foreach (string s in suggestions)
        {
            string text = s;
            var chip = new Button {
                Content = text, Cursor = Cursors.Hand, FontSize = 12,
                Background = Brushes.Transparent, Foreground = Fg,
                BorderBrush = Border, BorderThickness = new Thickness(1),
                Padding = new Thickness(12, 5, 12, 5), Margin = new Thickness(4, 4, 4, 4)
            };
            chip.Click += delegate
            {
                if (_goalInput != null)
                {
                    _goalInput.Text = text;
                    _goalInput.CaretIndex = text.Length;
                    _goalInput.Focus();
                }
            };
            wrap.Children.Add(chip);
        }
        block.Children.Add(wrap);
        outer.Child = block;
        return outer;
    }

    // P2 RESUME: inline banner shown (idle only, N>0) offering to relaunch the fleet with --resume,
    // which re-queues the unfinished goals from the last run's durable ledger. Hidden while a run is
    // live or when N==0 (the row is simply not emitted in those cases).
    UIElement ResumeAffordance(int n, string signature)
    {
        var row = new Border {
            BorderThickness = new Thickness(1), BorderBrush = Border, Background = BtnBg,
            CornerRadius = new CornerRadius(Theme.RadCard),
            Padding = new Thickness(16, 8, 16, 8), Margin = new Thickness(8, 8, 8, 4) };
        var dp = new DockPanel { LastChildFill = true };

        // This banner is advisory, not a blocking error. Let the user dismiss this exact stale
        // resume state permanently; a new interrupted run has a different signature and returns.
        var close = IconButton("close", 14, _lang == 0 ? "未完了ランの通知を閉じる" : "Dismiss unfinished-run notice");
        close.Width = 28; close.Height = 28; close.Margin = new Thickness(8, 0, 0, 0);
        close.Padding = new Thickness(0); close.BorderThickness = new Thickness(0);
        close.Background = Brushes.Transparent; close.Foreground = Muted;
        close.Template = FlatButtonTemplate();
        close.ToolTip = _lang == 0 ? "この未完了ランを非表示" : "Hide this unfinished run";
        string capturedSignature = signature;
        close.Click += delegate (object sender, RoutedEventArgs e)
        { e.Handled = true; DismissResumeState(capturedSignature); };
        DockPanel.SetDock(close, Dock.Right); dp.Children.Add(close);

        var btn = new Button {
            Content = _lang == 0 ? "再開" : "Resume", Cursor = Cursors.Hand, FontSize = 12,
            FontWeight = FontWeights.SemiBold, Padding = new Thickness(12, 4, 12, 4),
            BorderThickness = new Thickness(1) };
        btn.Background = BtnBg; btn.Foreground = Fg; btn.BorderBrush = Border;
        btn.Template = FlatButtonTemplate();
        btn.Click += delegate
        {
            try
            {
                SpawnFleetResume();
                _lastSig = "";
                if (_startNote != null)
                    _startNote.Text = _lang == 0 ? "前回のランを再開しました。" : "Resumed the last run.";
            }
            catch (Exception ex)
            {
                if (_startNote != null) _startNote.Text = (_lang == 0 ? "再開に失敗: " : "Resume failed: ") + ex.Message;
            }
        };
        DockPanel.SetDock(btn, Dock.Right);
        dp.Children.Add(btn);

        var msg = new TextBlock {
            Text = _lang == 0 ? ("前回のランに未完了が " + n + " 件")
                              : ("Last run left " + n + " unfinished"),
            Foreground = Fg, FontSize = 12.5, VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis };
        dp.Children.Add(msg);

        row.Child = dp;
        return row;
    }

    // TASK 3: Directive band — shown only in the live/active branch, inserted above card list.
    // Reads goal from the first/primary worker (passed as `firstWorker`).
    // [REAL goal], [COMPUTED meta] — spec §5a. No fabrication; multi-goal case is honest.
    UIElement DirectiveBand(Dictionary<string, object> firstWorker)
    {
        bool ja = _lang == 0;

        // Gather goal texts from the ON-BOARD workers (History-cleared lanes excluded, set in
        // BuildRows) to determine single vs multi-goal.
        var goalTexts = new List<string>();
        var goalDisplays = new List<string>();
        var dbSrc = _directiveBandWorkers != null && _directiveBandWorkers.Count > 0 ? _directiveBandWorkers : _toolbarAll;
        foreach (Dictionary<string, object> tw in dbSrc)
        {
            string fullGoal = S(tw, "goal");
            if (string.IsNullOrEmpty(fullGoal) || goalTexts.Contains(fullGoal)) continue;
            goalTexts.Add(fullGoal);
            string displayGoal = S(tw, "goal_summary");
            goalDisplays.Add(!string.IsNullOrEmpty(displayGoal) ? displayGoal : CardTitle("", fullGoal));
        }

        // Section label: "DIRECTIVE" / "指示" when a single goal; "Goals (N)" / "ゴール (N)" for multiple.
        bool multiGoal = goalTexts.Count > 1;
        string sectionLabel = multiGoal
            ? (ja ? ("ゴール (" + goalTexts.Count + ")") : ("Goals (" + goalTexts.Count + ")"))
            : (ja ? "指示" : "DIRECTIVE");

        // EVERY GOAL, ONE PER LINE. This used to render goalTexts[0] and summarise the rest as
        // "(他 N lane)" -- so with two goals in flight the band showed one of them and a count,
        // and WHICH one it showed depended on the order the worker list happened to arrive in.
        // Every refresh could pick a different first element, so the panel flickered between
        // goals as lanes progressed, and the operator saw the view "move" on its own.
        //
        // The list was already complete here; only the rendering threw it away. Concurrent
        // lanes are concurrent: they belong side by side, not behind a number.
        string goalDisplay = string.Join("\n", goalDisplays.ToArray());

        // Meta line: "started HH:MM · {elapsed} · {active}/{total} lanes active" [COMPUTED]
        string metaLine = _directiveBandMeta;

        var outer = new Border();
        outer.Margin = new Thickness(16, 0, 16, 0);
        outer.Padding = new Thickness(0, 8, 0, 0);
        outer.BorderThickness = new Thickness(0, 0, 0, 1);
        outer.BorderBrush = Theme.Br(Theme.Border(_dark));

        var col = new StackPanel();

        // Section label
        var lbl = new TextBlock();
        lbl.Text = sectionLabel;
        lbl.Foreground = Theme.Br(Theme.Muted(_dark));
        lbl.FontSize = Theme.FsMeta;
        lbl.FontWeight = FontWeights.SemiBold;
        lbl.Margin = new Thickness(0, 0, 0, 4);
        col.Children.Add(lbl);

        // Separator line
        var sep = new Border();
        sep.Height = 1;
        sep.Background = Theme.Br(Theme.Border(_dark));
        sep.Margin = new Thickness(0, 0, 0, 6);
        col.Children.Add(sep);

        // Goal text — show even if empty (no fallback fabrication; empty just shows nothing)
        if (!string.IsNullOrEmpty(goalDisplay))
        {
            var goalTb = new TextBlock();
            goalTb.Text = goalDisplay;
            goalTb.Foreground = Theme.Br(Theme.Text(_dark));
            goalTb.FontSize = Theme.FsBody;
            goalTb.TextWrapping = TextWrapping.Wrap;
            goalTb.Margin = new Thickness(0, 0, 0, 4);
            col.Children.Add(goalTb);
        }

        // Meta line (started · elapsed · lanes)
        if (!string.IsNullOrEmpty(metaLine))
        {
            var metaTb = new TextBlock();
            metaTb.Text = metaLine;
            metaTb.Foreground = Theme.Br(Theme.Muted(_dark));
            metaTb.FontSize = Theme.FsMeta;
            metaTb.Margin = new Thickness(0, 0, 0, 8);
            col.Children.Add(metaTb);
        }

        outer.Child = col;
        return outer;
    }


    // Single source of truth for the toolbar's per-category counts. Returns an int[] so both the
    // toolbar render (BuildCardToolbar) and the row signature (RowSig) use IDENTICAL arithmetic --
    // otherwise a status-only change (pending->done) that keeps cntAll constant would not flip the
    // signature and the toolbar would render stale counts until the next forced re-render.
    // Index map: [0]=cntAll [1]=cntActive [2]=cntNeeds [3]=cntDone [4]=doneN [5]=maxN [6]=badN
    //            [7]=hiddenTerminal (terminal cards moved to History, excluded above -- folding this
    //            into the signature makes "send to history" also re-render the toolbar).
    int[] ToolbarCounts(List<Dictionary<string, object>> all)
    {
        int cntAll = 0, cntActive = 0, cntNeeds = 0, cntDone = 0;
        int doneN = 0, maxN = 0, badN = 0, hiddenTerminal = 0, intN = 0;
        string startedRootTb = _lastRoot != null ? S(_lastRoot, "started") : "";
        if (all != null)
        {
            foreach (Dictionary<string, object> w in all)
            {
                if (_hiddenKeys.Count > 0 && IsTerminalWorker(w) && _hiddenKeys.Contains(WorkerKey(startedRootTb, w)))
                {
                    hiddenTerminal++;
                    continue;   // moved to History -- not on the board, don't count
                }
                cntAll++;
                string oc = S(w, "outcome");
                string st = S(w, "status");
                if (oc == "DONE") { doneN++; cntDone++; }
                else if (oc == "MAXTURNS") maxN++;
                // A CONTRADICTED claim counts with the failures, never with the completions.
                // Leaving it out of both tallies would be worse than either: the totals would
                // stop adding up to the number of workers, and a card would vanish from the
                // counts while still sitting on screen.
                else if (oc == "STUCK" || oc == "ERROR" || oc == "CANCELLED"
                         || oc == "EVIDENCE_CONTRADICTED") badN++;
                if (st == "awaiting") cntNeeds++;
                if (IsInterruptedWorker(w)) intN++;   // own counter: resumable, not a failure of the goal
                if (!IsTerminalWorker(w) && !IsInterruptedWorker(w) && st != "pending") cntActive++;
            }
        }
        return new int[] { cntAll, cntActive, cntNeeds, cntDone, doneN, maxN, badN, hiddenTerminal, intN };
    }

    UIElement BuildCardToolbar(List<Dictionary<string, object>> all,
                               List<Dictionary<string, object>> shown)
    {
        // Per-tab counts from the ON-BOARD worker list = the full list MINUS the terminal cards
        // the user moved to History (_hiddenKeys). We don't use `shown` because that is also
        // narrowed by the active filter; we want totals-per-category independent of the filter,
        // but a card moved to History must not be counted (else "all 1 / done 1" persists after
        // everything was sent to history). Counts come from the shared ToolbarCounts() helper so
        // the render and RowSig can never diverge.
        int[] tc = ToolbarCounts(all);
        int cntAll = tc[0], cntActive = tc[1], cntNeeds = tc[2], cntDone = tc[3];
        int doneN = tc[4], maxN = tc[5], badN = tc[6], cntIntr = tc[8];

        var bar = new Border();
        bar.BorderThickness = new Thickness(1); bar.BorderBrush = Border;
        bar.Background = CardBg; bar.CornerRadius = new CornerRadius(Theme.RadPopover);
        bar.Padding = new Thickness(12, 8, 12, 8); bar.Margin = new Thickness(8, 2, 8, 8);
        // clicks inside the toolbar must not bubble (it isn't a card, but stay safe)
        bar.MouseLeftButtonUp += delegate(object s, MouseButtonEventArgs e) { e.Handled = true; };

        var dp = new DockPanel();

        // right cluster: bulk MANUAL retry button (one-shot, respects the active filter)
        var rightCl = new StackPanel(); rightCl.Orientation = Orientation.Horizontal;
        rightCl.VerticalAlignment = VerticalAlignment.Center;

        // Only surface the bulk-retry button when there is at least one retry target in
        // `shown`. Mirrors RetryAllShown's selection, which is now the same closed set Python
        // uses -- see AutoRetryScan for what "anything but DONE" cost the fan-out.
        int retryTargets = 0;
        foreach (Dictionary<string, object> rw in shown)
        {
            if (!IsTerminalWorker(rw)) continue;
            if (!IsRetryableWorker(rw)) continue;
            retryTargets++;
        }
        if (retryTargets > 0)
        {
            var retryAll = new Button();
            retryAll.Content = T("retry_all");
            retryAll.Background = Brushes.Transparent; retryAll.Foreground = Fg;
            retryAll.BorderThickness = new Thickness(1); retryAll.BorderBrush = Theme.Br(Theme.BorderStrong(_dark));
            retryAll.Padding = new Thickness(12, 4, 12, 4); retryAll.Cursor = Cursors.Hand;
            retryAll.FontSize = 12; retryAll.FontWeight = FontWeights.SemiBold;
            retryAll.Margin = new Thickness(12, 0, 0, 0);
            retryAll.VerticalAlignment = VerticalAlignment.Center;
            List<Dictionary<string, object>> shownCap = shown;
            retryAll.Click += delegate
            {
                int skipped;
                int queued = RetryAllShown(shownCap, out skipped);
                if (_toolbarNote != null)
                {
                    string note = RunIsLive() ? "" : T("retry_note");
                    if (skipped > 0)
                    {
                        // Say how many of the shown targets were refused and why. Without this
                        // the press looks like a no-op and gets repeated.
                        string cap = skipped + "/" + (queued + skipped) + " " + T("retry_capped");
                        note = string.IsNullOrEmpty(note) ? cap : cap + " — " + note;
                    }
                    _toolbarNote.Text = note;
                }
            };
            rightCl.Children.Add(retryAll);
        }

        DockPanel.SetDock(rightCl, Dock.Right);
        dp.Children.Add(rightCl);

        // left: 4 spec tabs as a single segmented control container
        var left = new StackPanel(); left.Orientation = Orientation.Horizontal;
        left.VerticalAlignment = VerticalAlignment.Center;

        // Segmented container: one rounded Border holding all four tabs side-by-side.
        var seg = new Border();
        seg.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        seg.BorderBrush = Theme.Br(Theme.Border(_dark));
        seg.BorderThickness = new Thickness(1);
        seg.CornerRadius = new CornerRadius(Theme.RadSmall);
        seg.VerticalAlignment = VerticalAlignment.Center;
        var segRow = new StackPanel(); segRow.Orientation = Orientation.Horizontal;

        // Build all four tabs; insert thin dividers between them.
        string allLabel = T("flt_all") + " " + cntAll;
        string activeLabel = T("flt_active") + " " + cntActive;
        string needsLabel = T("flt_needs") + " " + cntNeeds;
        string doneLabel = T("flt_done") + " " + cntDone;

        segRow.Children.Add(SegFilterButton(allLabel, 0, false, 0, true, false));
        segRow.Children.Add(SegDivider());
        segRow.Children.Add(SegFilterButton(activeLabel, 1, false, 0, false, false));
        segRow.Children.Add(SegDivider());
        segRow.Children.Add(SegFilterButton(needsLabel, 2, true, cntNeeds, false, false));
        segRow.Children.Add(SegDivider());
        bool showIntr = cntIntr > 0 || _cardFilter == 4;
        segRow.Children.Add(SegFilterButton(doneLabel, 3, false, 0, false, !showIntr));
        if (showIntr)
        {
            segRow.Children.Add(SegDivider());
            segRow.Children.Add(SegFilterButton(T("flt_intr") + " " + cntIntr, 4, false, 0, false, true));
        }

        seg.Child = segRow;
        left.Children.Add(seg);

        // Task 5: only show the summary when there are failures; hide when nothing notable.
        int failTotal = badN + maxN;
        if (failTotal > 0)
        {
            var summary = new TextBlock();
            summary.Text = (_lang == 0 ? ("失敗 " + failTotal) : ("Failed " + failTotal));
            summary.Foreground = Theme.Br(Theme.Danger(_dark)); summary.FontSize = 12;
            summary.FontWeight = FontWeights.SemiBold;
            summary.VerticalAlignment = VerticalAlignment.Center;
            summary.Margin = new Thickness(16, 0, 0, 0);
            left.Children.Add(summary);
        }

        _toolbarNote = new TextBlock();
        _toolbarNote.Foreground = Muted; _toolbarNote.FontSize = 11.5;
        _toolbarNote.VerticalAlignment = VerticalAlignment.Center;
        _toolbarNote.Margin = new Thickness(16, 0, 0, 0);
        left.Children.Add(_toolbarNote);

        dp.Children.Add(left);
        bar.Child = dp;
        return bar;
    }
    TextBlock _toolbarNote;
    Button _autoRetryBtn;
    TextBlock _autoRetryCapVal;
    Button _reconnectChatBtn;   // settings-panel "チャット再接続"/"Reconnect chat" -- always available,
                                 // regardless of the Tool dot's state (see RunBridgeReconnectManual).

    // ON => calm success-tinted chip (AccentSoft bg with Success border + text); OFF => neutral muted.
    // Accent fill is reserved exclusively for the primary Start action; this is a status toggle chip.
    void PaintAutoRetryBtn()
    {
        if (_autoRetryBtn == null) return;
        _autoRetryBtn.Content = T("autoretry") + ": " + (_autoRetry ? "ON" : "OFF");
        if (_autoRetry)
        {
            // Subtle success treatment: light chip background, success-colored border, dark/fg text
            // In dark mode: AccentSoft bg is very dark (#3A2416) -- use SurfaceSubtle instead for
            // better readability as a success chip; pair with Success border and Success-tinted text.
            _autoRetryBtn.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            _autoRetryBtn.Foreground = Theme.Br(Theme.Success(_dark));
            _autoRetryBtn.BorderBrush = Theme.Br(Theme.Success(_dark));
        }
        else
        {
            _autoRetryBtn.Background = BtnBg; _autoRetryBtn.Foreground = Muted; _autoRetryBtn.BorderBrush = Border;
        }
    }

    // P2 (c): auto-archive toggle chip -- same calm success/neutral treatment as PaintAutoRetryBtn.
    void PaintAutoArchiveBtn()
    {
        if (_autoArchiveBtn == null) return;
        _autoArchiveBtn.Content = T("autoarchive") + ": " + (_autoArchive ? "ON" : "OFF");
        if (_autoArchive)
        {
            _autoArchiveBtn.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            _autoArchiveBtn.Foreground = Theme.Br(Theme.Success(_dark));
            _autoArchiveBtn.BorderBrush = Theme.Br(Theme.Success(_dark));
        }
        else
        {
            _autoArchiveBtn.Background = BtnBg; _autoArchiveBtn.Foreground = Muted; _autoArchiveBtn.BorderBrush = Border;
        }
    }

    void SetAutoRetryMax(int v)
    {
        _autoRetryMax = Math.Max(1, Math.Min(3, v));   // hard safety bound: 1..3, never unbounded
        SaveKey("autoretry_max", _autoRetryMax.ToString());
        if (_autoRetryCapVal != null) _autoRetryCapVal.Text = _autoRetryMax.ToString();
    }

    // Thin vertical divider between segmented filter tabs.
    UIElement SegDivider()
    {
        var d = new Border { Width = 1, Background = Theme.Br(Theme.Border(_dark)),
                             VerticalAlignment = VerticalAlignment.Stretch,
                             Margin = new Thickness(0, 4, 0, 4) };
        return d;
    }

    // One tab button inside the segmented filter container. Active tab gets a Surface-colored
    // rounded background; inactive tabs are transparent. `isNeeds` + amber treatment preserved.
    // `isFirst`/`isLast` clip corner radius so the active fill doesn't overflow the container border.
    Button SegFilterButton(string label, int val, bool isNeeds, int needsCount, bool isFirst, bool isLast)
    {
        var b = new Button();
        b.Content = label; b.Cursor = Cursors.Hand; b.FontSize = 12;
        b.Padding = new Thickness(12, 5, 12, 5);
        b.BorderThickness = new Thickness(0);   // no individual border -- container holds the border
        bool active = _cardFilter == val;
        CornerRadius cr = new CornerRadius(
            isFirst ? 5 : 0, isLast ? 5 : 0, isLast ? 5 : 0, isFirst ? 5 : 0);
        if (active)
        {
            if (isNeeds && needsCount > 0)
            {
                // Warning amber: active "Needs input" with items
                b.Background = Theme.Br(Theme.Surface(_dark));
                b.Foreground = Theme.Br(Theme.Warning(_dark));
                b.FontWeight = FontWeights.SemiBold;
            }
            else
            {
                // Normal active: Surface fill, Fg text, semibold
                b.Background = Theme.Br(Theme.Surface(_dark));
                b.Foreground = Fg;
                b.FontWeight = FontWeights.SemiBold;
            }
        }
        else
        {
            b.Background = Brushes.Transparent;
            b.Foreground = Muted;
            b.FontWeight = FontWeights.Normal;
        }
        // Use flat template so Background is respected (default Aero ignores it)
        var tmpl = new ControlTemplate(typeof(Button));
        var bd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border), "Bd");
        bd.SetValue(System.Windows.Controls.Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
        bd.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, cr);
        var cp2 = new FrameworkElementFactory(typeof(ContentPresenter));
        cp2.SetValue(ContentPresenter.HorizontalAlignmentProperty, HorizontalAlignment.Center);
        cp2.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
        cp2.SetValue(ContentPresenter.MarginProperty, new Thickness(0));
        bd.AppendChild(cp2);
        tmpl.VisualTree = bd;
        b.Template = tmpl;
        int v = val;
        b.Click += delegate
        {
            if (_cardFilter == v) return;
            _cardFilter = v;
            _lastSig = ""; OnTick(null, null);
        };
        return b;
    }


    // "⋮" kebab button for card secondary actions. A null label inserts a visual separator.
    // Uses a WPF ContextMenu
    // (not a manual Popup) so it survives the VirtualizingStackPanel recycling its host row
    // without causing layout/focus loops. A ContextMenu is hosted in its own popup root and
    // is NOT tied to the visual tree of the virtualized element; it handles detach gracefully.
    UIElement CardKebabBtn(string[] labels, Action[] actions, Dictionary<string, object> w)
    {
        var btn = new Button();
        // Draw three vertical dots as geometry
        var dotsPanel = new StackPanel { Orientation = Orientation.Vertical,
                                         VerticalAlignment = VerticalAlignment.Center,
                                         HorizontalAlignment = HorizontalAlignment.Center };
        for (int i = 0; i < 3; i++)
        {
            var dot = new System.Windows.Shapes.Ellipse { Width = 3, Height = 3, Fill = Muted };
            if (i > 0) dot.Margin = new Thickness(0, 3, 0, 0);
            dotsPanel.Children.Add(dot);
        }
        btn.Content = dotsPanel;
        btn.Width = 28; btn.Height = 28; btn.Cursor = Cursors.Hand;
        btn.BorderThickness = new Thickness(0); btn.Background = Brushes.Transparent;
        btn.Padding = new Thickness(0);
        btn.ToolTip = _lang == 0 ? "その他の操作" : "More actions";
        btn.Template = FlatButtonTemplate();
        // Screen-reader name: include the worker name so each card's kebab is distinguishable.
        string kebabWorker = S(w, "name");
        string kebabName;
        if (!string.IsNullOrEmpty(kebabWorker))
            kebabName = _lang == 0 ? (kebabWorker + " その他の操作") : (kebabWorker + " more actions");
        else
            kebabName = _lang == 0 ? "その他の操作" : "More actions";
        System.Windows.Automation.AutomationProperties.SetName(btn, kebabName);

        // Build a ContextMenu. Unlike a manual Popup, a ContextMenu lives in its own HwndSource
        // and does not hold a reference that breaks when the virtualizing container is recycled.
        var cm = new ContextMenu();
        cm.Background = Theme.Br(Theme.Surface(_dark));
        cm.BorderBrush = Theme.Br(Theme.Border(_dark));
        cm.BorderThickness = new Thickness(1);
        cm.Padding = new Thickness(2);
        // The kebab sits at the far-right edge of a card. Shift the popup left so the complete
        // menu remains inside the Fleet window instead of growing off its right edge.
        cm.MinWidth = 240;
        cm.HorizontalOffset = -250;

        string[] lbls = labels;
        Action[] acts = actions;
        for (int i = 0; i < lbls.Length; i++)
        {
            int idx = i;
            if (lbls[idx] == null)
            {
                cm.Items.Add(new Separator { Margin = new Thickness(4, 3, 4, 3) });
                continue;
            }
            var mi = new MenuItem();
            mi.Header = lbls[idx];
            mi.Background = Brushes.Transparent;
            mi.Foreground = Theme.Br(Theme.Text(_dark));
            mi.BorderThickness = new Thickness(0);
            mi.Padding = new Thickness(12, 6, 12, 6);
            mi.FontSize = 12.5;
            Action a = acts[idx];
            mi.Click += delegate (object s2, RoutedEventArgs e2) { a(); };
            cm.Items.Add(mi);
        }
        btn.ContextMenu = cm;

        btn.Click += delegate (object s, RoutedEventArgs e)
        {
            e.Handled = true;
            if (btn.ContextMenu != null)
            {
                btn.ContextMenu.PlacementTarget = btn;
                btn.ContextMenu.Placement = PlacementMode.Bottom;
                btn.ContextMenu.IsOpen = true;
            }
        };
        // ButtonBase already handles its mouse-up. Do not mark PreviewMouseLeftButtonUp handled:
        // doing so prevents ButtonBase from raising Click, leaving this menu apparently dead.
        return btn;
    }

    // The history SECTION HEADER (Clear button + caption). Factored out of the old AppendHistory
    // so the virtualizing converter can build it for a Kind==2 row. The history ROWS are built
    // separately by HistoryRow() per Kind==3 row.
    UIElement HistoryHeader()
    {
        var head = new DockPanel();
        head.Margin = new Thickness(8, 16, 8, 4);
        var clear = new Button();
        clear.Content = T("clear_history") + " (" + _history.Count + ")";
        clear.Cursor = Cursors.Hand; clear.BorderThickness = new Thickness(1);
        clear.Background = BtnBg; clear.Foreground = Fg; clear.BorderBrush = Border;
        clear.Padding = new Thickness(12, 2, 12, 2); clear.FontSize = 12;
        clear.Click += delegate { ClearHistory(); };
        DockPanel.SetDock(clear, Dock.Right);
        head.Children.Add(clear);

        // P2 (b): live search box. Docked right of the caption; typing filters the rows below with a
        // ~300ms debounce (so it doesn't rebuild on every keystroke). Built ONCE and preserved across
        // renders (the header row's signature is static), so focus + caret survive a re-filter.
        if (_histSearchBox == null)
        {
            _histSearchBox = new TextBox();
            System.Windows.Automation.AutomationProperties.SetAutomationId(_histSearchBox, "histSearch");
            _histSearchBox.Text = _histQuery ?? "";
            _histSearchBox.Width = 160; _histSearchBox.FontSize = 12;
            _histSearchBox.Padding = new Thickness(6, 2, 6, 2);
            _histSearchBox.VerticalContentAlignment = VerticalAlignment.Center;
            _histSearchBox.BorderThickness = new Thickness(1);
            _histSearchBox.ToolTip = T("hist_search");
            _histSearchBox.TextChanged += delegate { OnHistSearchChanged(); PaintHistHint(); };
            _histSearchBox.GotKeyboardFocus += delegate { PaintHistHint(); };
            _histSearchBox.LostKeyboardFocus += delegate { PaintHistHint(); };
        }
        _histSearchBox.Background = BtnBg; _histSearchBox.Foreground = Fg; _histSearchBox.BorderBrush = Border;
        // The box is cached so focus and caret survive a re-filter, but the wrapper around it is
        // not: this method runs again whenever the list re-realizes the header row (virtualization
        // recycling, DataContext change), and handing an element that still has a logical parent
        // to a second Border throws InvalidOperationException. That escapes through a binding
        // converter, where nothing catches it, and WPF aborts the process -- the cockpit was
        // dying mid-run. Disconnect from the previous wrapper first.
        // BOTH KINDS OF PARENT. The box used to sit directly in a Border, so a Decorator cast
        // found it; it now sits in a Grid alongside its placeholder, and a Decorator cast returns
        // null for a Panel -- which would put the crash this guard exists to prevent straight
        // back. Detaching from whichever it is keeps the guard honest as the tree changes.
        var prevDec = LogicalTreeHelper.GetParent(_histSearchBox) as Decorator;
        if (prevDec != null) prevDec.Child = null;
        var prevPanel = LogicalTreeHelper.GetParent(_histSearchBox) as Panel;
        if (prevPanel != null) prevPanel.Children.Remove(_histSearchBox);
        // A TEXTBOX WITH NOTHING IN IT IS NOT A CONTROL, IT IS A RECTANGLE. This one had a
        // tooltip and nothing else -- no label, no placeholder, no icon -- so on the screen it
        // read as a box that had failed to draw. WPF has no placeholder, so the standard shape
        // is a hint drawn behind the text and hidden the moment there is any.
        if (_histHint == null)
        {
            _histHint = new TextBlock();
            _histHint.FontSize = 12;
            _histHint.IsHitTestVisible = false;
            _histHint.VerticalAlignment = VerticalAlignment.Center;
            _histHint.Margin = new Thickness(8, 0, 0, 0);
        }
        _histHint.Text = _lang == 0 ? "絞り込み" : "Filter";
        _histHint.Foreground = Theme.Br(Theme.Faint(_dark));
        var hintHost = LogicalTreeHelper.GetParent(_histHint) as Panel;
        if (hintHost != null) hintHost.Children.Remove(_histHint);

        var searchStack = new Grid();
        searchStack.Children.Add(_histSearchBox);
        searchStack.Children.Add(_histHint);
        var searchWrap = new Border { Child = searchStack, Margin = new Thickness(8, 0, 8, 0) };
        DockPanel.SetDock(searchWrap, Dock.Right);
        head.Children.Add(searchWrap);
        PaintHistHint();

        // A HEADING AND ITS HELP TEXT, NOT ONE SENTENCE IN ONE WEIGHT. What this section IS and
        // how it BEHAVES were set in the same 12.5px muted grey, so the eye had to read the whole
        // line to find the one word that names the section.
        var ht = new TextBlock();
        ht.VerticalAlignment = VerticalAlignment.Center;
        var htName = new System.Windows.Documents.Run(_lang == 0 ? "履歴" : "History");
        htName.FontWeight = FontWeights.SemiBold;
        htName.Foreground = Fg;
        ht.Inlines.Add(htName);
        var htNote = new System.Windows.Documents.Run(_lang == 0
            ? "  クリアするまで蓄積、クリックで会話を表示"
            : "  stacks until cleared, click to open");
        htNote.Foreground = Theme.Br(Theme.Faint(_dark));
        ht.Inlines.Add(htNote);
        ht.FontSize = 12.5;
        head.Children.Add(ht);
        return head;
    }

    // Show the hint only while the box is genuinely empty. Not tied to focus alone: a focused
    // empty box still needs to say what it filters, and a box with text in it must not have a
    // second string underneath.
    void PaintHistHint()
    {
        if (_histHint == null) return;
        bool empty = _histSearchBox == null || string.IsNullOrEmpty(_histSearchBox.Text);
        _histHint.Visibility = empty ? Visibility.Visible : Visibility.Collapsed;
    }

    // Debounced history-search handler: stash the query, then (re)arm a ~300ms one-shot timer that
    // triggers a single re-render. Prevents a rebuild-per-keystroke thrash while staying live.
    void OnHistSearchChanged()
    {
        if (_histSearchBox == null) return;
        _histQuery = _histSearchBox.Text ?? "";
        if (_histSearchTimer == null)
        {
            _histSearchTimer = new DispatcherTimer();
            _histSearchTimer.Interval = TimeSpan.FromMilliseconds(300);
            _histSearchTimer.Tick += delegate { _histSearchTimer.Stop(); ForceRender(); };
        }
        _histSearchTimer.Stop();
        _histSearchTimer.Start();
    }

    // Divider that separates the live (active/queued) cards above from this run's TERMINAL workers
    // below -- so the top of the list is only what's still running.
    UIElement CompletedDivider()
    {
        int n = 0;
        if (_toolbarShown != null)
            foreach (Dictionary<string, object> w in _toolbarShown)
                if (IsTerminalWorker(w)) n++;
        var head = new DockPanel();
        head.Margin = new Thickness(8, 16, 8, 4);
        var all = new Button();
        all.Content = T("all_to_history");
        all.Cursor = Cursors.Hand; all.BorderThickness = new Thickness(1);
        all.Background = BtnBg; all.Foreground = Fg; all.BorderBrush = Border;
        all.Padding = new Thickness(12, 2, 12, 2); all.FontSize = 12;
        all.ToolTip = _lang == 0 ? "完了をすべて履歴へ移動" : "Move all completed to history";
        all.Click += delegate { ArchiveAllTerminal(); };
        DockPanel.SetDock(all, Dock.Right);
        head.Children.Add(all);
        var t = new TextBlock();
        t.Text = (_lang == 0 ? "完了 — " : "Completed — ") + n + (_lang == 0 ? " 件（このラン）" : " (this run)");
        t.Foreground = Muted; t.FontSize = 12.5; t.VerticalAlignment = VerticalAlignment.Center;
        head.Children.Add(t);
        return head;
    }

    Border HistoryRow(Dictionary<string, object> e)
    {
        string status = S(e, "status");
        bool internalControl = IsLocalLoopControlGoal(S(e, "goal"));
        string ck = ColorKey(status);
        string conv = S(e, "conv_url");
        // Default COLLAPSED, exactly like a live card: a terminal worker that scrolls down into
        // History must NOT spring open. The disclosure key is the archive `key` (started#name),
        // which never collides with a live worker name (those carry no '#'), so it can ride the
        // same _expanded set / ToggleExpand path as live cards.
        string hkey = S(e, "key");
        bool isOpen = !string.IsNullOrEmpty(hkey) && _expanded.Contains(hkey);

        var row = new Border();
        row.BorderThickness = new Thickness(1);
        row.BorderBrush = Border; row.Background = CardBg;
        row.CornerRadius = new CornerRadius(Theme.RadCard);
        row.Padding = new Thickness(16, 8, 16, 8); row.Margin = new Thickness(8, 3, 8, 3);

        var col = new StackPanel();
        var dp = new DockPanel();
        // chevron disclosure (only when there is a key to toggle)
        if (!string.IsNullOrEmpty(hkey))
        {
            var chev = ChevronToggle(hkey, isOpen);
            DockPanel.SetDock(chev, Dock.Left);
            dp.Children.Add(chev);
        }
        string hcanon = status == "ready" ? "waiting" : status;
        var pill = internalControl
            ? Pill(_lang == 0 ? "内部制御" : "Internal control", "neutral")
            : Pill(Theme.StatusLabel(hcanon, _lang), Theme.StatusKind(hcanon));
        pill.Margin = new Thickness(0, 0, 5, 0);
        DockPanel.SetDock(pill, Dock.Left);
        dp.Children.Add(pill);
        var turns = new TextBlock();
        turns.Text = T("turn") + " " + I(e, "turn");
        turns.Foreground = Muted; turns.FontSize = 11.5;
        turns.VerticalAlignment = VerticalAlignment.Center;
        turns.HorizontalAlignment = HorizontalAlignment.Right;
        DockPanel.SetDock(turns, Dock.Right);
        dp.Children.Add(turns);
        // Collapsed: the concise headline (conv_title if captured, else derived) -- single line,
        // ellipsis-trimmed -- so History matches the live collapsed card instead of dumping the
        // full goal text. The long goal only appears when this row is explicitly expanded.
        var head = new TextBlock();
        string histSummary = S(e, "goal_summary");
        head.Text = !string.IsNullOrEmpty(histSummary)
            ? histSummary : CardTitle(S(e, "conv_title"), S(e, "goal"));
        head.Foreground = Fg; head.FontSize = 13;
        head.VerticalAlignment = VerticalAlignment.Center;
        head.TextTrimming = TextTrimming.CharacterEllipsis;
        dp.Children.Add(head);
        col.Children.Add(dp);

        if (isOpen)
        {
            // expanded: full goal, wrapped, as muted selectable text (mirrors the live card)
            var g = new TextBox();
            g.Text = S(e, "goal");
            g.Foreground = Muted; g.FontSize = 12.5;
            g.IsReadOnly = true; g.BorderThickness = new Thickness(0);
            g.Background = Brushes.Transparent; g.Padding = new Thickness(0);
            g.IsTabStop = false; g.TextWrapping = TextWrapping.Wrap;
            g.Margin = new Thickness(0, 6, 0, 2);
            SwallowMouseUp(g);
            col.Children.Add(g);
            if (!string.IsNullOrEmpty(S(e, "submitter")))
                col.Children.Add(new TextBlock { Text = (_lang == 0 ? "提出者: " : "Submitted by: ") + S(e, "submitter"),
                                                 Foreground = Muted, FontSize = 11.5, Margin = new Thickness(0, 2, 0, 0) });
            if (!string.IsNullOrEmpty(S(e, "transcript"))) col.Children.Add(ReviewerLedgerPanel(S(e, "transcript")));

            // "続ける" (Continue): send a FOLLOW-UP instruction to this finished task, carrying its
            // prior context. Launches a FRESH fleet run whose goal PREPENDS the prior goal + a note
            // to re-read the on-disk outputs (no reopenable URL for this agent). Capture e's values
            // into locals BEFORE the delegate for closure-safety (same pattern as the row handler).
            string contPrior = S(e, "goal");
            string contConv = S(e, "conv_url");
            var contBtn = new Button();
            contBtn.Content = _lang == 0 ? "続ける" : "Continue";
            contBtn.Cursor = Cursors.Hand;
            contBtn.FontSize = 12;
            contBtn.Padding = new Thickness(12, 3, 12, 3);
            contBtn.Margin = new Thickness(0, 6, 0, 0);
            contBtn.HorizontalAlignment = HorizontalAlignment.Right;
            contBtn.BorderThickness = new Thickness(1);
            contBtn.Background = BtnBg;
            contBtn.BorderBrush = Border;
            contBtn.Foreground = Fg;
            contBtn.Template = FlatButtonTemplate();
            contBtn.Click += delegate
            {
                string fu = PromptFollowup();
                if (string.IsNullOrEmpty(fu)) return;
                string goalText = ContinueText(contPrior, fu, contConv);
                // The continuation goal is MULTI-LINE; SpawnFleet's GoalsToJsonl now escapes a
                // plain multi-line goal string safely on its own, so no manual serialization is
                // needed for the common case. Only pre-serialize here when there's an EXTRA key
                // beyond "text" to carry (resume_conv) -- GoalsToJsonl detects an already-JSON
                // goal string and passes it through as-is instead of double-wrapping it.
                var glist = new List<string>();
                if (!string.IsNullOrEmpty(contConv))
                {
                    var gd = new Dictionary<string, object>();
                    gd["text"] = goalText;
                    gd["resume_conv"] = contConv;
                    glist.Add(_js.Serialize(gd));
                }
                else
                {
                    glist.Add(goalText);
                }
                SpawnFleet(glist, "continue_input.txt");
                if (_startNote != null)
                {
                    _startNote.Text = _lang == 0
                        ? "前回タスクの続きを開始しました（成果物を読み直してから追加指示を実行します）。"
                        : "Started a continuation of the prior task (it re-reads the saved outputs, then runs the follow-up).";
                }
            };
            if (!internalControl) col.Children.Add(contBtn);
        }

        row.Child = col;
        // A history row is openable when we have ANY way to reach its body: a real conv_url, the
        // disk transcript path, or the worker name (the chat resolves the .jsonl by name). conv_url
        // is empty in the final snapshot / for the new agent, so keying ONLY on it left completed
        // conversations un-clickable -- the body was on disk the whole time. (friction #20)
        string htx = S(e, "transcript"), hnm = S(e, "name");
        if (!string.IsNullOrEmpty(conv) || !string.IsNullOrEmpty(htx) || !string.IsNullOrEmpty(hnm))
        {
            row.Cursor = Cursors.Hand;
            string url = conv, tx = htx, nm = hnm;
            row.MouseLeftButtonUp += delegate { FlashOpen(row); OpenHistory(nm, url, tx); };
        }
        return row;
    }

    // Pass A2-1: true when a status is a terminal-but-unresolved attention lane (stuck/maxturns/error).
    // These get the "recovery surface" treatment on their collapsed row (§6 spec).
    static bool IsAttentionStatus(string status)
    {
        return status == "stuck" || status == "maxturns" || status == "error";
    }

    static bool IsAgentSetupRuntimeWait(string reason)
    {
        if (string.IsNullOrWhiteSpace(reason)) return false;
        return reason.IndexOf("Agent Instructions are missing or stale", StringComparison.OrdinalIgnoreCase) >= 0
            && reason.IndexOf("browser answered RUN without a SQLite commit", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    static bool IsLocalLoopControlGoal(string goal)
    {
        string norm = (goal ?? "").Replace("\r", " ").Replace("\n", " ").Replace("\t", " ").Trim().ToLowerInvariant();
        while (norm.Contains("  ")) norm = norm.Replace("  ", " ");
        return norm.StartsWith("local_loop run ")
            || norm.StartsWith("local_loop bootstrap ")
            || norm.StartsWith("local_loop protocol ")
            || norm.StartsWith("execute local_loop job ")
            || norm.StartsWith("run local_loop job ");
    }

    static bool IsOperatorAttention(Dictionary<string, object> w)
    {
        return w != null
            && !IsLocalLoopControlGoal(S(w, "goal"))
            && !IsInfraStuck(w)
            && IsAttentionStatus(S(w, "status"));
    }

    static bool IsRetryableWorker(Dictionary<string, object> w)
    {
        if (w == null) return false;
        // The worker's own verdict beats the outcome-wide rule: `retryable: false` is written when
        // re-running could duplicate work (e.g. an ambiguous fresh submit, outcome STUCK).
        object rv;
        if (w.TryGetValue("retryable", out rv) && rv is bool && !(bool)rv) return false;
        return !IsLocalLoopControlGoal(S(w, "goal"))
            && IsRetryableOutcome(S(w, "outcome"));
    }

    // One key per worker instance (jid when the runner minted one, else name + run_id), so a
    // terminal STUCK worker is re-queued at most once per cockpit session.
    static string RetryWorkerKey(Dictionary<string, object> w)
    {
        string jid = S(w, "jid");
        if (!string.IsNullOrEmpty(jid)) return "jid:" + jid;
        return "w:" + S(w, "name") + "|" + S(w, "run_id");
    }

    // True when this terminal worker has already been re-queued (by us, or by the runner), or an
    // identical goal is queued/running now -- i.e. another copy exists and must not be added.
    bool RetryAlreadyCovered(Dictionary<string, object> root, Dictionary<string, object> w)
    {
        if (_autoRetriedWorkers.Contains(RetryWorkerKey(w))) return true;
        object rq;
        if (w.TryGetValue("retry_queued", out rq) && rq is bool && (bool)rq) return true;
        string goal = S(w, "goal");
        object wo;
        if (root != null && root.TryGetValue("workers", out wo) && wo is object[])
        {
            foreach (object o in (object[])wo)
            {
                var x = o as Dictionary<string, object>;
                if (x == null || object.ReferenceEquals(x, w)) continue;
                if (IsTerminalWorker(x)) continue;
                if (S(x, "goal") == goal) return true;
            }
        }
        return false;
    }

    // P0: an INFRA_STUCK worker is NOT a task failure — the engine parked it because the infra
    // (Edge session / sign-in / connector) broke. It gets a distinct ORANGE インフラ待ち pill,
    // is excluded from failure/miss counts, and its `reason` (actionable) is shown as-is.
    static bool IsInfraStuck(Dictionary<string, object> w)
    {
        return string.Equals(S(w, "outcome"), "INFRA_STUCK", StringComparison.OrdinalIgnoreCase);
    }

    // Build a small outline button for the attention recovery row (§6: equal-weight neutral buttons).
    Button AttentionBtn(string label)
    {
        var b = new Button();
        b.Content = label;
        b.Cursor = Cursors.Hand;
        b.FontSize = 12;
        b.Padding = new Thickness(12, 3, 12, 3);
        b.Margin = new Thickness(0, 0, 6, 0);
        b.BorderThickness = new Thickness(1);
        b.Background = Brushes.Transparent;
        b.BorderBrush = Border;
        b.Foreground = Muted;
        b.Template = FlatButtonTemplate();
        return b;
    }

    // One canonical user-facing result string for the expanded result panel, clipboard, and
    // artifact detection. Keeping these entry points together prevents the menu from copying raw
    // runner chatter while the card displays the cleaned final answer.
    string WorkerResultText(Dictionary<string, object> w, bool terminal)
    {
        string displayResult = w != null ? S(w, "display_result") : "";
        if (!string.IsNullOrEmpty(displayResult)) return displayResult;

        string last = w != null ? S(w, "last") : "";
        if (!string.IsNullOrEmpty(last))
        {
            string cleaned = CleanAgentResultForUi(last);
            return !string.IsNullOrEmpty(cleaned)
                ? cleaned
                : (_lang == 0 ? "結果を受信しました" : "Result received");
        }

        string outcome = w != null ? S(w, "outcome") : "";
        return terminal ? OutcomeLabel(outcome) : (_lang == 0 ? "実行中…" : "Working…");
    }

    void CopyWorkerResult(string result)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(result)) throw new InvalidOperationException();
            Clipboard.SetText(result);
            ShowScaleToast(T("copy_result_done"));
        }
        catch (Exception) { ShowScaleToast(T("copy_result_fail")); }
    }

    // ── Split group (分割グループ) line on a fan-out parent card ─────────────────────────────
    // `groups` in status.json is written by relay/family_view.py (contract in its docstring).
    // OWNER RULES: never the long goal text -- only the capped `ledger` strings it carries;
    // plain Japanese wording; one small line, no popup, no toast. Read-only.
    Dictionary<string, object> GroupOfParent(Dictionary<string, object> w)
    {
        if (w == null || _lastRoot == null) return null;
        object go;
        if (!_lastRoot.TryGetValue("groups", out go) || !(go is object[])) return null;
        string nm = S(w, "name");
        if (nm.Length == 0) return null;
        foreach (object o in (object[])go)
        {
            var g = o as Dictionary<string, object>;
            var par = g != null ? Obj(g, "parent") : null;
            if (par != null && S(par, "name") == nm) return g;
        }
        return null;
    }

    static string GroupCount(Dictionary<string, object> ch, string key, string label)
    {
        int n = I(ch, key);
        return n > 0 ? label + " " + n : "";
    }

    // One line: 分割グループ 子N件: 待機 a · 実行中 b · ... / 統合: <label> [/ 下位グループ n件]
    // The words live in GroupTreeView (EffortPolicy.cs, tested by a compiled harness).
    string GroupLineText(Dictionary<string, object> w)
    {
        return GroupTreeView.Line(GroupOfParent(w), _lang == 0);
    }

    // The second line of a root group: tree usage + the depth-cap note. "" when not reported.
    string GroupExtraText(Dictionary<string, object> w, out int level)
    {
        level = 0;
        var g = GroupOfParent(w);
        if (g == null || _lastRoot == null) return "";
        return GroupTreeView.ExtraText(_lastRoot, g, _lang == 0, out level) ?? "";
    }

    string GroupExtraSig(Dictionary<string, object> w)
    {
        int lv;
        string x = GroupExtraText(w, out lv);
        return x.Length == 0 ? "" : "|x" + lv + x;
    }

    // Cards whose usage line the person collapsed (click the group line). Not persisted.
    readonly HashSet<string> _groupExtraHidden = new HashSet<string>();

    UIElement BuildGroupLine(Dictionary<string, object> w)
    {
        string text = GroupLineText(w);
        if (text.Length == 0) return null;
        var g = GroupOfParent(w);
        var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(24 + GroupTreeView.IndentPx(g), 3, 0, 0) };
        int xLevel;
        string extra = GroupExtraText(w, out xLevel);
        TextBlock extraTb = null;
        UIElement barRow = null;
        string cardName = S(w, "name");
        row.MouseLeftButtonUp += delegate (object s2, MouseButtonEventArgs e2)
        {
            e2.Handled = true;
            if (extraTb == null) return;
            bool hide = extraTb.Visibility == Visibility.Visible;
            extraTb.Visibility = hide ? Visibility.Collapsed : Visibility.Visible;
            if (barRow != null) barRow.Visibility = extraTb.Visibility;
            if (hide) _groupExtraHidden.Add(cardName); else _groupExtraHidden.Remove(cardName);
        };
        row.Children.Add(new TextBlock {
            Text = text, Foreground = Muted, FontSize = 12,
            VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis,
            TextWrapping = TextWrapping.NoWrap, MaxWidth = 560 });
        // Constraint tokens (dates, quoted names, amounts) from the capped ledger: small chips.
        var led = Obj(g, "ledger");
        int shown = 0;
        var tip = new List<string>();
        if (led != null)
        {
            if (S(led, "task").Length > 0) tip.Add(S(led, "task"));
            object co, to;
            if (led.TryGetValue("constraints", out co) && co is object[])
                foreach (object c in (object[])co) tip.Add("- " + (c != null ? c.ToString() : ""));
            if (led.TryGetValue("tokens", out to) && to is object[])
                foreach (object t in (object[])to)
                {
                    string ts = t != null ? t.ToString() : "";
                    if (ts.Length == 0 || shown >= 4) continue;
                    var chip = Pill(ts, "neutral");
                    chip.Margin = new Thickness(8, 0, 0, 0);
                    row.Children.Add(chip);
                    shown++;
                }
        }
        if (tip.Count > 0) row.ToolTip = string.Join("\n", tip.ToArray());
        if (extra.Length == 0) return row;
        extraTb = new TextBlock {
            Text = extra, FontSize = 12, Margin = new Thickness(24 + GroupTreeView.IndentPx(g), 1, 0, 0),
            Foreground = xLevel >= 2 ? Theme.Br(Theme.Danger(_dark))
                         : xLevel == 1 ? Theme.Br(Theme.Warning(_dark)) : Muted,
            TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap, MaxWidth = 560,
            Visibility = _groupExtraHidden.Contains(cardName) ? Visibility.Collapsed : Visibility.Visible };
        var both = new StackPanel { Orientation = Orientation.Vertical };
        both.Children.Add(row);
        both.Children.Add(extraTb);
        var barsAll = new StackPanel { Orientation = Orientation.Vertical, Visibility = extraTb.Visibility };
        var turnsBar = BuildBudgetBar(w, Visibility.Visible);
        if (turnsBar != null) barsAll.Children.Add(turnsBar);
        // The other ledger dimensions (workers, concurrent, minutes), additive under the turns bar.
        int[] moreIdx = { 0, 1, 3 };
        for (int mi = 0; mi < moreIdx.Length; mi++)
        {
            var more = BuildBudgetBarAt(w, moreIdx[mi]);
            if (more != null) barsAll.Children.Add(more);
        }
        if (barsAll.Children.Count > 0) { barRow = barsAll; both.Children.Add(barRow); }
        return both;
    }

    // Turns used against the tree's turn cap: a thin bar and "turns 12 / 40". Display only; the
    // words and the fraction come from GroupTreeView.BudgetBar. null when no cap is reported.
    UIElement BuildBudgetBar(Dictionary<string, object> w, Visibility vis)
    {
        var g = GroupOfParent(w);
        if (g == null || _lastRoot == null) return null;
        double frac; int level;
        string text = GroupTreeView.BudgetBar(_lastRoot, g, _lang == 0, out frac, out level);
        if (text == null) return null;
        var fill = level >= 2 ? Theme.Br(Theme.Danger(_dark))
                   : level == 1 ? Theme.Br(Theme.Warning(_dark)) : Theme.Br(Theme.Info(_dark));
        const double width = 120;
        var track = new Grid { Width = width, Height = 6, VerticalAlignment = VerticalAlignment.Center };
        track.Children.Add(new Border { Background = Theme.Br(Theme.Border(_dark)), CornerRadius = new CornerRadius(3) });
        track.Children.Add(new Border {
            Background = fill, CornerRadius = new CornerRadius(3),
            Width = Math.Max(frac > 0 ? 3 : 0, width * frac), HorizontalAlignment = HorizontalAlignment.Left });
        var bar = new StackPanel {
            Orientation = Orientation.Horizontal, Visibility = vis,
            Margin = new Thickness(24 + GroupTreeView.IndentPx(g), 2, 0, 0) };
        bar.Children.Add(track);
        bar.Children.Add(new TextBlock {
            Text = text, FontSize = 12, Margin = new Thickness(8, 0, 0, 0), Foreground = Muted,
            VerticalAlignment = VerticalAlignment.Center });
        System.Windows.Automation.AutomationProperties.SetAutomationId(bar, "budgetBar_turns");
        return bar;
    }

    // One more ledger dimension as a bar (idx: 0 total, 1 active, 3 minutes), same drawing and
    // colouring as the turns bar. Display only; null when that dimension has no cap reported.
    UIElement BuildBudgetBarAt(Dictionary<string, object> w, int idx)
    {
        var g = GroupOfParent(w);
        if (g == null || _lastRoot == null) return null;
        double frac; int level;
        string text = GroupTreeView.BudgetBarAt(_lastRoot, g, idx, _lang == 0, out frac, out level);
        if (text == null) return null;
        var fill = level >= 2 ? Theme.Br(Theme.Danger(_dark))
                   : level == 1 ? Theme.Br(Theme.Warning(_dark)) : Theme.Br(Theme.Info(_dark));
        const double width = 120;
        var track = new Grid { Width = width, Height = 6, VerticalAlignment = VerticalAlignment.Center };
        track.Children.Add(new Border { Background = Theme.Br(Theme.Border(_dark)), CornerRadius = new CornerRadius(3) });
        track.Children.Add(new Border {
            Background = fill, CornerRadius = new CornerRadius(3),
            Width = Math.Max(frac > 0 ? 3 : 0, width * frac), HorizontalAlignment = HorizontalAlignment.Left });
        var bar = new StackPanel {
            Orientation = Orientation.Horizontal,
            Margin = new Thickness(24 + GroupTreeView.IndentPx(g), 2, 0, 0) };
        bar.Children.Add(track);
        bar.Children.Add(new TextBlock {
            Text = text, FontSize = 12, Margin = new Thickness(8, 0, 0, 0), Foreground = Muted,
            VerticalAlignment = VerticalAlignment.Center });
        System.Windows.Automation.AutomationProperties.SetAutomationId(bar, "budgetBar_" + FanoutBudgetView.ReportKeys[idx]);
        return bar;
    }

    Border Card(Dictionary<string, object> w)
    {
        string name = S(w, "name");
        string goal = S(w, "goal");
        string goalSummary = S(w, "goal_summary");
        string rawStatus = S(w, "status");
        string status = rawStatus == "ready" ? "waiting" : rawStatus;   // canonical (runner emits fine states)
        string reason = S(w, "reason");
        string last = S(w, "last");
        string conv = S(w, "conv_url");
        string convTitle = S(w, "conv_title");
        int turn = I(w, "turn");
        int reviews = I(w, "verify_attempts");
        bool verifiedOk = string.Equals(S(w, "verified"), "True", StringComparison.OrdinalIgnoreCase);
        bool closed = string.Equals(S(w, "closed"), "True", StringComparison.OrdinalIgnoreCase);
        bool terminal = status == "done" || status == "stuck" || status == "maxturns"
                        || status == "error" || status == "cancelled";
        bool isOpen = _expanded.Contains(name);
        bool internalControl = IsLocalLoopControlGoal(goal);
        // P0: INFRA_STUCK is an infra pause (Edge/sign-in/connector broke), NOT a task failure. It
        // gets the distinct ORANGE インフラ待ち treatment: a warning rail/pill, its actionable reason
        // shown as-is, and a 再投入 re-queue action — NOT the red stuck/error recovery surface.
        bool isInfra = !closed && !internalControl && IsInfraStuck(w);
        bool isLocalRuntimeWait = !closed
            && status == "waiting_runtime"
            && string.Equals(S(w, "execution_profile"), "LOCAL_LOOP", StringComparison.OrdinalIgnoreCase)
            && string.Equals(S(w, "runtime_resume_allowed"), "True", StringComparison.OrdinalIgnoreCase);
        bool isAgentSetupWait = isLocalRuntimeWait && IsAgentSetupRuntimeWait(reason);
        bool isLocalLoop = string.Equals(
            S(w, "execution_profile"), "LOCAL_LOOP", StringComparison.OrdinalIgnoreCase);
        bool localSteerBlocked = isLocalLoop && (status == "waiting_user"
            || status == "waiting_external" || status == "needs_routing");
        // Attention lane: stuck/maxturns/error and NOT yet expanded -- gets recovery surface treatment.
        // INFRA_STUCK is carved out of the red attention lane (handled by its own infra branch).
        bool isAttention = !closed && IsOperatorAttention(w);

        // The chip carries the status. There used to be a 3px coloured rail down the left edge of
        // every row as well, and removing it cost nothing measurable: the chip sits at a nearly
        // constant x a few pixels in, so scanning the column for "which row needs me" reads the
        // chip, not the rail thirty pixels to its left. The rail restated what the chip already
        // said, in the one shape the operator has objected to for months.
        bool isDone = status == "done" || string.Equals(S(w, "outcome"), "DONE", StringComparison.OrdinalIgnoreCase);
        string chipKind = internalControl ? "neutral"
            : (isDone ? "success" : (isInfra ? "warning" : Theme.StatusKind(status)));
        // Fan-out PARENT: its real status is done/FANOUT on purpose, which reads as "finished"
        // while its children still run. The coordinator-side derived display_label (waiting etc.)
        // replaces the chip text only; status/outcome are untouched.
        string parentLabel = internalControl ? "" : S(w, "display_label");
        if (parentLabel.Length > 0)
        {
            string pds = S(w, "display_state");
            chipKind = pds == "merge_failed" ? "danger"
                     : (pds == "interrupted" ? "warning" : (pds == "done" ? "success" : "info"));
        }

        // Pass A2-1 TASK 1: demote the collapsed row to a LEDGER ROW.
        // - No rounded corners, no card background fill, no full border.
        // - Flat row on the app surface: bottom divider only (Theme.Border).
        // - Tighter vertical padding so rows are denser (~64-76px collapsed).
        var card = new Border();
        card.Tag = name;                // lets a chevron toggle find & replace just this card
        // Ledger row: only a bottom hairline divider (no rounded card border).
        card.BorderThickness = new Thickness(0, 0, 0, 1);
        card.BorderBrush = Border;
        // Transparent background (inherits the flat app surface).
        card.Background = Brushes.Transparent;
        // No margin between rows; the bottom border IS the separator.
        card.Margin = new Thickness(0, 0, 0, 0);

        var shell = new Grid();
        shell.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        // 12px on the left where the rail's 3px plus the old 10px inset used to sit, so the text
        // column does not move now that the rail is gone.
        var col = new StackPanel { Margin = new Thickness(12, 7, 12, 7) };
        Grid.SetColumn(col, 0); shell.Children.Add(col);

        // ── line 1: [chevron] [chip] [title* ........] [Open] [primary action | kebab⋮] ──
        // Fixed right-action width (~112px) so long titles never collide with actions.
        var top = new Grid();
        top.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });  // left: chevron+chip+title
        top.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(112, GridUnitType.Pixel) }); // right: actions

        // Right action area (fixed 112px): Open + primary action or kebab.
        var right = new StackPanel { Orientation = Orientation.Horizontal,
                                     HorizontalAlignment = HorizontalAlignment.Right,
                                     VerticalAlignment = VerticalAlignment.Center };
        {
            var openLink = new TextBlock();
            openLink.Text = _lang == 0 ? "開く" : "Open";
            openLink.Foreground = Muted; openLink.FontSize = 12;
            openLink.VerticalAlignment = VerticalAlignment.Center; openLink.Cursor = Cursors.Hand;
            openLink.Margin = new Thickness(0, 0, 8, 0);
            openLink.ToolTip = _lang == 0 ? "この会話をメインチャットで開く" : "Open this conversation in the chat";
            string onm = name; string ourl = conv;
            openLink.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; FlashOpen(card); OpenWorker(onm, ourl); };
            right.Children.Add(openLink);
        }
        if (isLocalRuntimeWait)
        {
            var runtimeResume = AttentionBtn(_lang == 0 ? "再開" : "Resume");
            runtimeResume.ToolTip = _lang == 0
                ? "実行環境を修復した後、同じ永続タスクを再開します"
                : "Resume the same durable task after repairing the runtime";
            Dictionary<string, object> runtimeWaitWorker = w;
            runtimeResume.Click += delegate (object s2, RoutedEventArgs e2)
            {
                e2.Handled = true;
                ResumeLocalLoopRuntime(runtimeWaitWorker);
            };
            right.Children.Add(runtimeResume);
        }
        else if (closed || terminal)
        {
            // Completed/released Fleet card menu: keep the kebab available after its agent tab is
            // released. The result and artifacts remain useful, and the same goal can be run again.
            // Reuse actions come first; the cleanup action sits after a separator.
            var wt2 = w;
            string menuResult = WorkerResultText(wt2, true);
            var artifactPaths = DetectExistingPaths(menuResult);
            var menuLabels = new List<string>();
            var menuActions = new List<Action>();

            string copyText = menuResult;
            menuLabels.Add(T("copy_result"));
            menuActions.Add(delegate { CopyWorkerResult(copyText); });

            if (artifactPaths.Count > 0)
            {
                string firstArtifact = artifactPaths[0];
                menuLabels.Add(T("reveal_artifacts"));
                menuActions.Add(delegate {
                    ShowScaleToast(RevealPath(firstArtifact) ? T("reveal_artifacts_done") : T("reveal_artifacts_fail"));
                });
            }

            if (!IsLocalLoopControlGoal(goal))
            {
                menuLabels.Add(T("rerun_same"));
                menuActions.Add(delegate { RetryGoal(wt2); ShowScaleToast(T("rerun_started")); });
            }

            menuLabels.Add(null);
            menuActions.Add(null);
            menuLabels.Add("→ " + T("to_history"));
            menuActions.Add(delegate { ArchiveAndHide(wt2); });
            right.Children.Add(CardKebabBtn(menuLabels.ToArray(), menuActions.ToArray(), w));
        }
        else
        {
            // running (non-terminal) card: release stays VISIBLE (single-click, never hidden)
            var relBtn = new Button();
            relBtn.Content = MakeReleaseContent();
            relBtn.Cursor = Cursors.Hand; relBtn.BorderThickness = new Thickness(1);
            relBtn.Background = BtnBg; relBtn.BorderBrush = Border; relBtn.Foreground = Fg;
            relBtn.Padding = new Thickness(6, 2, 8, 2);
            relBtn.ToolTip = _lang == 0 ? "このタスクを停止してタブを解放（fleet 停止中ならカードを片付け）"
                                        : "Stop this task and release its tab (clears the card if the fleet is stopped)";
            string nm = name; var wt2 = w;
            relBtn.Click += delegate {
                RequestClose(nm);
                if (_lastRoot != null
                    && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                    && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
                    ArchiveAndHide(wt2);
            };
            right.Children.Add(relBtn);
        }
        Grid.SetColumn(right, 1); top.Children.Add(right);

        // left cluster: chevron + status chip, then the title fills the rest (1 line, ellipsis)
        var left = new DockPanel { LastChildFill = true };
        var chev = ChevronToggle(name, isOpen); DockPanel.SetDock(chev, Dock.Left); left.Children.Add(chev);
        // INFRA_STUCK -> distinct ORANGE インフラ待ち pill; otherwise the normal status label.
        var chip = Pill(internalControl
            ? (_lang == 0 ? "内部制御" : "Internal control")
            : (isAgentSetupWait ? (_lang == 0 ? "エージェント設定待ち" : "Agent setup required")
                : (isInfra ? T("infra_wait") : (parentLabel.Length > 0 ? parentLabel : Theme.StatusLabel(status, _lang)))), chipKind);
        chip.Margin = new Thickness(2, 0, 5, 0);
        if (status == "interrupted" && !string.IsNullOrEmpty(reason)) chip.ToolTip = reason;
        DockPanel.SetDock(chip, Dock.Left); left.Children.Add(chip);
        // AGENT BADGE (P0 feature 4): which agent this conversation is bound to. Green subtle badge
        // for the configured agent, WARNING-colored 既定Copilot badge for a plain /chat/ (default) url.
        var agentBadge = BuildAgentBadge(conv, convTitle);
        if (agentBadge != null) { DockPanel.SetDock(agentBadge, Dock.Left); left.Children.Add(agentBadge); }
        // EFFORT BADGE: only when the runner sent effort_level (policy not off); absent = none.
        var effBadge = BuildEffortBadge(w);
        if (effBadge != null) { DockPanel.SetDock(effBadge, Dock.Left); left.Children.Add(effBadge); }
        string headline = !string.IsNullOrEmpty(goalSummary)
            ? goalSummary : CardTitle(convTitle, goal);
        var ht = new TextBlock {
            Text = headline, Foreground = Fg, FontSize = 13.5, FontWeight = FontWeights.SemiBold,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap
        };
        left.Children.Add(ht);
        Grid.SetColumn(left, 0); top.Children.Add(left);
        col.Children.Add(top);
        UIElement groupLine = BuildGroupLine(w);
        if (groupLine != null) col.Children.Add(groupLine);

        if (!isOpen)
        {
            // ── COLLAPSED ROW body (ledger row, not expanded drawer) ──────────────────────────
            if (isLocalRuntimeWait)
            {
                if (!string.IsNullOrEmpty(reason))
                {
                    var runtimeReason = new TextBlock
                    {
                        Text = (isAgentSetupWait ? (_lang == 0 ? "エージェント設定待ち: " : "Agent setup required: ")
                            : (_lang == 0 ? "実行環境待ち: " : "Runtime paused: ")) + OneLine(reason),
                        Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(runtimeReason);
                }
            }
            else if (isInfra)
            {
                // P0 INFRA_STUCK collapsed row: the reason text is actionable (e.g. "sign-in
                // required" / "default-Copilot fallback"), so render it verbatim, then offer a
                // 再投入 action reusing the existing retry path. NOT a red error surface.
                if (!string.IsNullOrEmpty(reason))
                {
                    var ir = new TextBlock
                    {
                        Text = OneLine(reason),
                        Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(ir);
                }
                var infraRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(24, 6, 0, 2) };
                infraRow.MouseLeftButtonUp += delegate (object s2, MouseButtonEventArgs e2) { e2.Handled = true; };
                // 再投入 (Re-queue): reuse RetryGoal (add_goal if live, else respawn) — the same
                // affordance stopped goals already use, per spec ("reuse the existing retry path").
                var requeueBtn = AttentionBtn(T("infra_retry"));
                requeueBtn.ToolTip = _lang == 0
                    ? "インフラ復旧後にこのゴールを再投入します（実行中なら add_goal、停止済なら fleet を再起動）"
                    : "Re-queue this goal after the infra recovers (add_goal if live, else respawn fleet)";
                Dictionary<string, object> wInfra = w;
                requeueBtn.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; RetryGoal(wInfra); };
                infraRow.Children.Add(requeueBtn);
                // Open-conversation shortcut so the user can inspect the parked lane.
                var infraOpen = AttentionBtn(_lang == 0 ? "会話を開く" : "Open conversation");
                string infraNm = name; string infraUrl = conv;
                infraOpen.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; FlashOpen(card); OpenWorker(infraNm, infraUrl); };
                infraRow.Children.Add(infraOpen);
                col.Children.Add(infraRow);
            }
            else if (isAttention)
            {
                // Pass A2-1 TASK 2: RECOVERY ROW for attention lanes (stuck/maxturns/error).
                // §6: "Not an error card — a recovery surface."

                // Line 2: human recovery sentence from the `reason` field [REAL].
                // Calm lead-in + verbatim reason text. Omit when reason is empty.
                if (!string.IsNullOrEmpty(reason))
                {
                    string leadIn = _lang == 0 ? "停止理由: " : "Stopped: ";
                    var rl2 = new TextBlock
                    {
                        Text = leadIn + OneLine(reason),
                        Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(rl2);
                }

                // Line 3: freshness — "最終更新 {N} 前" / "last update {N} ago" [COMPUTED].
                // Best proxy: transcript meta ts (start of this worker); fallback to status updated.
                {
                    string transcriptPath2 = S(w, "transcript");
                    double evidenceTs = ReadTranscriptStartTs(transcriptPath2);
                    // If transcript ts unavailable, fall back to the status.json top-level updated field.
                    if (evidenceTs <= 0 && _lastRoot != null)
                        evidenceTs = Dbl(_lastRoot, "updated");
                    string freshnessText;
                    if (evidenceTs > 0)
                    {
                        double age = NowUnix() - evidenceTs;
                        if (age < 0) age = 0;
                        freshnessText = _lang == 0
                            ? ("最終更新 " + Fmt(age) + " 前")
                            : ("last update " + Fmt(age) + " ago");
                    }
                    else
                    {
                        freshnessText = _lang == 0 ? "最終更新: 不明" : "last update: unknown";
                    }
                    var ml3 = new TextBlock
                    {
                        Text = freshnessText,
                        Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 12,
                        Margin = new Thickness(24, 2, 0, 0)
                    };
                    col.Children.Add(ml3);
                }

                // Recovery actions (§6): [再開]/[Resume], [会話を開く]/[Open conversation], [停止]/[Stop].
                // Equal-weight outline buttons; NO accent color.
                var recov = new StackPanel
                {
                    Orientation = Orientation.Horizontal,
                    Margin = new Thickness(24, 6, 0, 2)
                };
                recov.MouseLeftButtonUp += delegate (object s2, MouseButtonEventArgs e2) { e2.Handled = true; };

                // [再開]/[Resume]: fall back to RetryGoal (re-queues or re-spawns via existing path).
                var resumeBtn = AttentionBtn(_lang == 0 ? "再開" : "Resume");
                resumeBtn.ToolTip = _lang == 0
                    ? "このタスクを再開・再試行します（実行中なら add_goal、停止済なら fleet を再起動）"
                    : "Resume or retry this task (add_goal if fleet live, else respawn fleet)";
                Dictionary<string, object> wResume = w;
                resumeBtn.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; RetryGoal(wResume); };
                recov.Children.Add(resumeBtn);

                // [会話を開く]/[Open conversation]: open the conversation/transcript in the main chat.
                var evidBtn = AttentionBtn(_lang == 0 ? "会話を開く" : "Open conversation");
                evidBtn.ToolTip = _lang == 0
                    ? "この会話を開いて最後のやり取りを確認する"
                    : "Open this conversation to review the last exchange";
                string evidNm = name; string evidUrl = conv;
                evidBtn.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; FlashOpen(card); OpenWorker(evidNm, evidUrl); };
                recov.Children.Add(evidBtn);

                // [停止]/[Stop]: release/archive this lane via existing mechanisms.
                var stopBtn = AttentionBtn(_lang == 0 ? "停止" : "Stop");
                stopBtn.ToolTip = _lang == 0
                    ? "このレーンを停止して履歴へ移動する"
                    : "Stop this lane and move to history";
                string stopNm = name; Dictionary<string, object> wStop = w;
                stopBtn.Click += delegate (object s2, RoutedEventArgs e2)
                {
                    e2.Handled = true;
                    RequestClose(stopNm);
                    if (_lastRoot != null
                        && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                        && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
                        ArchiveAndHide(wStop);
                    else
                        ArchiveAndHide(wStop);
                };
                recov.Children.Add(stopBtn);

                col.Children.Add(recov);
            }
            else
            {
                // ── Normal collapsed ledger row: line 2 + line 3 ────────────────────────────
                // Line 2: latest human-readable progress. Precedence: display_result > last > fallback.
                var execution = Obj(w, "execution");
                if (execution != null)
                {
                    string currentStep = S(execution, "current_step");
                    int currentIndex = I(execution, "current_step_index");
                    int totalSteps = I(execution, "total_steps");
                    // Open-ended LOCAL_LOOP has no turn_plan, so current_step is initially the
                    // authoritative goal itself. The headline already shows its compact task
                    // identity; repeating the full (often English, multi-kilobyte) goal here makes
                    // the collapsed card look like progress when it is only the original request.
                    bool currentStepRepeatsGoal = string.Equals(
                        (currentStep ?? "").Trim(), (goal ?? "").Trim(), StringComparison.Ordinal);
                    if (!string.IsNullOrEmpty(currentStep) && !currentStepRepeatsGoal)
                    {
                        string stepPrefix = totalSteps > 0
                            ? (currentIndex + "/" + totalSteps + "  ")
                            : (currentIndex + "  ");
                        var stepLine = new StackPanel { Orientation = Orientation.Horizontal,
                            Margin = new Thickness(24, 4, 0, 0) };
                        stepLine.Children.Add(MakeIcon("play_arrow", 13, Fg));
                        stepLine.Children.Add(new TextBlock
                        {
                            Text = stepPrefix + OneLine(currentStep),
                            Foreground = Fg, FontSize = 12.5, FontWeight = FontWeights.SemiBold,
                            TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap,
                            Margin = new Thickness(4, 0, 0, 0)
                        });
                        col.Children.Add(stepLine);
                    }
                }

                string collapsedDisplayResult = S(w, "display_result");
                string resultText;
                if (!string.IsNullOrEmpty(collapsedDisplayResult))
                    resultText = collapsedDisplayResult;
                else if (!string.IsNullOrEmpty(last))
                    resultText = last;
                else if (terminal || closed)
                    resultText = "";
                else if (status == "pending")
                    resultText = (_lang == 0 ? "待機中…" : "Queued…");
                else
                    resultText = (_lang == 0 ? "実行中…" : "Working…");

                if (!string.IsNullOrEmpty(resultText))
                {
                    string displayLine;
                    if (!string.IsNullOrEmpty(collapsedDisplayResult))
                    {
                        // display_result is already clean; just take the first line for the collapsed view
                        int nl2 = collapsedDisplayResult.IndexOf('\n');
                        string fl2 = nl2 >= 0 ? collapsedDisplayResult.Substring(0, nl2) : collapsedDisplayResult;
                        displayLine = OneLine(!string.IsNullOrEmpty(fl2) ? fl2 : collapsedDisplayResult);
                    }
                    else if (!string.IsNullOrEmpty(last))
                    {
                        string cleanedResult = CleanAgentResultForUi(last);
                        string firstLine = "";
                        if (!string.IsNullOrEmpty(cleanedResult))
                        {
                            int nl = cleanedResult.IndexOf('\n');
                            firstLine = nl >= 0 ? cleanedResult.Substring(0, nl) : cleanedResult;
                        }
                        displayLine = !string.IsNullOrEmpty(firstLine)
                            ? OneLine(firstLine)
                            : (_lang == 0 ? "結果を受信しました" : "Result received");
                    }
                    else
                    {
                        displayLine = OneLine(resultText);
                    }
                    var rl = new TextBlock
                    {
                        Text = displayLine, Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(rl);
                }

                // Line 3: meta — worker name · turn N · alive {freshness} ago · verified [COMPUTED].
                var meta = new StringBuilder();
                string transcriptPath = S(w, "transcript");
                double startTs = ReadTranscriptStartTs(transcriptPath);
                if (startTs > 0)
                {
                    double cardElapsed = NowUnix() - startTs;
                    if (cardElapsed > 0)
                        meta.Append(Fmt(cardElapsed)).Append(" · ");
                }
                meta.Append(name.ToUpper());
                if (turn > 0) meta.Append(" · ").Append(T("turn")).Append(' ').Append(turn);
                if (reviews > 0) meta.Append(" · ").Append(_lang == 0 ? ("確認 " + reviews + " 回") : ("reviewed " + reviews));
                if (verifiedOk) meta.Append(" · ").Append(_lang == 0 ? "検証OK" : "verified");
                if (execution != null)
                {
                    int completed = I(execution, "completed_count");
                    int totalSteps = I(execution, "total_steps");
                    int artifactCount = ArrCount(execution, "artifacts");
                    meta.Append(" · ").Append(_lang == 0 ? "完了 " : "done ").Append(completed);
                    if (totalSteps > 0) meta.Append('/').Append(totalSteps);
                    if (artifactCount > 0)
                        meta.Append(" · ").Append(artifactCount).Append(_lang == 0 ? " 成果物" : " artifacts");
                    double progressTs = Dbl(execution, "last_progress_at");
                    if (progressTs > 0)
                    {
                        double progressAge = Math.Max(0, NowUnix() - progressTs);
                        meta.Append(" · ").Append(_lang == 0 ? "進捗 " : "progress ")
                            .Append(Fmt(progressAge)).Append(_lang == 0 ? " 前" : " ago");
                    }
                }
                var ml = new TextBlock
                {
                    Text = meta.ToString(), Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 12,
                    Margin = new Thickness(24, 3, 0, 0)
                };
                col.Children.Add(ml);

                // TASK 3 (Bucket C): next_step + self_confidence [REAL — agent-emitted, shown as-is].
                string nextStep = S(w, "next_step");
                string selfConf = S(w, "self_confidence");

                // "→ 次: {next_step}" / "→ next: {next_step}"  — muted, 1 line ellipsis.
                if (!string.IsNullOrEmpty(nextStep))
                {
                    string nextPrefix = _lang == 0 ? "→ 次: " : "→ next: ";
                    var nextTb = new TextBlock
                    {
                        Text = nextPrefix + nextStep,
                        Foreground = Theme.Br(Theme.Muted(_dark)),
                        FontSize = 12,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 2, 0, 0)
                    };
                    col.Children.Add(nextTb);
                }

                // Self-confidence chip — clearly labeled "自己申告" (self-reported, not verified).
                // Neutral/muted styling; do NOT use a trust-signal color. Subtle level tint only.
                if (!string.IsNullOrEmpty(selfConf))
                {
                    string confLabel = _lang == 0
                        ? ("自己申告: " + selfConf)
                        : ("self-reported: " + selfConf);
                    // Subtle background tint by level (very muted -- not a trust signal).
                    // The six hex values used to be written here. They were the only colours in
                    // the UI outside Theme.cs, which is the file that exists because the palette
                    // had already drifted once from being duplicated at call sites.
                    string tintKind = selfConf == "high" ? "success"
                                    : (selfConf == "medium" ? "warning" : "danger");
                    string tintHex = Theme.KindTint(tintKind, _dark);

                    var confChip = new Border();
                    confChip.CornerRadius = new CornerRadius(Theme.RadChip);
                    confChip.Background = Theme.Br(tintHex);
                    confChip.Padding = new Thickness(6, 1, 6, 1);
                    confChip.Margin = new Thickness(24, 3, 0, 0);
                    confChip.HorizontalAlignment = HorizontalAlignment.Left;
                    var confTb = new TextBlock();
                    confTb.Text = confLabel;
                    confTb.FontSize = 11;
                    confTb.Foreground = Theme.Br(Theme.Muted(_dark));
                    confChip.Child = confTb;
                    col.Children.Add(confChip);
                }
            }

            // Feature 1: always-visible steer affordance -- reachable WITHOUT expanding the card.
            // Same terminal gate the expanded drawer's SteerRow/RetryRow/ContinueRow switch uses
            // (below): a terminal card (done/stuck/maxturns/error/cancelled) gets nothing here.
            if (!terminal && !localSteerBlocked) col.Children.Add(CollapsedSteerRow(name));
        }
        else
        {
            // ── EXPANDED DRAWER ───────────────────────────────────────────────────────────────
            // Expanded detail is organized into tabs (spec): Overview (default) / Conversation /
            // Review / Logs -- so raw transcript, refuter notes and internal fields no longer all
            // dump onto the surface at once. Heavy content is built ONLY when expanded.
            col.Children.Add(BuildCardTabs(w, name, goal, last, reason, terminal));
            // Actions live BELOW the tabs (not inside one) so steer/retry are always reachable.
            if (!terminal && !localSteerBlocked) col.Children.Add(SteerRow(name));
            else if (IsRetryableWorker(w)) col.Children.Add(RetryRow(w));
            else col.Children.Add(ContinueRow(name, goal, S(w, "conv_url")));
        }

        card.Child = shell;
        // FIX B: while a Stop is in flight (optimistic, not yet confirmed by the sweep), dim every
        // non-terminal card so the click reads as "taking effect" immediately. terminal cards
        // (already done/stuck/etc.) are left alone -- Stop doesn't touch them.
        card.Opacity = (_stopping && !terminal) ? 0.45 : 1.0;
        // Always clickable: open this worker in the main chat BY NAME, so it works even when the
        // Copilot conv_url was never captured (the main chat renders the live status.json snapshot
        // for this worker). conv_url is passed too so /history can fill in the full transcript
        // when it is available.
        card.Cursor = Cursors.Hand;
        string wname = name; string url = conv;
        card.MouseLeftButtonUp += delegate { FlashOpen(card); OpenWorker(wname, url); };
        card.ToolTip = _lang == 0 ? "クリックでこの会話をメインに表示" : "Click to open this conversation in the chat";
        return card;
    }

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

    //: How many recent entries the card lists. Three is the starting value, not a measurement --
    //: it is what fits beside the state and the controls without the card becoming the reading
    //: surface again. The rest are reachable by count and a link, never by scrolling here.
    const int MINI_THREAD_ENTRIES = 3;

    //: How much of an entry is shown so a reader can decide whether to open it. A line, not a
    //: paragraph: the question this answers is "is this worth reading", and a paragraph answers
    //: "what does it say", which is the other screen's job.
    const int MINI_THREAD_SUMMARY_CHARS = 110;

    // WHAT THE CARD SHOWS OF A CONVERSATION: enough to choose what to read, and no reading
    // surface of its own.
    //
    // THE DEFECT THIS REPLACES. Each turn was a TextBox capped at MaxHeight 90 with its own
    // scrollbar, inside a ScrollViewer capped at 240 with another, inside the card list's
    // scroller. Three scroll regions stacked in the same direction: to reach the second turn you
    // scrolled the first one to its end, then the panel, and the card moved under you while you
    // did it. Reported as 「開いてしまえば一番下までスクロールしないと次のが見られない」 --
    // read-shaped, not readable. An external review put the principle exactly: 「問題はスクロールの
    // 存在ではなく、同じ操作方向に異なる本文領域が重なっていること」.
    //
    // Raising the caps does not fix it and lowering them is the same thing smaller: a monitoring
    // card cannot host hundreds of lines without becoming the thing it was meant to summarise,
    // and one long-running worker would push every other card off the screen. So the card lists
    // WHAT HAPPENED and the full text opens where reading belongs -- the chat window, positioned
    // on the entry, which the review recommended over an in-card expansion (the card jumps),
    // a modal (it covers the monitor) and an unbounded body (one lane eats the view).
    //
    // Null when the transcript is empty or missing, as before.
    UIElement MiniThread(string transcriptPath, string workerName, string convUrl)
    {
        var turns = ReadLastTurns(transcriptPath, MINI_THREAD_ENTRIES);
        if (turns.Count == 0) return null;
        bool ja = _lang == 0;
        var panel = new StackPanel { Margin = new Thickness(0, 6, 0, 6) };

        int total = CountTurns(transcriptPath);
        // The listed entries are the LAST few, so the first of them sits at this offset in the
        // whole transcript. Clicking one has to name the entry the reader chose, not the n-th
        // of the three shown.
        int firstIndex = Math.Max(0, total - turns.Count);
        int shown = 0;
        foreach (var t in turns)
        {
            bool user = t.Item1 == "U";
            string body = (t.Item2 ?? "").Replace("\r", "");
            int lines = body.Length == 0 ? 0 : body.Split('\n').Length;
            string summary = FirstMeaningfulLine(body, MINI_THREAD_SUMMARY_CHARS);

            var row = new StackPanel { Margin = new Thickness(0, 0, 0, 8) };
            var head = new TextBlock
            {
                Text = user ? (ja ? "指示 / あなた" : "Instruction / You") : (ja ? "エージェント" : "Agent"),
                FontSize = 11,
                FontWeight = FontWeights.SemiBold,
                Foreground = user ? Accent : Muted,
                Margin = new Thickness(0, 0, 0, 2),
            };
            row.Children.Add(head);

            // ONE LINE, AND IT DOES NOT SCROLL. TextTrimming rather than a height cap: a clipped
            // line is honest about being clipped, a scrollbox invites reading and then refuses.
            var line = new TextBlock
            {
                Text = summary,
                FontSize = 12,
                Foreground = Fg,
                TextTrimming = TextTrimming.CharacterEllipsis,
                TextWrapping = TextWrapping.NoWrap,
            };
            row.Children.Add(line);

            // THE SIZE IS THE REASON TO OPEN IT, so it is stated rather than implied by a
            // scrollbar the reader has to discover.
            if (lines > 1)
            {
                var size = new TextBlock
                {
                    Text = ja ? ("本文 " + lines + " 行") : (lines + " lines"),
                    FontSize = 11,
                    Foreground = Muted,
                    Margin = new Thickness(0, 2, 0, 0),
                };
                row.Children.Add(size);
            }

            // THE ENTRY IS THE CHOOSER. Clicking it opens the conversation in the chat window
            // AT THIS ENTRY -- the card decides what to read, the chat window is where reading
            // happens. Opening at the end would hand the reader the job of finding again what
            // they had just picked.
            int idx = firstIndex + shown;
            string wn = workerName, cu = convUrl;
            var hit = new Border
            {
                Child = row,
                Background = Brushes.Transparent,
                Cursor = Cursors.Hand,
                Padding = new Thickness(0, 4, 0, 4),
            };
            hit.ToolTip = ja ? "この発言をメインチャットで開く" : "Open this entry in the chat window";
            hit.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e)
            {
                e.Handled = true;
                OpenWorker(wn, cu, idx);
            };
            panel.Children.Add(hit);
            shown++;
        }

        if (total > turns.Count)
        {
            var more = new TextBlock
            {
                Text = ja ? ("ほか " + (total - turns.Count) + " 件") : ((total - turns.Count) + " more"),
                FontSize = 11,
                Foreground = Muted,
                Margin = new Thickness(0, 0, 0, 0),
            };
            panel.Children.Add(more);
        }
        return panel;
    }

    // The first line worth showing: skips blanks and a lone markdown heading marker, and trims to
    // `max`. Not a summary -- a label. Producing a real summary is the agent's job, not the
    // card's, and a card that paraphrases would be inventing.
    static string FirstMeaningfulLine(string body, int max)
    {
        if (string.IsNullOrEmpty(body)) return "";
        foreach (string raw in body.Split('\n'))
        {
            string ln = raw.Trim();
            if (ln.Length == 0) continue;
            int h = 0; while (h < ln.Length && ln[h] == '#') h++;
            if (h > 0 && h < ln.Length && ln[h] == ' ') ln = ln.Substring(h + 1).Trim();
            ln = ln.Replace("**", "").Replace("`", "");
            if (ln.Length == 0) continue;
            return ln.Length > max ? ln.Substring(0, max) + "…" : ln;
        }
        return "";
    }

    // How many turns the transcript holds, so "ほか N 件" is a fact rather than a guess. Counts
    // the same lines ReadLastTurns keeps (meta and guid markers skipped); 0 on any failure,
    // which renders as no line at all rather than as a wrong number.
    int CountTurns(string transcriptPath)
    {
        try
        {
            if (string.IsNullOrEmpty(transcriptPath) || !File.Exists(transcriptPath)) return 0;
            int n = 0;
            foreach (string ln in File.ReadLines(transcriptPath))
            {
                if (ln.Length == 0) continue;
                if (ln.IndexOf("\"role\"", StringComparison.Ordinal) < 0) continue;
                n++;
            }
            return n;
        }
        catch (Exception) { return 0; }
    }

    // Last `n` (role, text) turns from a jsonl transcript (skips meta / guid marker lines).
    List<Tuple<string, string>> ReadLastTurns(string path, int n)
    {
        var all = new List<Tuple<string, string>>();
        try
        {
            if (string.IsNullOrEmpty(path) || !File.Exists(path)) return all;
            string[] lines;
            using (var fsr = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8))
                lines = sr.ReadToEnd().Replace("\r", "").Split('\n');
            foreach (var ln in lines)
            {
                if (string.IsNullOrEmpty(ln)) continue;
                Dictionary<string, object> o;
                try { o = _js.DeserializeObject(ln) as Dictionary<string, object>; } catch { continue; }
                if (o == null || !o.ContainsKey("role")) continue;
                string role = o["role"] != null ? o["role"].ToString() : "assistant";
                string text = (o.ContainsKey("text") && o["text"] != null) ? o["text"].ToString() : "";
                all.Add(Tuple.Create(role.StartsWith("user") ? "U" : "A", text));
            }
        }
        catch { }
        if (all.Count > n) all = all.GetRange(all.Count - n, n);
        return all;
    }

    // Feature 1: slim always-visible steer affordance on a COLLAPSED non-terminal card -- mid-run
    // steering used to require expanding the card first. One line: a borderless underline TextBox
    // + a small "↳" send glyph. Reuses TrySteerSend (see above) for the actual command write.
    //
    // Keystroke-safety (the reason this is more than a two-line TextBox): a LIVE worker's `last`/
    // `turn`/etc. stream every ~700ms sweep, which changes this card's RowSig and makes Card(w)
    // rebuild the WHOLE card -- including this box -- independent of whether the user is mid-type.
    // Three things make that survivable:
    //   1. Text is seeded from _steerDraft[name] on every build, and TextChanged writes back to
    //      it -- so a torn-down-and-rebuilt box never starts empty.
    //   2. _steerBoxRef[name] is updated to the newest TextBox instance on every build, and
    //      RenderCards' post-SetRows Dispatcher pass (Loaded priority, mirroring the existing
    //      scroll-restore pass right above it) re-Focus()es + re-seats the caret on whichever
    //      instance is current, so a rebuild mid-type does not drop focus either.
    //   3. GotFocus/LostFocus maintain _steerFocusWorker so that pass knows there IS a focus to
    //      restore, and to WHICH worker. LostFocus defers its "did focus really leave" check to
    //      ContextIdle -- strictly after the Loaded-priority restore -- so a teardown-triggered
    //      LostFocus on the OLD box (which fires before the new box is focused) can't race ahead
    //      and clear the tracked worker before the restore runs.
    UIElement CollapsedSteerRow(string name)
    {
        var outer = new DockPanel();
        outer.Margin = new Thickness(24, 5, 0, 0);
        // must not bubble to the card's whole-row open-conversation click handler
        outer.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; };

        var send = new Button();
        // Icon-only so the row stays one line. Was U+21B3, which is a typographic arrow and
        // not a control: nothing set its weight or its size but the fallback font.
        send.Content = MakeIcon("subdirectory_arrow_left", 14, Muted);
        send.ToolTip = _lang == 0 ? "送信" : "Send";
        send.Background = Brushes.Transparent; send.Foreground = Muted;
        send.BorderThickness = new Thickness(0); send.Cursor = Cursors.Hand; send.FontSize = 13;
        send.Padding = new Thickness(6, 0, 2, 0);
        DockPanel.SetDock(send, Dock.Right);
        outer.Children.Add(send);

        string nm = name;
        // input + placeholder watermark overlay (same idea as SteerRow's, compacted into one line)
        var inputGrid = new Grid();
        var tb = new TextBox();
        SwallowMouseUp(tb);   // selecting/typing must not trigger the card's open-conversation click
        tb.FontSize = 12; tb.Padding = new Thickness(4, 2, 4, 2);
        tb.BorderThickness = new Thickness(0, 0, 0, 1); tb.BorderBrush = Border;
        tb.Background = Brushes.Transparent; tb.Foreground = Fg; tb.CaretBrush = Fg;
        tb.ToolTip = _lang == 0 ? "このタスクへ追加指示（長時間タスクでは次の安全なターン境界で反映）"
                                : "Add an operator instruction (durable tasks apply it at the next safe turn boundary)";
        string draft;
        tb.Text = _steerDraft.TryGetValue(nm, out draft) ? draft : "";
        _steerBoxRef[nm] = tb;   // newest realized instance for this worker (used by the focus-restore pass)

        var placeholder = new TextBlock();
        placeholder.Text = T("steer_collapsed_placeholder");
        placeholder.Foreground = Muted; placeholder.FontSize = 12;
        placeholder.Padding = new Thickness(4, 2, 4, 2);
        placeholder.IsHitTestVisible = false;
        placeholder.Visibility = string.IsNullOrEmpty(tb.Text) ? Visibility.Visible : Visibility.Collapsed;
        inputGrid.Children.Add(placeholder);
        inputGrid.Children.Add(tb);

        Func<bool> trySend = delegate
        {
            string failReason;
            if (!TrySteerSend(nm, tb.Text, out failReason))
            {
                if (failReason != null) ShowScaleToast(failReason);
                return false;
            }
            tb.Text = "";
            _steerDraft.Remove(nm);
            ShowScaleToast(T("steer_collapsed_ack"));
            return true;
        };
        send.Click += delegate { trySend(); };
        // PreviewKeyDown (tunnel), NOT KeyDown (bubble) -- matches _goalInput's own Enter
        // handling (line ~4358). MEASURED BUG: the operator typed a steer into a running
        // job's box and pressing Enter minimised the whole cockpit window instead of (or as
        // well as) sending it. This box used a bubbling KeyDown, so this control's own
        // Handled=true only stops OTHER BUBBLE handlers -- it can never run early enough to
        // stop a tunnel-phase handler above it, and it cannot stop a class handler anywhere
        // in the ancestor chain that listens with handledEventsToo=true (WPF's IsDefault-button
        // mechanism is exactly that shape). Handling it in Preview, at the box itself, marks
        // Handled during the tunnel pass -- before the bubble pass (and anything hooked to it)
        // ever runs -- which is the earliest point this control can act and the same guarantee
        // the composer already relies on.
        tb.PreviewKeyDown += delegate (object s2, KeyEventArgs e2)
        {
            if (e2.Key == Key.Return) { trySend(); e2.Handled = true; }
        };
        tb.TextChanged += delegate
        {
            _steerDraft[nm] = tb.Text;
            placeholder.Visibility = string.IsNullOrEmpty(tb.Text) ? Visibility.Visible : Visibility.Collapsed;
        };
        tb.GotFocus += delegate { _steerFocusWorker = nm; };
        tb.LostFocus += delegate
        {
            // Defer the "did focus really move away" check to ContextIdle -- strictly lower
            // priority than the Loaded-priority restore pass in RenderCards, so if this LostFocus
            // fired only because the container was rebuilt out from under the user (not because
            // they clicked elsewhere), the restore pass has already re-focused the NEW box by the
            // time this runs, and stillOnSteer below comes back true (no-op).
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (_steerFocusWorker != nm) return;
                TextBox cur = Keyboard.FocusedElement as TextBox;
                TextBox tracked;
                bool stillOnSteer = cur != null && _steerBoxRef.TryGetValue(nm, out tracked) && cur == tracked;
                if (!stillOnSteer) _steerFocusWorker = null;
            }), System.Windows.Threading.DispatcherPriority.ContextIdle);
        };
        outer.Children.Add(inputGrid);   // last child + LastChildFill -> fills remaining width
        return outer;
    }

    // ② steering: inject a mid-task instruction into this worker's conversation.
    // Task 6: restyled as a small composer -- rounded surfaceSubtle border, placeholder watermark,
    // neutral send button, post-send wording updated.
    UIElement SteerRow(string name)
    {
        var outer = new StackPanel();
        outer.Margin = new Thickness(0, 12, 0, 0);
        // clicks inside this row must not bubble to the card's open-conversation handler
        outer.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; };

        // mini-composer wrapper border (matches bottom composer look, smaller)
        var composerBorder = new Border();
        composerBorder.CornerRadius = new CornerRadius(Theme.RadCard);
        composerBorder.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        composerBorder.BorderBrush = Border;
        composerBorder.BorderThickness = new Thickness(1);
        composerBorder.Padding = new Thickness(12, 8, 12, 8);

        var dp = new DockPanel();

        var send = new Button();
        send.Content = _lang == 0 ? "送信" : "Send";
        // Task 6: neutral by default (not accent)
        send.Background = Brushes.Transparent; send.Foreground = Fg;
        send.BorderThickness = new Thickness(1); send.BorderBrush = Border;
        send.Padding = new Thickness(12, 4, 12, 4); send.Cursor = Cursors.Hand; send.FontSize = 12;
        send.FontWeight = FontWeights.SemiBold;
        DockPanel.SetDock(send, Dock.Right);
        dp.Children.Add(send);

        // honest feedback when there is no live fleet to consume the steer
        var note = new TextBlock();
        note.FontSize = 11.5; note.Foreground = Muted; note.TextWrapping = TextWrapping.Wrap;
        note.VerticalAlignment = VerticalAlignment.Center; note.Margin = new Thickness(0, 0, 8, 0);
        DockPanel.SetDock(note, Dock.Right);
        dp.Children.Add(note);

        // input + placeholder watermark overlay
        var inputGrid = new Grid();
        var tb = new TextBox();
        tb.FontSize = 12.5; tb.Padding = new Thickness(4, 3, 4, 3);
        tb.BorderThickness = new Thickness(0); tb.Background = Brushes.Transparent; tb.Foreground = Fg;
        tb.CaretBrush = Fg;
        tb.ToolTip = _lang == 0 ? "このタスクへ追加指示（長時間タスクでは次の安全なターン境界で反映）"
                                : "Add an operator instruction (durable tasks apply it at the next safe turn boundary)";
        string savedDraft;
        tb.Text = _steerDraft.TryGetValue(name, out savedDraft) ? savedDraft : "";
        _steerBoxRef[name] = tb;
        // placeholder watermark text (hides when text is present)
        var placeholder = new TextBlock();
        placeholder.Text = _lang == 0 ? "このタスクに追加指示..." : "Add instruction to this task...";
        placeholder.Foreground = Muted; placeholder.FontSize = 12.5;
        placeholder.Padding = new Thickness(4, 3, 4, 3);
        placeholder.VerticalAlignment = VerticalAlignment.Center;
        placeholder.IsHitTestVisible = false;
        inputGrid.Children.Add(placeholder);
        inputGrid.Children.Add(tb);
        dp.Children.Add(inputGrid);

        composerBorder.Child = dp;
        outer.Children.Add(composerBorder);

        string nm = name;
        // returns true iff the steer was actually sent; keeps the text + shows a note otherwise.
        // Reuses TrySteerSend (the ONE steer-send code path, shared with CollapsedSteerRow).
        Func<bool> trySteer = delegate
        {
            string failReason;
            if (!TrySteerSend(nm, tb.Text, out failReason))
            {
                if (failReason != null) note.Text = failReason;
                return false;
            }
            tb.Text = "";
            _steerDraft.Remove(nm);
            // Task 6: updated post-send wording
            note.Text = _lang == 0 ? "次のターンに送信しました" : "Queued for the next turn";
            return true;
        };
        send.Click += delegate { trySteer(); };
        // PreviewKeyDown, not KeyDown -- see the matching note on CollapsedSteerRow's box
        // above (same control family, same fix, same reason).
        tb.PreviewKeyDown += delegate (object s, KeyEventArgs e)
        {
            if (e.Key == Key.Return) { trySteer(); e.Handled = true; }
        };
        tb.TextChanged += delegate
        {
            bool hasText = tb.Text != null && tb.Text.Length > 0;
            _steerDraft[nm] = tb.Text;
            placeholder.Visibility = hasText ? Visibility.Collapsed : Visibility.Visible;
            if (note.Text.Length > 0 && hasText) note.Text = "";
        };
        return outer;
    }

    // "続ける" (Continue) inline composer for a FINISHED (done) card -- the SAME mini-composer UI as
    // SteerRow (割り込み). A done worker is terminal and can't take a steer, so instead of injecting
    // into a live worker this launches a FRESH continuation: BuildContinueGoal prepends the prior
    // goal + "re-read your on-disk outputs" so the agent recovers context, then runs the follow-up.
    // Written as ONE JSONL line by SpawnFleet's GoalsToJsonl (the prefix is multi-line; a plain
    // multi-line string would otherwise be split one bogus goal per source line).
    // No RunIsLive gate -- a continuation starts its own run.
    UIElement ContinueRow(string name, string goal, string conv)
    {
        var outer = new StackPanel();
        outer.Margin = new Thickness(0, 12, 0, 0);
        outer.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; };

        var composerBorder = new Border();
        composerBorder.CornerRadius = new CornerRadius(Theme.RadCard);
        composerBorder.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        composerBorder.BorderBrush = Border;
        composerBorder.BorderThickness = new Thickness(1);
        composerBorder.Padding = new Thickness(12, 8, 12, 8);

        var dp = new DockPanel();

        var send = new Button();
        send.Content = _lang == 0 ? "続ける" : "Continue";
        send.Background = Brushes.Transparent; send.Foreground = Fg;
        send.BorderThickness = new Thickness(1); send.BorderBrush = Border;
        send.Padding = new Thickness(12, 4, 12, 4); send.Cursor = Cursors.Hand; send.FontSize = 12;
        send.FontWeight = FontWeights.SemiBold;
        DockPanel.SetDock(send, Dock.Right);
        dp.Children.Add(send);

        var note = new TextBlock();
        note.FontSize = 11.5; note.Foreground = Muted; note.TextWrapping = TextWrapping.Wrap;
        note.VerticalAlignment = VerticalAlignment.Center; note.Margin = new Thickness(0, 0, 8, 0);
        DockPanel.SetDock(note, Dock.Right);
        dp.Children.Add(note);

        var inputGrid = new Grid();
        var tb = new TextBox();
        tb.FontSize = 12.5; tb.Padding = new Thickness(4, 3, 4, 3);
        tb.BorderThickness = new Thickness(0); tb.Background = Brushes.Transparent; tb.Foreground = Fg;
        tb.CaretBrush = Fg;
        tb.ToolTip = _lang == 0 ? "この完了タスクに追加指示（前回の成果物を読み直してから実行）"
                                : "Follow-up to this finished task (re-reads its saved outputs first)";
        var placeholder = new TextBlock();
        placeholder.Text = _lang == 0 ? "完了タスクに続けて指示..." : "Continue this finished task...";
        placeholder.Foreground = Muted; placeholder.FontSize = 12.5;
        placeholder.Padding = new Thickness(4, 3, 4, 3);
        placeholder.VerticalAlignment = VerticalAlignment.Center;
        placeholder.IsHitTestVisible = false;
        inputGrid.Children.Add(placeholder);
        inputGrid.Children.Add(tb);
        dp.Children.Add(inputGrid);

        composerBorder.Child = dp;
        outer.Children.Add(composerBorder);

        string g = goal, c = conv;
        Func<bool> tryContinue = delegate
        {
            string t = (tb.Text ?? "").Trim();
            if (t.Length == 0) return false;
            string goalText = ContinueText(g, t, c);
            // SpawnFleet's GoalsToJsonl escapes a plain multi-line goal string safely on its
            // own; only pre-serialize here to carry the EXTRA resume_conv key (GoalsToJsonl
            // detects an already-JSON goal string and passes it through as-is).
            var glist = new List<string>();
            if (!string.IsNullOrEmpty(c))
            {
                var gd = new Dictionary<string, object>();
                gd["text"] = goalText;
                gd["resume_conv"] = c;
                glist.Add(_js.Serialize(gd));
            }
            else
            {
                glist.Add(goalText);
            }
            SpawnFleet(glist, "continue_input.txt");
            tb.Text = "";
            note.Text = _lang == 0 ? "続きを開始しました" : "Continuation started";
            return true;
        };
        send.Click += delegate { tryContinue(); };
        // PreviewKeyDown, not KeyDown -- see the matching note on CollapsedSteerRow's box.
        tb.PreviewKeyDown += delegate (object s, KeyEventArgs e)
        {
            if (e.Key == Key.Return) { tryContinue(); e.Handled = true; }
        };
        tb.TextChanged += delegate
        {
            bool hasText = tb.Text != null && tb.Text.Length > 0;
            placeholder.Visibility = hasText ? Visibility.Collapsed : Visibility.Visible;
            if (note.Text.Length > 0 && hasText) note.Text = "";
        };
        return outer;
    }

    // Feature A: a read-only TextBox inside a clickable card would still raise the card's
    // MouseLeftButtonUp -> open-conversation, so dragging to select text would yank you into
    // the chat. Swallow the mouse-up at the TextBox (handledEventsToo) so selection works and
    // the card click never fires.
    void SwallowMouseUp(TextBox tb)
    {
        tb.AddHandler(UIElement.MouseLeftButtonUpEvent,
            new MouseButtonEventHandler(delegate(object s, MouseButtonEventArgs e) { e.Handled = true; }),
            true);
    }

    // Feature C: per-card "Retry" on a terminal non-DONE worker. Re-queues this exact goal
    // (with its acceptance checks + cwd) at priority via add_goal. Clicks must not bubble to
    // the card's open-conversation handler.
    UIElement RetryRow(Dictionary<string, object> w)
    {
        var dp = new DockPanel();
        dp.Margin = new Thickness(0, 12, 0, 0);
        dp.MouseLeftButtonUp += delegate(object s, MouseButtonEventArgs e) { e.Handled = true; };
        var note = new TextBlock();
        note.FontSize = 11.5; note.Foreground = Muted;
        note.VerticalAlignment = VerticalAlignment.Center;
        note.TextWrapping = TextWrapping.Wrap;
        dp.Children.Add(note);
        var btn = new Button();
        btn.Content = T("retry");
        btn.Background = AccentFill; btn.Foreground = AccentFg; btn.BorderThickness = new Thickness(0);
        btn.Padding = new Thickness(12, 4, 12, 4); btn.Cursor = Cursors.Hand; btn.FontSize = 12;
        btn.FontWeight = FontWeights.SemiBold;
        DockPanel.SetDock(btn, Dock.Right);
        Dictionary<string, object> wkr = w;
        btn.Click += delegate
        {
            RetryGoal(wkr);
            note.Text = RunIsLive() ? "" : T("retry_note");
        };
        dp.Children.Add(btn);
        return dp;
    }

    // Build one add_goal entry { text, checks, cwd, priority } for a worker dict. checks is
    // passed through as-is from the worker (whatever the fleet stored -- a list).
    Dictionary<string, object> RetryEntry(Dictionary<string, object> w)
    {
        var item = new Dictionary<string, object>();
        item["text"] = S(w, "goal");
        object checks = null;
        if (w.ContainsKey("checks")) checks = w["checks"];
        item["checks"] = checks;
        item["cwd"] = S(w, "cwd");
        item["priority"] = true;
        item["retry"] = true;   // the runner refuses a retry whose goal is already live
        return item;
    }

    // Re-run the worker's goal. TWO honest paths, picked by whether a run is LIVE:
    //  * LIVE  -> send an add_goal command of its own (one file per command); the running fleet
    //            consumes it on its next sweep and re-runs it WITH its acceptance gate (checks+cwd).
    //  * FINISHED/stale -> nothing alive would ever drain add_goal, so instead SPAWN a fresh fleet
    //            for this goal text (SpawnFleet). The relaunched run picks it up and re-runs it.
    // The auto-retry scanner only calls this while live, so its add_goal path is unchanged.
    void RetryGoal(Dictionary<string, object> w)
    {
        string goal = S(w, "goal");
        if (string.IsNullOrEmpty(goal)) return;
        if (IsLocalLoopControlGoal(goal))
        {
            if (_startNote != null) _startNote.Text = _lang == 0
                ? "これはLOCAL_LOOPの内部制御記録です。通常Fleetとして再実行せず、耐久タスクのカードから再開してください。"
                : "This is a LOCAL_LOOP control record. Do not rerun it as Fleet work; resume the durable task card instead.";
            return;
        }
        if (RunIsLive())
        {
            SubmissionBaseline submitBaseline = CaptureSubmissionBaseline();
            var adds = new List<object>();
            adds.Add(RetryEntry(w));
            SendCommand(Cmd1("add_goal", adds));
            NoteSubmitted(new List<string> { S(w, "goal") }, submitBaseline);
            return;
        }
        try { SpawnFleet(new List<string> { goal }, "retry_input.txt"); _lastSig = ""; } catch (Exception) { }
    }

    // Feature C bulk: re-run EVERY currently-shown terminal non-DONE worker (respecting the active
    // filter). LIVE -> ONE add_goal command carrying every retried entry; FINISHED/stale -> ONE
    // relaunched fleet carrying
    // all the retried goal texts (mirrors RetryGoal's live/finished split).
    // THE RETRY BUDGET IS SHARED WITH AutoRetryScan, DELIBERATELY. This button had no cap of
    // any kind, so _autoRetryMax bounded only the automatic path. One submitted goal reached
    // 891 workers, 575 of them stuck: each press re-queued every still-stuck goal again, and
    // nothing on screen said the press had done anything -- so it was pressed again.
    //
    // Counting against the same per-goal counter stops the amplification. RETURNING the
    // skipped count is what stops the re-pressing, and matters just as much: a skip nobody
    // can see is indistinguishable from a button that did not work, which is the behaviour
    // that produced the pile in the first place.
    int RetryAllShown(List<Dictionary<string, object>> shown, out int skippedAtCap)
    {
        skippedAtCap = 0;
        bool live = RunIsLive();
        var adds = new List<object>();
        var goalTexts = new List<string>();
        int n = 0;
        foreach (Dictionary<string, object> w in shown)
        {
            if (!IsTerminalWorker(w)) continue;
            // Same closed set as AutoRetryScan and as relay/outcomes.py. "Retry all" used to
            // mean "everything that is not DONE", which swept up fan-out parents and threw
            // away the merged answers they carried.
            if (!IsRetryableWorker(w)) continue;
            // Already re-queued once (by us or by the runner): never a second copy of this worker.
            if (RetryAlreadyCovered(null, w)) continue;
            string g = S(w, "goal");
            // Same counter, same key (goal text), same ceiling as AutoRetryScan, so the auto
            // and manual paths cannot each spend a full allowance on the same goal.
            if (!string.IsNullOrEmpty(g))
            {
                int used = 0;
                if (_autoRetryCount.ContainsKey(g)) used = _autoRetryCount[g];
                if (used >= _autoRetryMax) { skippedAtCap++; continue; }
                _autoRetryCount[g] = used + 1;   // count BEFORE queueing, as AutoRetryScan does
            }
            _autoRetriedWorkers.Add(RetryWorkerKey(w));
            adds.Add(RetryEntry(w));
            if (!string.IsNullOrEmpty(g)) goalTexts.Add(g);
            n++;
        }
        if (n == 0) return 0;
        if (live)
        {
            SubmissionBaseline submitBaseline = CaptureSubmissionBaseline();
            SendCommand(Cmd1("add_goal", adds));
            NoteSubmitted(goalTexts, submitBaseline);
        }
        else if (goalTexts.Count > 0)
        {
            try { SpawnFleet(goalTexts, "retry_input.txt"); _lastSig = ""; } catch (Exception) { }
        }
        return n;
    }

    // MIRRORS relay/outcomes.py RETRYABLE. A copy, and copies drift -- so a test fails if
    // this stops matching the Python set. An outcome missing from here is not retried, which
    // is the safe direction; an outcome wrongly added here re-runs finished work.
    static readonly string[] _retryableOutcomes = { "STUCK", "INFRA_STUCK", "REFUSED" };

    static bool IsRetryableOutcome(string outcome)
    {
        if (string.IsNullOrEmpty(outcome)) return false;
        foreach (string r in _retryableOutcomes)
            if (outcome == r) return true;
        return false;
    }

    static bool IsTerminalWorker(Dictionary<string, object> w)
    {
        // MUST MATCH relay/relay_fleet.py TERMINAL. A status missing here does more than skip
        // one card: MaybeAutoArchive returns early the moment any worker looks unfinished, so
        // one unrecognised status costs the whole run its auto-archive.
        string status = S(w, "status");
        return status == "done" || status == "stuck" || status == "maxturns"
               || status == "error" || status == "cancelled"
               || status == "content_refused";
    }

    // relay/fleet_reaper.py writes status "interrupted" into the sidecars of a coordinator that
    // DIED (crash, kill, disk full). It is NOT terminal (never add it to IsTerminalWorker: the
    // work is resumable and must not be archived or counted as finished) and it is NOT running
    // and NOT a failure of the goal. Mirrors relay/outcomes.py STATUS_OF["INTERRUPTED"].
    static bool IsInterruptedWorker(Dictionary<string, object> w)
    {
        return w != null && S(w, "status") == "interrupted";
    }

    // Severity rank for the "unfinished only" sort: failures first, then max-turns, then
    // cancelled, then still-running/other (stable within a rank).
    static int SeverityRank(Dictionary<string, object> w)
    {
        string oc = S(w, "outcome");
        if (oc == "STUCK" || oc == "ERROR") return 0;
        if (oc == "MAXTURNS") return 1;
        if (oc == "CANCELLED") return 2;
        if (oc == "INTERRUPTED") return 2;
        return 3;   // still-running / other
    }

    UIElement MakeReleaseContent()
    {
        var sp = new StackPanel(); sp.Orientation = Orientation.Horizontal;
        sp.Children.Add(MakeIcon("close", 13, Fg));
        var t = new TextBlock(); t.Text = "  " + T("release");
        t.FontSize = 12; t.Foreground = Fg; t.VerticalAlignment = VerticalAlignment.Center;
        sp.Children.Add(t);
        return sp;
    }

    // Status chip (spec): a small OUTLINED pill -- colored border + colored text on a transparent
    // fill, never a saturated block. The kind is one of neutral/info/success/warning/danger.
    // P0 AGENT BADGE (feature 4): show which agent a worker's conversation is bound to, read
    // from its conv_url in status.json.
    //   • conv_url carries "/agent/<id>" AND <id> matches the configured agent -> subtle Muted
    //     badge with the agent's short name (12-char title, else "agent").
    //   • conv_url carries "/agent/<otherid>" -> subtle badge (still an agent, just not ours).
    //   • conv_url is a plain "/chat/" url with NO "/agent/" segment -> WARNING 既定Copilot badge
    //     (a default-Copilot conversation has no MCP connector — the incident case).
    //   • no conv_url yet (worker just started) -> null (no badge; don't accuse prematurely).
    Border BuildAgentBadge(string convUrl, string convTitle)
    {
        if (string.IsNullOrEmpty(convUrl)) return null;
        string lo = convUrl.ToLowerInvariant();
        bool hasAgentSeg = lo.Contains("/agent/");
        if (hasAgentSeg)
        {
            // Agent-bound conversation. Prefer a short name from the title; fall back to "agent".
            string shortName = "agent";
            if (!string.IsNullOrEmpty(convTitle))
            {
                string tt = OneLine(convTitle);
                shortName = tt.Length > 12 ? tt.Substring(0, 12) : tt;
            }
            bool mine = !string.IsNullOrEmpty(_agentMarkerId)
                        && lo.Contains(_agentMarkerId.ToLowerInvariant());
            // Subtle (Muted) badge either way; the configured-agent case is the expected/quiet state.
            return AgentBadge(shortName, mine ? "neutral" : "neutral");
        }
        // A conversation URL that is a plain /chat/ (no /agent/ segment) — default Copilot.
        if (lo.Contains("/chat/") || lo.Contains("/conversation/"))
            return AgentBadge(T("badge_default_copilot"), "warning");
        return null;
    }

    // Small outlined badge. kind: "neutral" (subtle) | "warning" (default-Copilot alert).
    Border AgentBadge(string text, string kind)
    {
        Brush color = kind == "warning" ? Theme.Br(Theme.Warning(_dark)) : Theme.Br(Theme.Muted(_dark));
        var b = new Border();
        b.Background = Brushes.Transparent;
        b.BorderBrush = color; b.BorderThickness = new Thickness(1);
        b.CornerRadius = new CornerRadius(Theme.RadChip);
        b.Padding = new Thickness(5, 0, 5, 0);
        b.Margin = new Thickness(0, 0, 5, 0);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = text; t.Foreground = color;
        t.FontSize = 10.5; t.FontWeight = FontWeights.SemiBold;
        t.VerticalAlignment = VerticalAlignment.Center;
        b.Child = t;
        b.ToolTip = kind == "warning"
            ? (_lang == 0 ? "既定Copilotの会話（MCPコネクタ無し）。エージェントに接続し直してください。"
                          : "Default-Copilot conversation (no MCP connector). Reconnect to the agent.")
            : (_lang == 0 ? "この会話はエージェントに接続されています" : "This conversation is bound to the agent");
        return b;
    }

    // Per-worker effort pill from the additive status.json fields (EffortPolicyView words it).
    Border BuildEffortBadge(Dictionary<string, object> w)
    {
        bool ja = _lang == 0;
        Dictionary<string, object> last = Obj(w, "effort_last_switch");
        string level = S(w, "effort_level"), src = S(w, "effort_source");
        string text = EffortPolicyView.BadgeText(level, src, last, ja);
        if (text == null) return null;
        var b = Pill(text, "neutral");
        b.Margin = new Thickness(0, 0, 5, 0);
        b.ToolTip = EffortPolicyView.BadgeTip(level, src, last, ja);
        return b;
    }

    Border Pill(string text, string kind)
    {
        var color = Theme.Br(Theme.KindColor(kind, _dark));
        var b = new Border();
        b.Background = Brushes.Transparent;
        b.BorderBrush = color; b.BorderThickness = new Thickness(1);
        // Tight rounded-rect tag (not a CornerRadius=999 stadium oval). The oval shape left
        // empty space inside the frame on each side of a short label like "完了", which read as a
        // floating "枠"/gap between the chip and the goal text. A small radius hugs the label.
        b.CornerRadius = new CornerRadius(Theme.RadChip);
        b.Padding = new Thickness(6, 1, 6, 1);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = text; t.Foreground = color;
        t.FontSize = 11.5; t.FontWeight = FontWeights.SemiBold;
        b.Child = t;
        return b;
    }

    // Feature 2: Pill() itself carries no external spacing (callers position it inside a DockPanel/
    // StackPanel that already spaces siblings). A WrapPanel does not, so give each header chip a
    // small right/bottom margin -- small helper instead of repeating the Thickness at every call site.
    Border ChipMargin(Border pill)
    {
        pill.Margin = new Thickness(0, 0, 6, 4);
        return pill;
    }

    // Collapse newlines/tabs/runs of whitespace into single spaces -- for the one-line card result.
    static string OneLine(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder(s.Length);
        bool lastSpace = false;
        foreach (char c in s)
        {
            char ch = (c == '\n' || c == '\r' || c == '\t') ? ' ' : c;
            if (ch == ' ') { if (lastSpace) continue; lastSpace = true; }
            else lastSpace = false;
            sb.Append(ch);
        }
        return sb.ToString().Trim();
    }

    // Task 3: clean agent result text for display only (never touch logs/transcript).
    // Normalizes line endings, trims lines, drops empty and preamble-only lines,
    // removes adjacent duplicate lines, and trims a trailing lone "DONE" if redundant.
    static readonly string[] _resultPreambleTokens = {
        "desktopfile操作", "browser操作", "computeruse", "Copilot", "エージェント"
    };
    static string CleanAgentResultForUi(string raw)
    {
        if (string.IsNullOrEmpty(raw)) return "";
        // Normalize CRLF/CR to LF, split, trim each line, drop empty.
        string normalized = raw.Replace("\r\n", "\n").Replace("\r", "\n");
        string[] parts = normalized.Split('\n');
        var lines = new List<string>();
        foreach (string p in parts)
        {
            string t = p.Trim();
            if (t.Length == 0) continue;
            // drop lines that are only a known preamble token (case-insensitive exact match)
            bool isPreamble = false;
            foreach (string tok in _resultPreambleTokens)
            {
                if (string.Equals(t, tok, StringComparison.OrdinalIgnoreCase))
                { isPreamble = true; break; }
            }
            if (isPreamble) continue;
            lines.Add(t);
        }
        // Remove repeated identical adjacent lines.
        var deduped = new List<string>();
        for (int i = 0; i < lines.Count; i++)
        {
            if (deduped.Count == 0 || lines[i] != deduped[deduped.Count - 1])
                deduped.Add(lines[i]);
        }
        // If the final line is exactly "DONE" and previous line already ends with "DONE", drop it.
        if (deduped.Count >= 2
            && deduped[deduped.Count - 1] == "DONE"
            && deduped[deduped.Count - 2].EndsWith("DONE"))
        {
            deduped.RemoveAt(deduped.Count - 1);
        }
        if (deduped.Count == 0) return "";
        var sb2 = new StringBuilder();
        for (int i = 0; i < deduped.Count; i++)
        {
            if (i > 0) sb2.Append('\n');
            sb2.Append(deduped[i]);
        }
        return sb2.ToString();
    }

    UIElement Dots()
    {
        var sp = new StackPanel(); sp.Orientation = Orientation.Horizontal;
        sp.VerticalAlignment = VerticalAlignment.Center; sp.Margin = new Thickness(8, 0, 0, 0);
        for (int i = 0; i < 3; i++)
        {
            var dot = new System.Windows.Shapes.Ellipse();
            dot.Width = 6; dot.Height = 6; dot.Fill = Muted; dot.Margin = new Thickness(2, 0, 0, 0);
            var anim = new DoubleAnimation(0.25, 1.0, new Duration(TimeSpan.FromMilliseconds(600)));
            anim.AutoReverse = true; anim.RepeatBehavior = RepeatBehavior.Forever;
            anim.BeginTime = TimeSpan.FromMilliseconds(i * 150);
            dot.BeginAnimation(UIElement.OpacityProperty, anim);
            sp.Children.Add(dot);
        }
        return sp;
    }

    // Disclosure chevron: '>' collapsed, 'v' expanded -- drawn as vector geometry to match the
    // Material-Symbols-as-paths aesthetic (the glyph subset has no chevron). Clicking toggles
    // this worker's detail and re-renders immediately. e.Handled stops the click from bubbling
    // to the card's open-conversation handler.
    UIElement ChevronToggle(string name, bool expanded)
    {
        var hit = new Border();
        hit.Background = Brushes.Transparent;            // whole padded area is the hit target
        hit.Padding = new Thickness(8, 8, 6, 8);
        hit.MinWidth = 24; hit.MinHeight = 28;           // ~24x28 px target -- tighter caret->chip, still easy to click
        hit.Cursor = Cursors.Hand;
        hit.VerticalAlignment = VerticalAlignment.Center;
        hit.HorizontalAlignment = HorizontalAlignment.Center;
        hit.ToolTip = _lang == 0
            ? (expanded ? "詳細を閉じる" : "詳細を開く（最新の進捗・割り込み）")
            : (expanded ? "Collapse details" : "Expand (latest progress + steer)");

        var path = new System.Windows.Shapes.Path();
        path.Data = Geometry.Parse("M 0,0 L 4,4 L 0,8");   // a '>' caret
        path.Stroke = Muted; path.StrokeThickness = 1.6;
        path.StrokeStartLineCap = PenLineCap.Round;
        path.StrokeEndLineCap = PenLineCap.Round;
        path.StrokeLineJoin = PenLineJoin.Round;
        path.Width = 7; path.Height = 11; path.Stretch = Stretch.None;
        path.VerticalAlignment = VerticalAlignment.Center;
        path.HorizontalAlignment = HorizontalAlignment.Center;
        if (expanded)
        {
            path.RenderTransformOrigin = new Point(0.5, 0.5);
            path.RenderTransform = new RotateTransform(90);   // '>' -> 'v'
        }
        hit.Child = path;

        string nm = name;
        // Fire on button-DOWN so the chevron beats the ListBoxItem's selection handling, which
        // captures the mouse on DOWN and would otherwise swallow our UP. PreviewMouseLeftButtonDown
        // runs in the tunnel before the item sees it; e.Handled stops the open-conversation handler.
        hit.PreviewMouseLeftButtonDown += delegate (object s, MouseButtonEventArgs e)
        {
            e.Handled = true;
            ToggleExpand(nm);
        };
        // Also swallow the button-UP: the toggle drives on DOWN, but the card opens the conversation
        // on MouseLeftButtonUp -- a DOWN-only Handled does NOT stop the separate UP event, so the UP
        // would bubble to card.MouseLeftButtonUp -> OpenWorker and wrongly open main. Consume it here
        // (same pattern as openLink / SwallowMouseUp elsewhere in this file).
        hit.PreviewMouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e)
        {
            e.Handled = true;
        };
        return hit;
    }

    // Flip one worker's detail and refresh the virtualizing list. With virtualization the old
    // "find the card in _cards.Children" trick no longer applies (only ~10-20 cards are realized),
    // so we rebuild the lightweight row models and reassign -- cheap, because only visible rows
    // realize. Scroll offset is preserved via ListScroller(), and _lastSig is re-synced to the
    // new expanded state so the next 700ms tick doesn't immediately trigger a redundant rebuild.
    void ToggleExpand(string name)
    {
        if (_expanded.Contains(name)) _expanded.Remove(name);
        else _expanded.Add(name);
        if (_lastRoot == null) { _lastSig = ""; OnTick(null, null); return; }

        ScrollViewer sc = ListScroller();
        double off = (sc != null) ? sc.VerticalOffset : 0.0;
        SetRows(BuildRows(_lastRoot));
        if (off > 0.0)
        {
            double target = off;
            // Restore the (pixel-based) offset AFTER the VirtualizingStackPanel has re-measured the
            // full scroll extent. At Loaded priority the extent isn't final yet, so ScrollTo clamps
            // to ~0 -> the big upward jump when expanding a low card. Run at Background (after
            // layout), force a layout pass, then re-apply clamped to the scrollable range so the
            // toggled card stays put and its detail appears in place.
            Dispatcher.BeginInvoke(new Action(delegate
            {
                ScrollViewer s2 = ListScroller();
                if (s2 == null) return;
                if (_list != null) _list.UpdateLayout();
                double max = s2.ScrollableHeight;
                double t = (target < 0.0) ? 0.0 : (target > max ? max : target);
                s2.ScrollToVerticalOffset(t);
            }), System.Windows.Threading.DispatcherPriority.Background);
        }
        _lastSig = Sig(_lastRoot) + "|s" + _submittedSig;   // the same shape OnTick compares
    }

    // ── cockpit -> fleet control channel ─────────────────────────────────────────
    // ONE COMMAND PER FILE, written through the shared FleetCommands.Write (ui/FleetCommands.cs).
    // The old ReadCommands/WriteCommands pair read all of commands.json, added this caller's key
    // and wrote the whole file back; CopilotChat.exe -- a SEPARATELY BUILT PROCESS -- did the same
    // thing to the same file, so whichever wrote second deleted the other's queued command. See
    // FleetCommands.cs for the full account. Each method below now sends ONLY its own key.
    Dictionary<string, object> Cmd1(string key, object val)
    {
        var d = new Dictionary<string, object>();
        d[key] = val;
        return d;
    }
    bool SendCommand(Dictionary<string, object> patch)
    {
        return FleetCommands.Write(_fleetDir, patch);
    }
    bool SendTrackedCommand(Dictionary<string, object> patch, out string path)
    {
        return FleetCommands.WriteTracked(_fleetDir, patch, out path);
    }

    // THE DEDUPE WENT WITH THE MERGE, DELIBERATELY. This used to read the pending `close` list
    // and skip a name already in it. With one command per file there is no pending list to
    // consult -- and reintroducing one would reintroduce the read-modify-write race that cost a
    // goal. It is acceptable here because the fleet applies closes idempotently: closing a
    // worker that is already closed is a no-op, so a duplicate close changes nothing.
    void RequestClose(string name)
    {
        var closes = new List<object>();
        closes.Add(name);
        SendCommand(Cmd1("close", closes));
    }

    void RequestSetMaxtabs(int n)
    {
        SendCommand(Cmd1("set_maxtabs", n));
    }

    // Live autoscale control: {"set_autoscale":{"on":0|1,"default":N,"max":M}}, sent as its own
    // command file through the SAME writer RequestSetMaxtabs uses, so it cannot clobber a
    // concurrent close/steer/add_goal from this window or from CopilotChat.exe.
    void RequestSetAutoscale(bool on, int def, int max)
    {
        var sa = new Dictionary<string, object>();
        sa["on"] = on ? 1 : 0;
        sa["default"] = def;
        sa["max"] = max;
        SendCommand(Cmd1("set_autoscale", sa));
    }

    Dictionary<string, object> WorkerByName(string name)
    {
        if (string.IsNullOrEmpty(name)) return null;
        try
        {
            foreach (Dictionary<string, object> w in WorkersOf(_lastRoot))
                if (string.Equals(S(w, "name"), name, StringComparison.Ordinal)) return w;
            Dictionary<string, object> root = ReadStatus();
            foreach (Dictionary<string, object> w in WorkersOf(root))
                if (string.Equals(S(w, "name"), name, StringComparison.Ordinal)) return w;
        }
        catch (Exception) { }
        return null;
    }

    static bool IsLocalLoopWorker(Dictionary<string, object> w)
    {
        return w != null && string.Equals(
            S(w, "execution_profile"), "LOCAL_LOOP", StringComparison.OrdinalIgnoreCase);
    }

    bool QueueLocalLoopSteer(Dictionary<string, object> w, string text, out string failReason)
    {
        failReason = null;
        string steerFile = null;
        System.Diagnostics.Process proc = null;
        try
        {
            if (w == null) { failReason = "LOCAL_LOOP worker metadata is unavailable."; return false; }
            if (!DurableRuntimeEnabled())
            {
                failReason = _lang == 0 ? "長時間実行が無効です。" : "Durable runtime is disabled.";
                return false;
            }
            string jobId = S(w, "name").Replace("\"", "");
            string db = S(w, "local_job_db").Replace("\"", "");
            if (string.IsNullOrWhiteSpace(jobId) || string.IsNullOrWhiteSpace(db))
            {
                failReason = _lang == 0 ? "長時間タスクの識別情報が不足しています。"
                                        : "Durable task identity is incomplete.";
                return false;
            }
            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            string stateDir = Path.GetDirectoryName(_statusPath);
            string token = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString() + "_"
                         + Guid.NewGuid().ToString("N").Substring(0, 8);
            steerFile = Path.Combine(stateDir, "durable_steer_" + token + ".txt");
            File.WriteAllText(steerFile, text ?? "", new UTF8Encoding(false));

            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m relay.local_loop_controller --operator-steer-job-id \"" + jobId
                          + "\" --operator-steer-file \"" + steerFile
                          + "\" --state-dir \"" + stateDir + "\" --db \"" + db + "\"";
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            proc = System.Diagnostics.Process.Start(psi);
            if (proc == null) throw new InvalidOperationException("LOCAL_LOOP steer process did not start");
            WatchLocalLoopSteer(proc, jobId, steerFile, text ?? "");
            return true;
        }
        catch (Exception ex)
        {
            try { if (!string.IsNullOrEmpty(steerFile) && File.Exists(steerFile)) File.Delete(steerFile); }
            catch (Exception) { }
            try { if (proc != null) proc.Dispose(); } catch (Exception) { }
            failReason = (_lang == 0 ? "追加指示を保存できませんでした: " : "Could not save operator update: ") + ex.Message;
            return false;
        }
    }

    void WatchLocalLoopSteer(System.Diagnostics.Process proc, string jobId,
                             string steerFile, string text)
    {
        var timer = new System.Windows.Threading.DispatcherTimer();
        timer.Interval = TimeSpan.FromMilliseconds(200);
        timer.Tick += delegate
        {
            try
            {
                proc.Refresh();
                if (!proc.HasExited) return;
                timer.Stop();
                int code = -1;
                try { code = proc.ExitCode; } catch (Exception) { }
                if (code == 0)
                {
                    _steerDraft.Remove(jobId);
                    try { if (!string.IsNullOrEmpty(steerFile) && File.Exists(steerFile)) File.Delete(steerFile); }
                    catch (Exception) { }
                    ShowScaleToast(_lang == 0 ? "追加指示を長時間タスクへ保存しました"
                                               : "Operator update saved to the durable task");
                    _lastSig = "";
                }
                else
                {
                    _steerDraft[jobId] = text;
                    TextBox box;
                    if (_steerBoxRef.TryGetValue(jobId, out box) && box != null)
                    {
                        try { if (string.IsNullOrEmpty(box.Text)) box.Text = text; } catch (Exception) { }
                    }
                    try { if (!string.IsNullOrEmpty(steerFile) && File.Exists(steerFile)) File.Delete(steerFile); }
                    catch (Exception) { }
                    ShowScaleToast((_lang == 0 ? "追加指示を保存できませんでした。入力を復元しました。終了コード "
                                                   : "Operator update failed; input restored. Exit code ") + code);
                    _lastSig = "";
                }
                try { proc.Dispose(); } catch (Exception) { }
            }
            catch (Exception ex)
            {
                timer.Stop();
                _steerDraft[jobId] = text;
                TextBox box;
                if (_steerBoxRef.TryGetValue(jobId, out box) && box != null)
                {
                    try { if (string.IsNullOrEmpty(box.Text)) box.Text = text; } catch (Exception) { }
                }
                ShowScaleToast((_lang == 0 ? "追加指示の確認に失敗しました。入力を復元しました。 "
                                               : "Could not confirm operator update; input restored. ") + ex.Message);
                try { proc.Dispose(); } catch (Exception) { }
                _lastSig = "";
            }
        };
        timer.Start();
    }

    void RequestSteer(string name, string text)
    {
        if (string.IsNullOrEmpty(text)) return;
        var item = new Dictionary<string, object>();
        item["worker"] = name; item["text"] = text;
        var steers = new List<object>();
        steers.Add(item);
        SendCommand(Cmd1("steer", steers));
    }

    // Feature 1: the ONE steer-send code path, shared by SteerRow (expanded drawer) and
    // CollapsedSteerRow (always-visible mini box on a collapsed non-terminal card). Neither
    // caller re-implements the RequestSteer/commands.json write -- both funnel through here.
    // Returns true iff the steer was actually queued; otherwise failReason carries the reason
    // text (steer_dead) so the caller can surface it however fits its layout (inline note vs toast).
    bool TrySteerSend(string name, string text, out string failReason)
    {
        failReason = null;
        string t = (text ?? "").Trim();
        if (t.Length == 0) return false;
        Dictionary<string, object> worker = WorkerByName(name);
        if (IsLocalLoopWorker(worker))
            return QueueLocalLoopSteer(worker, t, out failReason);
        if (!RunIsLive()) { failReason = T("steer_dead"); return false; }
        RequestSteer(name, t);
        return true;
    }

    // FIX A: immediate LOCAL feedback on an open-conversation click. The main chat only notices
    // open.json on its own ~800ms poll, so without this the click reads as dead for up to 0.8s.
    // Briefly flips the clicked card's border/background to the accent colour and back; a
    // DispatcherTimer one-shot reverts it (mirrors the PaintPause optimistic-feedback pattern, and
    // the ShowScaleToast fade-timer style already used elsewhere in this file). Does NOT touch the
    // .fleet/open.json file-signal protocol -- purely a local visual cue.
    // Safe if `card` is later disposed/rebuilt mid-flash: it's just a property set on a UIElement
    // that may no longer be in the visual tree, which WPF tolerates, and the null check covers the
    // case where no card reference exists at all (e.g. a future caller passes null).
    void FlashOpen(Border card)
    {
        if (card == null) return;
        try
        {
            Brush origBorder = card.BorderBrush;
            Brush origBg = card.Background;
            card.BorderBrush = Theme.Br(Theme.Accent(_dark));
            card.Background = Theme.Br(Theme.AccentSoft(_dark));
            var t = new DispatcherTimer();
            t.Interval = TimeSpan.FromMilliseconds(350);
            t.Tick += delegate
            {
                t.Stop();
                if (card == null) return;
                card.BorderBrush = origBorder;
                card.Background = origBg;
            };
            t.Start();
        }
        catch (Exception) { }
    }

    // ① groundwork: ask the chat to open this conversation (it polls open.json).
    void OpenConv(string url)
    {
        if (string.IsNullOrEmpty(url)) return;
        try
        {
            _openSeq++;
            var o = new Dictionary<string, object>();
            o["url"] = url; o["ts"] = _openSeq;
            File.WriteAllText(_openPath, _js.Serialize(o), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    // Open a worker in the main chat by NAME (robust click target). The chat polls open.json;
    // it resolves the worker by name in status.json and shows its live snapshot, and uses url
    // for /history when a real Copilot conv_url was captured.
    void OpenWorker(string name, string url) { OpenWorker(name, url, -1); }

    // `turnIndex` is which entry the reader clicked, 0-based over the transcript's turns, or -1
    // for "just open it".
    //
    // WHY THE INDEX TRAVELS. The card lists what happened and holds no body; the full text opens
    // in the chat window. Opening it at the END would make the reader hunt for the entry they
    // had just chosen -- the review asked about this said so plainly: 「単に会話の末尾を開く
    // 設計では、対象を探し直す負担が残る」. Carrying the position is what makes the card a
    // chooser rather than a teaser.
    void OpenWorker(string name, string url, int turnIndex)
    {
        try
        {
            _openSeq++;
            var o = new Dictionary<string, object>();
            o["worker"] = name ?? ""; o["url"] = url ?? ""; o["ts"] = _openSeq;
            // Written only when there is one, so a reader of open.json can tell "no position
            // was asked for" from "position 0" -- which are different requests.
            if (turnIndex >= 0) o["turn_index"] = turnIndex;
            File.WriteAllText(_openPath, _js.Serialize(o), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    // Open a HISTORY (archived) conversation. Carries the exact disk transcript path so the chat
    // shows the full body straight from the .jsonl even when conv_url is empty -- plus the worker
    // name as a fallback (the chat resolves the newest .jsonl by name). Fixes the completed-
    // conversation "本文はまだ取得できません" dead-end.
    void OpenHistory(string name, string url, string transcript)
    {
        try
        {
            _openSeq++;
            var o = new Dictionary<string, object>();
            o["worker"] = name ?? ""; o["url"] = url ?? "";
            o["transcript"] = transcript ?? ""; o["ts"] = _openSeq;
            File.WriteAllText(_openPath, _js.Serialize(o), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    // Markers that open scaffolding WE compose and append to an operator's own instruction --
    // fanout range headers, fan-in aggregation notes, subtask separators, continuation notes.
    // Mirrors tools/skill_lessons.py's _COMPOSED_FROM exactly (same list, same "cut at the
    // earliest one" rule) so a card headline and a generated Skill proposal agree on where an
    // operator's own words end. MEASURED without this: 69 stored goals render, verbatim, as one
    // of these markers and nothing else -- e.g. a card headlined only "【前回タスクの続き】" or
    // "【この会話が担当する範囲 — 全体の 1/9】". Both are literally true and completely useless:
    // every unrelated fanout run's first card says the same thing.
    static readonly string[] _ComposedFrom = new string[] {
        "【この会話が担当する範囲", "【分割実行の結果をまとめてください】",
        "--- サブタスク ", "【前回タスクの続き】"
    };

    // The fixed per-turn protocol preamble (relay/copilot_autopilot_relay.py's PROTOCOL) that is
    // prepended ahead of the operator's own goal for every turn sent to a worker. Anchored on its
    // literal opening and literal closing sentence -- both constants -- rather than the whole
    // block, because the text between them (OUTPUT_DISCIPLINE) is loaded from a file and can
    // change length without notice. A goal that doesn't start with this preamble is untouched.
    const string _PreambleStart = "【最重要】使えるツールは";
    const string _PreambleEndMark = "まず最初のステップを実行。";

    // The part of `goal` a person actually typed: composed scaffolding, then the fixed protocol
    // preamble, stripped. Mirrors tools/skill_lessons.py's operator_instruction() -- conservative
    // in the same direction: a goal carrying neither is returned unchanged, never emptied.
    string OperatorInstruction(string goal)
    {
        string text = goal ?? "";
        int cut = text.Length;
        foreach (string mark in _ComposedFrom)
        {
            int i = text.IndexOf(mark, StringComparison.Ordinal);
            if (i >= 0 && i < cut) cut = i;
        }
        string outp = text.Substring(0, cut).Trim();
        if (outp.Length == 0) outp = text.Trim();
        if (outp.StartsWith(_PreambleStart, StringComparison.Ordinal))
        {
            int endIdx = outp.IndexOf(_PreambleEndMark, StringComparison.Ordinal);
            if (endIdx >= 0)
            {
                int rest = endIdx + _PreambleEndMark.Length;
                if (rest < outp.Length && outp[rest] == '\n') rest++;
                if (rest + 5 <= outp.Length && outp.Substring(rest, 5) == "Goal:") rest += 5;
                while (rest < outp.Length && (outp[rest] == ' ' || outp[rest] == '\n')) rest++;
                if (rest < outp.Length) outp = outp.Substring(rest).Trim();
            }
        }
        return outp;
    }

    // A concise card/conversation title, in order of preference: the Copilot-generated conv_title
    // when present and not generic; else the issue heading derived from the goal (the first real
    // line after a "== ... issue ==" marker -- SWE-bench-shaped goals only); else the first
    // non-boilerplate line of the OPERATOR'S OWN instruction (composed scaffolding + the fixed
    // protocol preamble stripped via OperatorInstruction, above); else the raw goal. Trimmed so
    // long goal text never wrecks readability.
    string CardTitle(string convTitle, string goal)
    {
        // A generic Copilot auto-title ("会話" / "Chat" / "新しいチャット") carries no information,
        // so prefer the goal-derived heading -- otherwise every SWE worker card just reads "会話"
        // and w0..w7 are indistinguishable at a glance.
        string ct = (convTitle ?? "").Trim();
        bool genericCt = ct.Length <= 4 || ct == "会話" || ct == "新しいチャット"
                         || ct.Equals("Chat", StringComparison.OrdinalIgnoreCase)
                         || ct.Equals("New chat", StringComparison.OrdinalIgnoreCase);
        if (!string.IsNullOrEmpty(ct) && !genericCt) return Trunc(ct, 90);
        if (string.IsNullOrEmpty(goal)) return string.IsNullOrEmpty(ct) ? "" : Trunc(ct, 90);
        string[] lines = goal.Replace("\r", "").Split('\n');
        for (int i = 0; i < lines.Length; i++)
        {
            string ln = lines[i].Trim();
            if (ln.StartsWith("==") && ln.IndexOf("issue", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                for (int j = i + 1; j < lines.Length; j++)
                {
                    string h = lines[j].Trim();
                    if (h.Length > 0) return Trunc(h, 90);
                }
            }
        }
        // From here on, work off the operator's own instruction rather than the raw goal, so a
        // fanout/continuation goal's headline names the job instead of the scaffolding we glued
        // onto it.
        string opGoal = OperatorInstruction(goal);
        if (string.IsNullOrEmpty(opGoal)) opGoal = goal;
        string[] opLines = opGoal.Replace("\r", "").Split('\n');
        foreach (string l in opLines)
        {
            string t = l.Trim();
            if (t.Length > 0 && !t.StartsWith("==") && !t.StartsWith("あなたは")
                && !t.StartsWith("対象") && !t.StartsWith("この"))
                return Trunc(t, 90);
        }
        foreach (string l in opLines) { string t = l.Trim(); if (t.Length > 0) return Trunc(t, 90); }
        return "";
    }

    // ── persistent history: finished/released tasks stack until cleared ───────────
    // "COULD NOT READ IT" AND "THERE IS NOTHING TO READ" ARE NOT THE SAME ANSWER, and this pair
    // of methods treated them as one. LoadHistory emptied _history first and swallowed every
    // failure, so a load that failed was indistinguishable from a first run -- and the next
    // SaveHistory then wrote the empty list over the file it had just failed to read.
    //
    // MEASURED 2026-09-08. The cockpit started at 18:21:10 with an empty _history. No worker
    // reached a terminal state for the next 24 minutes, so nothing saved and the file still held
    // its 44 rows at 18:44. At 18:45 a run archived its first worker, SaveHistory fired, and 44
    // rows became 1. Nothing reported anything. Set against 218 runs' worth of transcripts on
    // disk, the 44 rows that survived were themselves the remains of earlier rounds of this --
    // the oldest was from 03:40 the same morning.
    //
    // The trigger for that particular failed load is not established, and this does not pretend
    // to fix it. What it fixes is the consequence: a load that fails now refuses to let the
    // save destroy the evidence, and the unreadable file is kept under a dated name rather than
    // overwritten, so the next reader still has something to look at.
    bool _historyLoaded;          // false = the last LoadHistory could not read the file

    void LoadHistory()
    {
        _history = new List<object>();
        _archivedKeys = new System.Collections.Generic.HashSet<string>();
        _historyLoaded = false;
        try
        {
            // Absent is a legitimately empty history -- a first run. That is a successful load.
            if (!File.Exists(_historyPath)) { _historyLoaded = true; return; }
            var arr = _js.DeserializeObject(File.ReadAllText(_historyPath, Encoding.UTF8)) as object[];
            // Present but not an array is CORRUPT, not empty: do not claim it loaded.
            if (arr == null) return;
            foreach (object o in arr)
            {
                _history.Add(o);
                var d = o as Dictionary<string, object>;
                if (d != null && d.ContainsKey("key")) _archivedKeys.Add(S(d, "key"));
            }
            _historyLoaded = true;
        }
        catch (Exception) { }
        if (!_historyLoaded) PreserveUnreadableHistory();
    }

    // Keep what could not be read. Renaming rather than copying means the next save writes a new
    // file instead of appending to a damaged one, and the original bytes are still on disk for
    // whoever asks what went wrong.
    void PreserveUnreadableHistory()
    {
        try
        {
            if (!File.Exists(_historyPath)) return;
            string kept = _historyPath + ".unreadable-" + DateTime.Now.ToString("yyyyMMdd-HHmmss");
            if (!File.Exists(kept)) File.Move(_historyPath, kept);
        }
        catch (Exception) { }
    }

    void SaveHistory()
    {
        // A SAVE AFTER A FAILED LOAD IS A DELETION. _history holds only what this session
        // archived, so writing it over a file whose contents were never read replaces every
        // earlier row with the handful from this run. Refusing costs the current session's rows
        // until the next clean start; writing costs every row ever recorded.
        if (!_historyLoaded) return;
        try { File.WriteAllText(_historyPath, _js.Serialize(_history), new UTF8Encoding(false)); }
        catch (Exception) { }
    }
    // The STABLE per-run+worker key (run identity + worker name). `started` is absent from
    // some per-worker dicts, so it's read from the root snapshot. Matches the archive scheme.
    static string WorkerKey(string started, Dictionary<string, object> w)
    { return (started ?? "") + "#" + S(w, "name"); }

    void LoadHidden()
    {
        _hiddenKeys = new System.Collections.Generic.HashSet<string>();
        try
        {
            if (!File.Exists(_hiddenPath)) return;
            var arr = _js.DeserializeObject(File.ReadAllText(_hiddenPath, Encoding.UTF8)) as object[];
            if (arr == null) return;
            foreach (object o in arr)
                if (o != null) _hiddenKeys.Add(o.ToString());
        }
        catch (Exception) { }
    }

    void SaveHidden()
    {
        try
        {
            var list = new List<object>();
            foreach (string k in _hiddenKeys) list.Add(k);
            File.WriteAllText(_hiddenPath, _js.Serialize(list), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    void ClearHistory()
    {
        // RECORD THE DECISION BEFORE TOUCHING THE DATA. Renaming the file aside protects the
        // BYTES; it does not protect the CHOICE, and those are not the same thing. .fleet/
        // socket_route.jsonl is append-only and still holds every conversation this button
        // discards, so a rebuild from it reconstructs exactly what was cleared -- which is what
        // happened on 2026-09-13: 3,469 rows the operator had deleted came back, perfectly, and
        // the tool that did it had no way to know it was undoing an instruction rather than
        // repairing a loss. This line is the way to know. tools/rebuild_history.py reads it as a
        // watermark and withholds everything at or before it.
        //
        // FIRST, not last: a clear interrupted between the rename and the log would leave the
        // history gone and the reason unrecorded, which is precisely the state this prevents.
        int clearedRows = _history.Count;
        try
        {
            File.AppendAllText(_clearedLogPath,
                _js.Serialize(new Dictionary<string, object> {
                    { "ts", NowUnix() },
                    { "iso", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") },
                    { "rows", clearedRows },
                    { "by", "cockpit/clear_history" },
                }) + "\n", new UTF8Encoding(false));
        }
        catch (Exception) { }
        _history.Clear(); _archivedKeys.Clear();
        // RENAMED, NOT DELETED -- the same rule PreserveUnreadableHistory already applies to
        // this exact file. The button still clears the history; the bytes are still on disk for
        // whoever asks what went wrong. .fleet/history.json went missing once and the only code
        // that could have removed it was this line, with no confirmation in front of it and
        // nothing left behind to read. An absent history is indistinguishable from a first run
        // by design (see LoadHistory), so nothing would have reported it either.
        try
        {
            if (File.Exists(_historyPath))
            {
                string kept = _historyPath + ".cleared-" + DateTime.Now.ToString("yyyyMMdd-HHmmss");
                if (File.Exists(kept)) File.Delete(_historyPath);
                else File.Move(_historyPath, kept);
            }
        }
        catch (Exception) { }
        try
        {
            var st = ReadStatus();
            bool runningFlag = st != null && st.ContainsKey("running") && Convert.ToBoolean(st["running"])
                               && !(st.ContainsKey("idle") && Convert.ToBoolean(st["idle"]));
            // A LIVE run updates status.json ~once/second. If "running" is true but the
            // file is STALE (the run crashed / was killed without writing a final snapshot,
            // e.g. a send failure), it is not actually live -- so allow clearing it.
            // Otherwise a dead run's card sticks forever and Clear appears to do nothing.
            double updated = 0;
            try { if (st != null && st.ContainsKey("updated")) updated = Convert.ToDouble(st["updated"]); }
            catch (Exception) { }
            bool stale = updated > 0 && (NowUnix() - updated) > 15;
            bool live = runningFlag && !stale;

            if (!live)
            {
                // Run is finished/dead: a clean reset -- blank the snapshot AND forget hides.
                File.WriteAllText(_statusPath,
                    "{\"total\":0,\"done_count\":0,\"running\":false,\"idle\":true,\"workers\":[]}",
                    new UTF8Encoding(false));
                _hiddenKeys.Clear();
                SaveHidden();
            }
            else
            {
                // Run is LIVE: the runner rewrites status.json each second, so we can't blank it
                // (it would just reappear, and would clobber running workers). Instead HIDE the
                // TERMINAL cards persistently -- the runner regenerating them won't un-hide them.
                // Running (non-terminal) workers are NEVER hidden: a live task vanishing is a hazard.
                object wo; string started = S(st, "started");
                if (st != null && st.TryGetValue("workers", out wo) && wo is object[])
                {
                    bool any = false;
                    foreach (object o in (object[])wo)
                    {
                        var w = o as Dictionary<string, object>;
                        if (w == null || !IsTerminalWorker(w)) continue;
                        if (_hiddenKeys.Add(WorkerKey(started, w))) any = true;
                    }
                    if (any) SaveHidden();
                }
            }
        }
        catch (Exception) { }
        ForceRender();
    }
    // The finished run's last archive pass. See the call site for why it exists and why it
    // may run only once. ArchiveTerminal is already idempotent through _archivedKeys; the
    // once-per-run key is what keeps a CLEARED history clear, because clearing empties
    // _archivedKeys and a per-tick pass would refill it from the frozen snapshot.
    void ArchiveRunTailOnce(Dictionary<string, object> root)
    {
        if (root == null) return;
        string started = S(root, "started");
        if (string.IsNullOrEmpty(started)) return;
        if (_tailArchivedRunStarted == started) return;
        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        if (((object[])wo).Length == 0) return;
        _tailArchivedRunStarted = started;
        ArchiveTerminal(root);
    }

    void ArchiveTerminal(Dictionary<string, object> root)
    {
        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        string started = S(root, "started");
        bool added = false;
        foreach (object o in (object[])wo)
        {
            var w = (Dictionary<string, object>)o;
            string status = S(w, "status");
            // MUST MATCH relay/relay_fleet.py's TERMINAL. `content_refused` was missing from
            // both terminal tests in this file while Python has set it since it was added, so
            // a worker that ended that way was never archived -- and worse, IsTerminalWorker
            // returning false for it stops MaybeAutoArchive for the WHOLE run, so one refused
            // goal could cost every other card in that run its history entry.
            bool terminal = status == "done" || status == "stuck" || status == "maxturns"
                            || status == "error" || status == "cancelled"
                            || status == "content_refused";
            if (!terminal) continue;
            string conv = S(w, "conv_url");
            // STABLE key per run+worker (conv_url is absent in the final snapshot, so
            // keying on it would double-archive the same task).
            string key = started + "#" + S(w, "name");
            if (_archivedKeys.Contains(key)) continue;
            _archivedKeys.Add(key);
            var e = new Dictionary<string, object>();
            e["key"] = key; e["goal"] = S(w, "goal"); e["status"] = status;
            e["goal_summary"] = S(w, "goal_summary");
            e["conv_title"] = S(w, "conv_title");
            e["outcome"] = S(w, "outcome"); e["conv_url"] = conv;
            // Carry the disk transcript path + worker name so a HISTORY row can still show the
            // full body: conv_url is empty in the final snapshot (and for the new agent), so
            // without these a completed conversation strands on "本文はまだ取得できません" even
            // though the jsonl transcript exists on disk.
            e["transcript"] = S(w, "transcript"); e["name"] = S(w, "name");
            e["submitter"] = S(w, "submitter");
            // CARRY THE VERDICT, NOT JUST THE STATUS. relay_fleet.py warns beside its own
            // panel ledger that anything reaching an analysis only through history.json is
            // "hostage to a second program choosing to carry the field" -- this is that
            // program, and it was not carrying this one.
            //
            // The worker keeps a three-state answer: null = never checked, false = the
            // acceptance check ran and failed, true = it passed. `status` cannot express
            // that: "done" covers both "proved" and "never asked". Dropping it here is why
            // the self-improvement dashboard reports verify_rate 0.0 -- it reads this file,
            // and the field has never been in it. Measured: 44 archived rows, none carrying
            // `verified`, while the live snapshot for the same worker carries it.
            //
            // run_id names the run so a row joins to its transcripts and to the other
            // ledgers; `key` is "<epoch>#<worker>", unique per row and joining to nothing.
            // BOTH ARCHIVE SITES GET THIS. Fixing one and leaving the other is the exact
            // shape of the misses this file already records.
            e["verified"] = w.ContainsKey("verified") ? w["verified"] : null;
            e["verify_attempts"] = I(w, "verify_attempts");
            e["run_id"] = S(w, "run_id");
            // THE ADMISSION-TIME ID (codex-plan item 1, 2026-09-09). Not the same thing as
            // run_id just above -- run_id names the SWEEP this worker ran in, jid names the
            // ADMITTED GOAL, unique per submission. BOTH ARCHIVE SITES GET THIS, same rule
            // as run_id's own comment: fixing one and leaving the other repeats the miss.
            e["jid"] = S(w, "jid");
            e["turn"] = I(w, "turn"); e["seq"] = _history.Count;
            e["ts"] = NowUnix();   // P2: archived-at timestamp -> date-group subheaders in History
            CarryTimeline(e, w, started);
            _history.Add(e);
            added = true;
        }
        if (added) SaveHistory();
    }

    // THE EXECUTION TIMELINE'S ONLY REAL SOURCE, and it was being dropped on the floor.
    //
    // The timeline panel has two modes: real phase transitions from `phase_events`, and a
    // two-point estimate derived from conversation turns, labelled "(会話ターンから推定)". The
    // real mode has never once fired for a past task -- measured on the live archive: 150 rows,
    // ZERO with phase_events. The fleet records them per worker, and both archive builders here
    // copied goal/status/outcome/transcript/turn and simply did not copy this one, so every
    // history row fell back to the estimate and showed 投入 and 終了 and nothing between.
    //
    // ONE HELPER, CALLED FROM BOTH. The two builders had already drifted into the identical
    // omission; adding the line twice would set them up to drift again the next time a field
    // is added.
    void CarryTimeline(Dictionary<string, object> e, Dictionary<string, object> w, string started)
    {
        object pe;
        if (w.TryGetValue("phase_events", out pe) && pe != null) e["phase_events"] = pe;
        // The run's start, so the timeline has an anchor even when a worker's own first event
        // is missing. Kept separate from "ts", which is when the row was ARCHIVED -- those are
        // different moments and conflating them is what made an end time wrong before.
        if (!string.IsNullOrEmpty(started)) e["run_started"] = started;
        object st;
        if (w.TryGetValue("started_ts", out st) && st != null) e["started_ts"] = st;
        object en;
        if (w.TryGetValue("ended_ts", out en) && en != null) e["ended_ts"] = en;
    }

    // Client-side archive of ONE worker into the persisted history + hide its card. Unlike the
    // per-worker 解放 (which writes a command file and needs a LIVE fleet to consume it), this works
    // with the fleet stopped -- so a finished or stale card can be moved to history any time.
    void _archiveOne(Dictionary<string, object> w)
    {
        if (w == null) return;
        string started = _lastRoot != null ? S(_lastRoot, "started") : "";
        string key = started + "#" + S(w, "name");
        if (!_archivedKeys.Contains(key))
        {
            _archivedKeys.Add(key);
            var e = new Dictionary<string, object>();
            e["key"] = key; e["goal"] = S(w, "goal"); e["status"] = S(w, "status");
            e["goal_summary"] = S(w, "goal_summary");
            e["conv_title"] = S(w, "conv_title"); e["outcome"] = S(w, "outcome");
            e["conv_url"] = S(w, "conv_url");
            // see _archiveTerminal: carry transcript path + name so the history row can show the
            // full disk transcript even when conv_url is empty.
            e["transcript"] = S(w, "transcript"); e["name"] = S(w, "name");
            e["submitter"] = S(w, "submitter");
            // Same three fields as the terminal-archive path above, and for the same
            // reason -- a row archived by hand must not be poorer than one archived
            // automatically, or the history depends on which route retired the worker.
            e["verified"] = w.ContainsKey("verified") ? w["verified"] : null;
            e["verify_attempts"] = I(w, "verify_attempts");
            e["run_id"] = S(w, "run_id");
            // THE ADMISSION-TIME ID (codex-plan item 1, 2026-09-09). Not the same thing as
            // run_id just above -- run_id names the SWEEP this worker ran in, jid names the
            // ADMITTED GOAL, unique per submission. BOTH ARCHIVE SITES GET THIS, same rule
            // as run_id's own comment: fixing one and leaving the other repeats the miss.
            e["jid"] = S(w, "jid");
            e["turn"] = I(w, "turn"); e["seq"] = _history.Count;
            e["ts"] = NowUnix();   // P2: archived-at timestamp -> date-group subheaders in History
            CarryTimeline(e, w, started);
            _history.Add(e);
        }
        _hiddenKeys.Add(WorkerKey(started, w));
    }

    void ArchiveAndHide(Dictionary<string, object> w) { _archiveOne(w); SaveHistory(); SaveHidden(); ForceRender(); }

    // Bulk: move every TERMINAL worker currently shown into history (the 完了-divider button).
    void ArchiveAllTerminal()
    {
        if (_toolbarShown == null) return;
        foreach (Dictionary<string, object> w in new List<Dictionary<string, object>>(_toolbarShown))
            if (IsTerminalWorker(w)) _archiveOne(w);
        SaveHistory(); SaveHidden(); ForceRender();
    }

    // Bulk-clear EVERY shown worker -- used by Stop-all when the run has gone STALE (no live fleet to
    // consume a stop command). Unlike ArchiveAllTerminal this clears non-terminal cards too, because a
    // stale run's "running" workers are frozen leftovers, not actually executing. Makes Stop-all do
    // something visible even with the driver dead, instead of silently writing an unread command file.
    void ArchiveAllStale()
    {
        if (_toolbarShown == null) return;
        foreach (Dictionary<string, object> w in new List<Dictionary<string, object>>(_toolbarShown))
            _archiveOne(w);
        SaveHistory(); SaveHidden(); ForceRender();
    }
}
