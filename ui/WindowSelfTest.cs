// WindowSelfTest.cs -- `--selftest` for both UI binaries: build the main window the ordinary
// way, run one dispatcher cycle, exit 0; or print the exception and exit non-zero.
//
// WHY. Everything else that tests this UI either reads its source or compiles a piece of it
// alone. Neither notices the one failure the operator sees first: a window that throws while
// it is being constructed, so double-clicking the exe does nothing at all. This constructs
// the real window through its real constructor -- the same `new ChatWindow()` / `new
// CockpitWindow(path)` Main uses -- inside the real exe that csc produced from the real Build
// line. ui/test_both_windows_can_be_constructed.py builds both and runs this on each.
//
// WHAT IT DOES NOT DO, deliberately:
//   * It never Show()s the window. Showing raises Loaded, which in the chat window writes
//     settings.txt on a first run -- a test must not change the operator's settings. So this
//     proves construction plus whatever the constructor queued on the dispatcher, not layout,
//     rendering or Loaded.
//   * No network, no fleet. `Active` is read by the few places that would reach out: the
//     chat's bridge probe and HttpGet refuse, the cockpit's health poll (which can start the
//     stack) does not start, and both windows get a fresh scratch directory as their .fleet
//     instead of the one beside the exe.
//
// Compiled into BOTH binaries (see ui/rebuild_ui.ps1's Build lines). Legacy csc, C# 5.
using System;
using System.IO;
using System.Windows;
using System.Windows.Threading;

static class WindowSelfTest
{
    /// True for the whole life of a process started with --selftest. Read from the command line
    /// rather than set by Main, so it is right however early a static initializer asks.
    public static readonly bool Active = Requested(Environment.GetCommandLineArgs());

    static bool Requested(string[] argv)
    {
        // argv[0] is the exe itself; Main tests its own args[0], which is argv[1] here.
        return argv != null && argv.Length >= 2
            && argv[1].Equals("--selftest", StringComparison.OrdinalIgnoreCase);
    }

    /// A fresh, empty directory under %TEMP% -- the selftest's stand-in for .fleet.
    public static string ScratchDir(string what)
    {
        string d = Path.Combine(Path.GetTempPath(), "ui-selftest-" + what + "-" + Guid.NewGuid().ToString("N").Substring(0, 8));
        Directory.CreateDirectory(d);
        return d;
    }

    /// Construct, pump once, report. The exit code is the verdict; stdout/stderr say why.
    public static int Run(Func<Window> make)
    {
        try
        {
            var app = new Application();
            app.ShutdownMode = ShutdownMode.OnExplicitShutdown;
            Window w = make();
            if (w == null) throw new InvalidOperationException("the window factory returned null");
            // ONE DISPATCHER CYCLE: everything the constructor queued at Background priority or
            // above runs now, and an exception from any of it comes out of this call.
            Dispatcher.CurrentDispatcher.Invoke(DispatcherPriority.Background, new Action(delegate { }));
            string name = w.GetType().Name;
            w.Close();
            Console.Out.WriteLine("selftest ok: " + name);
            Console.Out.Flush();
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("selftest FAILED: " + ex);
            Console.Error.Flush();
            return 3;
        }
    }
}
