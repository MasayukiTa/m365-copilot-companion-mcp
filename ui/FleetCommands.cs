// FleetCommands.cs -- the ONE way either UI binary hands a command to a running fleet.
//
// ONE FILE PER COMMAND, WHICH IS WHY THERE IS NO LOCK AND NO MERGE. Until this file existed,
// three separate hand-rolled read-modify-write copies of the same routine wrote
// .fleet/commands.json: FleetCockpit.cs had ReadCommands/WriteCommands, CopilotChat.cs had
// AppendCommand AND a second copy inside EnqueueToFleet (which additionally wrote a BOM while
// the other two did not). FleetCockpit.exe and CopilotChat.exe are separately built processes
// (see ui/rebuild_ui.ps1), so no lock taken inside one of them could have ordered the other:
// each read the whole file, added its own key and wrote the file back, and whichever wrote
// second silently deleted what the first had queued. A lost add_goal/steer is indistinguishable
// from one that was never sent. Two further failures came out of the same read-modify-write:
// the Python reader DELETES the file once it has read it, so a writer that read before the
// delete and wrote after it RESURRECTED an already-consumed command (duplicate delivery); and
// a torn File.WriteAllText produced a file the reader deleted rather than kept as .bad.
//
// Giving each command its own uniquely named file removes the read-modify-write entirely:
// nothing merges, so nothing can clobber, and no lock is needed across the two binaries.
// This is the same contract relay/task_router.py's write_command already implements on the
// Python side -- read its docstring; the C# side was simply never migrated to it. The reader
// is relay/fleet_runner.py's read_commands, which takes <state_dir>/commands.d/*.json in
// filename order, skips .tmp deliberately, and deletes each file as it consumes it.
//
// Compiled by legacy csc (Framework64 v4.0.30319, C# 5) into BOTH exes. NO expression-bodied
// members, NO string interpolation, NO null-conditional -- classic method bodies only.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

static class FleetCommands
{
    // Strictly increasing within this process. Starts at -1 so the first Increment yields 0,
    // matching the Python writer's itertools.count().
    static int _seq = -1;

    static readonly JavaScriptSerializer _js = new JavaScriptSerializer();

    static readonly DateTime _epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

    /// <summary>
    /// Leave ONE command for the running fleet as a file of its own. `patch` carries only the
    /// keys this caller means to send (e.g. {"set_maxtabs":4}); it is never merged with anything.
    /// Returns false if the command could not be written -- callers that tell the operator
    /// "sent" must check it.
    /// </summary>
    public static bool Write(string stateDir, Dictionary<string, object> patch)
    {
        try
        {
            if (string.IsNullOrEmpty(stateDir) || patch == null) return false;
            string dir = Path.Combine(stateDir, "commands.d");
            Directory.CreateDirectory(dir);

            // time_ns, then a per-process sequence, then a random tail. ALL THREE ARE
            // LOAD-BEARING, and the reason for each is measured in task_router.write_command:
            // the reader orders by filename, and the clock alone cannot carry send order --
            // Windows advances it in ~15.6 ms steps, so three commands sent in a row stamp the
            // SAME nanosecond (measured there: 0,1,2 in, 0,2,1 out). The counter makes one
            // process's writes strictly ordered whatever the clock does. The random tail stays
            // because the counter is per-process: two processes inside one tick would otherwise
            // land on the same name, and here a name collision means one command silently
            // overwrites another. Zero-padded so the names sort lexicographically, which is the
            // only ordering the reader (os.listdir + sorted) applies.
            long ns = (DateTime.UtcNow - _epoch).Ticks * 100L;
            int seq = Interlocked.Increment(ref _seq);
            string tail = Guid.NewGuid().ToString("N").Substring(0, 8);
            string name = ns.ToString("D19") + "-" + seq.ToString("D9") + "-" + tail;

            string path = Path.Combine(dir, name + ".json");
            string tmp = Path.Combine(dir, name + ".tmp");

            // Written to .tmp in the SAME directory and renamed, so a reader never sees half a
            // command; the reader skips .tmp for exactly that reason. utf-8 with NO BOM, always
            // -- the reader opens utf-8-sig and would tolerate one, but the three writers this
            // replaces disagreed about it, and a disagreement like that is a trap waiting on
            // whichever reader is added next.
            File.WriteAllText(tmp, _js.Serialize(patch), new UTF8Encoding(false));

            // The rename is retried briefly: on Windows a rename onto a path another process
            // holds open fails, and File.Move reports that as IOException.
            DateTime until = DateTime.UtcNow.AddSeconds(2.0);
            while (true)
            {
                try { File.Move(tmp, path); return true; }
                catch (IOException)
                {
                    if (DateTime.UtcNow > until)
                    {
                        try { File.Delete(tmp); }
                        catch (Exception) { }
                        return false;
                    }
                    Thread.Sleep(20);
                }
            }
        }
        catch (Exception) { return false; }
    }
}
