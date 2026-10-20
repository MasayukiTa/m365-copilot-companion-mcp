// FleetCockpit.Roster.cs -- who is working on what (agent roster), read-only.
// Part of `partial class CockpitWindow` (the other parts: ui/FleetCockpit.cs and ui/FleetCockpit.*.cs).
// Every part is listed on the Build "FleetCockpit" line of ui/rebuild_ui.ps1, which is the one list
// of what is compiled.
//
// `roster` in status.json is derived by relay/roster.py from the `workers` rows of the same file
// (contract in that module's docstring). Nothing is stored here: the text is built from the last
// snapshot and shown as the tooltip of the tabs chip in the header. No setting, no popup, no toast.
// Only goal_summary reaches the screen, never the long goal text. The file keeps its UTF-8 byte
// order mark, like the other parts, so csc reads the Japanese words correctly.
using System;
using System.Collections.Generic;
using System.Text;
using System.Windows;
using System.Windows.Controls;

partial class CockpitWindow
{
    const int RosterMaxLines = 24;   // the tooltip never grows past this many lines

    // The roster as plain lines, "" when the snapshot carries none (old runner, nobody listed).
    string RosterText(Dictionary<string, object> root)
    {
        Dictionary<string, object> r = root != null ? Obj(root, "roster") : null;
        if (r == null) return "";
        object co;
        if (!r.TryGetValue("campaigns", out co) || !(co is object[])) return "";
        bool ja = _lang == 0;
        var sb = new StringBuilder();
        sb.Append(ja ? "担当一覧" : "Roster");
        int lines = 0;
        foreach (object o in (object[])co)
        {
            var c = o as Dictionary<string, object>;
            if (c == null) continue;
            string cid = S(c, "campaign_id");
            if (lines >= RosterMaxLines) break;
            sb.Append('\n').Append(cid.Length == 0 ? (ja ? "単独" : "Standalone") : cid);
            object wo;
            if (!c.TryGetValue("workers", out wo) || !(wo is object[])) continue;
            foreach (object x in (object[])wo)
            {
                var w = x as Dictionary<string, object>;
                if (w == null) continue;
                if (lines >= RosterMaxLines) break;
                lines++;
                string role = S(w, "role");
                sb.Append("\n  ").Append(S(w, "name"))
                  .Append(" [").Append(role.Length == 0 ? "-" : role).Append('/').Append(S(w, "status")).Append("] t")
                  .Append(I(w, "turn"));
                string task = S(w, "task");
                if (task.Length > 0) sb.Append("  ").Append(task);
            }
        }
        int omitted = I(r, "omitted");
        if (omitted > 0)
            sb.Append('\n').Append(ja ? ("ほか " + omitted + " 人") : ("+" + omitted + " more"));
        return sb.ToString();
    }

    // Called once per live tick, after UpdateWorkerChip (which sets the plain hint). Replaces the
    // tooltip only when there is a roster to show.
    void ApplyRosterToChip(Dictionary<string, object> root)
    {
        if (_workerChipBorder == null || _workerChip == null) return;
        string text = RosterText(root);
        if (text.Length == 0) return;
        string hint = T("tabs_chip_hint");
        string full = (hint ?? "") + "\n\n" + text;
        _workerChip.ToolTip = full;
        _workerChipBorder.ToolTip = full;
    }
}
