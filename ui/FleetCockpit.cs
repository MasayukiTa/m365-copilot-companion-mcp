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
