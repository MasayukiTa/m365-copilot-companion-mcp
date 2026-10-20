// FleetCockpit.Settings.cs -- settings.txt location, LoadSettings and SaveKey.
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
    //: The same two-location rule as ApprovalPromptWindow above and tools/settings_path.py.
    //: This file carried TWO independent copies of the path and the audit found five more
    //: across the repo -- the fact that had six readers and no owner.
    static string SettingsFileNew
    { get { return Path.Combine(RepoRootForSettings(), ".config", "settings.txt"); } }

    static string SettingsFileOld
    {
        get
        {
            string app = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            if (string.IsNullOrEmpty(app))
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    ".copilot-bridge", "settings.txt");
            return Path.Combine(app, "copilot-bridge", "settings.txt");
        }
    }

    static string SettingsFile
    {
        get
        {
            try { if (File.Exists(SettingsFileNew)) return SettingsFileNew; } catch (Exception) { }
            try { if (File.Exists(SettingsFileOld)) return SettingsFileOld; } catch (Exception) { }
            return SettingsFileNew;
        }
    }

    static string SettingsFileForWrite
    {
        get
        {
            try { Directory.CreateDirectory(Path.GetDirectoryName(SettingsFileNew)); }
            catch (Exception) { }
            return SettingsFileNew;
        }
    }

    static string RepoRootForSettings()
    {
        try
        {
            string exe = System.Reflection.Assembly.GetExecutingAssembly().Location;
            DirectoryInfo d = new DirectoryInfo(Path.GetDirectoryName(exe));
            while (d != null && !Directory.Exists(Path.Combine(d.FullName, ".fleet")))
                d = d.Parent;
            if (d != null) return d.FullName;
        }
        catch (Exception) { }
        return Directory.GetCurrentDirectory();
    }

    // ── settings (shared with the chat) ──────────────────────────────────────────
    void LoadSettings()
    {
        try
        {
            if (!File.Exists(SettingsFile)) return;
            foreach (string ln in File.ReadAllLines(SettingsFile))
            {
                int v;
                if (ln.StartsWith("dark=")) _dark = ln.Substring(5).Trim() != "0";
                else if (ln.StartsWith("lang=") && int.TryParse(ln.Substring(5).Trim(), out v)) _lang = v;
                else if (ln.StartsWith("maxtabs=") && int.TryParse(ln.Substring(8).Trim(), out v)) _maxtabs = Math.Max(1, Math.Min(1000, v));
                else if (ln.StartsWith("autoscale_max=") && int.TryParse(ln.Substring(14).Trim(), out v)) _autoMax = Math.Max(1, Math.Min(1000, v));
                else if (ln.StartsWith("autoscale=")) _autoscale = ln.Substring(10).Trim() == "1";
                else if (ln.StartsWith("autoretry_max=") && int.TryParse(ln.Substring(14).Trim(), out v)) _autoRetryMax = Math.Max(1, Math.Min(3, v));
                else if (ln.StartsWith("autoretry=")) _autoRetry = ln.Substring(10).Trim() == "1";
                else if (ln.StartsWith("autoarchive=")) _autoArchive = ln.Substring(12).Trim() == "1";
                else if (ln.StartsWith("session_retention_days=") && int.TryParse(ln.Substring(23).Trim(), out v))
                    _retDays = Math.Max(0, Math.Min(3650, v));
                else if (ln.StartsWith("session_max_mb=") && int.TryParse(ln.Substring(15).Trim(), out v))
                    _retMb = Math.Max(0, Math.Min(100000, v));
                else if (ln.StartsWith("rate_ceiling_rpm=") && int.TryParse(ln.Substring(17).Trim(), out v))
                    _rateCeiling = Math.Max(0, Math.Min(10000, v));
                else if (ln.StartsWith("fleet_log_days=") && int.TryParse(ln.Substring(15).Trim(), out v))
                    _fleetLogDays = Math.Max(1, Math.Min(3650, v));
                else if (ln.StartsWith("sidebar_section_cap=") && int.TryParse(ln.Substring(20).Trim(), out v))
                    _sidebarCap = Math.Max(0, Math.Min(500, v));
                else if (ln.StartsWith("fleet_store_days=") && int.TryParse(ln.Substring(17).Trim(), out v))
                    _fleetStoreDays = Math.Max(1, Math.Min(3650, v));
                else if (ln.StartsWith("fleet_scratch_days=") && int.TryParse(ln.Substring(19).Trim(), out v))
                    _fleetScratchDays = Math.Max(1, Math.Min(3650, v));
                else if (ln.StartsWith("fleet_compress_hours=") && int.TryParse(ln.Substring(21).Trim(), out v))
                    _fleetCompressHours = Math.Max(1, Math.Min(720, v));
                else if (ln.StartsWith("disk_floor_gb="))
                {
                    double df;
                    if (double.TryParse(ln.Substring(14).Trim(), System.Globalization.NumberStyles.Float,
                                        System.Globalization.CultureInfo.InvariantCulture, out df))
                        _diskFloor = Math.Max(0.0, Math.Min(100.0, df));
                }
                else if (ln.StartsWith("ram_floor_mb="))
                {
                    double rf;
                    if (double.TryParse(ln.Substring(13).Trim(), System.Globalization.NumberStyles.Float,
                                        System.Globalization.CultureInfo.InvariantCulture, out rf))
                        _ramFloor = Math.Max(0.0, Math.Min(65536.0, rf));
                }
                else if (ln.StartsWith("ui_scale="))
                {
                    string sv = ln.Substring(9).Trim();
                    if (sv.Equals("auto", StringComparison.OrdinalIgnoreCase))
                    { _uiAuto = true; _uiScaleLoaded = true; }
                    else
                    {
                        double us;
                        if (double.TryParse(sv, System.Globalization.NumberStyles.Float,
                                            System.Globalization.CultureInfo.InvariantCulture, out us))
                        { _uiAuto = false; _uiScale = Math.Max(0.8, Math.Min(2.0, us)); _uiScaleLoaded = true; }
                    }
                }
                else if (ln.StartsWith("ui_scale_target="))
                {
                    double ut;
                    if (double.TryParse(ln.Substring(16).Trim(), System.Globalization.NumberStyles.Float,
                                        System.Globalization.CultureInfo.InvariantCulture, out ut))
                    { _scaleTarget = Math.Max(0.8, Math.Min(3.0, ut)); _scaleTargetLoaded = true; }
                }
                else if (ln.StartsWith(EffortPolicyView.Key + "="))
                {
                    string epm = EffortPolicyView.ParseMode(ln);
                    if (epm != null) _effortPolicy = epm;
                }
                else if (ln.StartsWith("effort="))
                {
                    string ef = ln.Substring(7).Trim();
                    if (ef == "min" || ef == "max" || ef == "ultra" || ef == "auto") _effort = ef;
                }
                else if (ln.StartsWith("approval="))
                {
                    string ap = ln.Substring(9).Trim();
                    if (ap == "run" || ap == "plan" || ap == "auto") _approval = ap;
                }
                else if (ln.StartsWith("fanout="))
                {
                    bool? fxv = FanoutView.ParseSetting(ln);   // same reading as Python's settings_fanout
                    if (fxv.HasValue) _fanout = fxv.Value;
                }
                else if (ln.StartsWith("fanout_write_scope="))
                {
                    string wsv = WriteScopeView.ParseLine(ln);   // off|shadow only; junk keeps the value
                    if (wsv != null) _wsVal = wsv;
                }
                else if (ln.StartsWith("fanout_load_routing="))
                {
                    string lrv = LoadRoutingView.ParseLine(ln);   // off|shadow only; junk keeps the value
                    if (lrv != null) _lrVal = lrv;
                }
                else if (ln.StartsWith("team_mode="))
                {
                    string tmv = TeamModeView.ParseLine(ln);   // off|shadow only; junk keeps the value
                    if (tmv != null) _tmVal = tmv;
                }
                else if (ln.StartsWith("relay_findings="))
                {
                    string rfv = RelayFindingsView.ParseLine(ln);   // off|shadow|on only; junk keeps the value
                    if (rfv != null) _rfVal = rfv;
                }
                else if (ln.StartsWith("agent_board_mode="))
                {
                    string abv = AgentBoardView.ParseLine(ln);   // off|p0 only; junk (and p1) keeps the value
                    if (abv != null) _abVal = abv;
                }
                else if (ln.StartsWith("org_version_record="))
                {
                    string ovv = OrgVersionView.ParseLine(ln);   // off|on only; junk keeps the value
                    if (ovv != null) _ovVal = ovv;
                }
                else if (ln.StartsWith("org_genotype_record="))
                {
                    string ogv = OrgGenotypeView.ParseLine(ln);   // off|on only; junk keeps the value
                    if (ogv != null) _ogVal = ogv;
                }
                else if (ln.StartsWith("org_shadow_eval="))
                {
                    string osv = OrgShadowView.ParseLine(ln);   // off|shadow only; junk (and on) keeps the value
                    if (osv != null) _osVal = osv;
                }
                else if (ln.StartsWith("org_ab="))
                {
                    string abv = OrgAbView.ParseLine(ln);   // off|shadow only; junk (and on) keeps the value
                    if (abv != null) _abVal = abv;
                }
                else if (ln.StartsWith("org_proposals="))
                {
                    string opv = OrgProposalView.ParseLine(ln);   // off|on only; junk keeps the value
                    if (opv != null) _opVal = opv;
                }
                else if (ln.StartsWith("fleet_auto_resume="))
                {
                    string arv = AutoResumeView.ParseLine(ln);   // off|on only; junk keeps the value
                    if (arv != null) _arVal = arv;
                }
                else if (ln.StartsWith("supervisor_self_restart="))
                {
                    string srv = SupervisorCodeView.ParseLine(ln);   // off|on only; junk keeps the value
                    if (srv != null) _srVal = srv;
                }
                else if (ln.StartsWith("tool_probe_idle_min="))
                {
                    string tpv = ToolProbeView.ParseLine(ln);   // 0|15|30|60 only; junk keeps the value
                    if (tpv != null) _tpVal = tpv;
                }
                else if (ln.StartsWith("resident_tools="))
                {
                    string rtv = ResidentToolsView.ParseLine(ln);   // off|on only; junk keeps the value
                    if (rtv != null) _rtVal = rtv;
                }
                else if (ln.StartsWith("tool_transport="))
                {
                    string trv = ToolRouteView.ParseLine(ln);   // connector|connector-less (bridge = old spelling); junk keeps the value
                    if (trv != null) _trVal = trv;
                }
                else if (ln.StartsWith("consent_auto_approve="))
                {
                    string cav = ConsentApproveView.ParseLine(ln);   // on|off only; junk keeps the value
                    if (cav != null) _caVal = cav;
                }
                else if (ln.StartsWith("result_cache="))
                {
                    string rcv = ResultCacheView.ParseLine(ln);   // off|shadow only; junk keeps the value
                    if (rcv != null) _rcVal = rcv;
                }
                else if (ln.StartsWith("merge_conversation="))
                {
                    string mcv = MergeConversationView.ParseLine(ln);   // fresh|parent only; junk keeps the value
                    if (mcv != null) _mcVal = mcv;
                }
                else if (ln.StartsWith("fanout_hierarchical_merge="))
                {
                    string hmv = HierarchicalMergeView.ParseLine(ln);   // off|on only; junk keeps the value
                    if (hmv != null) _hmVal = hmv;
                }
                else if (ln.StartsWith("fanout_max_depth="))
                {
                    int? fdv = FanoutDepthView.ParseLine(ln);   // clamped; junk keeps the value
                    if (fdv.HasValue) _fdVal = fdv.Value;
                }
                else if (ln.StartsWith("fanout_max_"))
                {
                    for (int fbi = 0; fbi < FanoutBudgetView.Keys.Length; fbi++)
                    {
                        int? fbv = FanoutBudgetView.ParseLine(fbi, ln);   // clamped; junk keeps the value
                        if (fbv.HasValue) _fbVals[fbi] = fbv.Value;
                    }
                }
                else if (ln.StartsWith("runtime="))
                {
                    string rt = ln.Substring(8).Trim().ToLower();
                    if (rt == "fleet" || rt == "durable") _runtimeMode = rt;
                }
            }
            _settingsMtime = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
        }
        catch (Exception) { }
    }
    void SaveKey(string key, string val)
    {
        try
        {
            var lines = new List<string>(); bool found = false;
            if (File.Exists(SettingsFile))
                foreach (string ln in File.ReadAllLines(SettingsFile))
                {
                    if (ln.StartsWith(key + "=")) { lines.Add(key + "=" + val); found = true; }
                    else lines.Add(ln);
                }
            if (!found) lines.Add(key + "=" + val);
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsFileForWrite));
            File.WriteAllText(SettingsFileForWrite, string.Join("\n", lines.ToArray()) + "\n", new UTF8Encoding(false));
            _settingsMtime = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
        }
        catch (Exception) { }
    }
}
