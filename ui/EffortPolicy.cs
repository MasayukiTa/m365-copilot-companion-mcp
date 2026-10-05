// EffortPolicy.cs -- the cockpit's reading of the effort policy. WPF-FREE ON PURPOSE: it links no
// WPF assembly, so ui/test_the_effort_policy_says_what_is_in_effect.py can compile
// it with csc beside ui/harness/EffortPolicyHarness.cs and run it. FleetCockpit.cs calls it.
//
// The Python side decides (relay/effort_policy.py: env MCP_EFFORT_POLICY > settings.txt
// effort_policy > off) and writes the answer into .fleet/status.json; this file only parses the
// setting line and words what the runner reported. It never guesses: no report -> no text.
//
// Legacy csc, C# 5.
using System;
using System.Collections.Generic;

public static class EffortPolicyView
{
    public const string Key = "effort_policy";

    //: The persisted tokens, in combo order. English on disk, like the effort= setting.
    public static readonly string[] Modes = { "off", "shadow", "on" };

    public static bool IsMode(string v)
    {
        return v == "off" || v == "shadow" || v == "on";
    }

    /// <summary>The mode named by one settings.txt line, or null when the line is not an
    /// effort_policy= line or the value is not off|shadow|on (the caller then KEEPS its default,
    /// off). `effort=on` does not match: the key is compared whole, up to the '='. A leading
    /// byte order mark is tolerated and the value is case-insensitive, as in Python.</summary>
    public static string ParseMode(string line)
    {
        if (line == null) return null;
        string ln = line.TrimStart('﻿').Trim();
        if (!ln.StartsWith(Key + "=", StringComparison.Ordinal)) return null;
        string v = ln.Substring(Key.Length + 1).Trim().ToLowerInvariant();
        return IsMode(v) ? v : null;
    }

    public static string ModeLabel(string mode, bool ja)
    {
        switch (mode)
        {
            case "off": return ja ? "オフ" : "Off";
            case "shadow": return ja ? "シャドウ(記録のみ)" : "Shadow (record only)";
            case "on": return ja ? "オン(初期effortを適用)" : "On (apply initial effort)";
        }
        return mode ?? "";
    }

    //: Short form for the "in effect" line (the option label minus its gloss).
    static string ShortMode(string mode, bool ja)
    {
        switch (mode)
        {
            case "off": return ja ? "オフ" : "off";
            case "shadow": return ja ? "シャドウ" : "shadow";
            case "on": return ja ? "オン" : "on";
        }
        return mode ?? "";
    }

    static string SourceLabel(string source, bool ja)
    {
        switch (source)
        {
            case "env": return ja ? "環境変数" : "environment";
            case "settings": return ja ? "設定" : "settings";
            case "default": return ja ? "既定" : "default";
        }
        return source ?? "";
    }

    public static string Help(bool ja)
    {
        return ja ? "オン=ファンアウト子の初期effortを1段下げる。走行中の切替は記録のみ。"
                  : "On = fan-out children start one level lower; live switching is record-only.";
    }

    //: Wording of WHEN a change lands, matching tools/settings_keys.py (each_gate).
    public static string TakeEffectTip(bool ja)
    {
        return ja ? "次のワーカー/ファンアウト/ターン評価から有効。再起動不要。決定済みのものは変わりません。"
                  : "Applies to the next worker / fan-out / turn evaluation, no restart. Decisions already made stand.";
    }

    /// <summary>What is really in effect, as the runner reported it. Null when it reported
    /// nothing usable (old runner): the cockpit then shows nothing rather than guessing.</summary>
    public static string Describe(string mode, string source, bool conflict, bool ja)
    {
        if (!IsMode(mode)) return null;
        return (ja ? "有効: " : "In effect: ") + ShortMode(mode, ja) + " (" + SourceLabel(source, ja) + ")";
    }

    /// <summary>The warning for an environment override that beats the selected value; null when
    /// nothing conflicts. `selected` is what the combo / settings.txt says.</summary>
    public static string ConflictText(string envMode, string selected, bool conflict, bool ja)
    {
        if (!conflict || !IsMode(envMode)) return null;
        string sel = IsMode(selected) ? selected : "?";
        return ja ? "環境変数 MCP_EFFORT_POLICY=" + envMode + " が設定 " + sel + " を上書き中"
                  : "env MCP_EFFORT_POLICY=" + envMode + " overrides this setting (" + sel + ")";
    }

    static string Str(Dictionary<string, object> d, string k)
    {
        object v;
        return (d != null && d.TryGetValue(k, out v) && v != null) ? v.ToString() : "";
    }

    /// <summary>The per-worker pill text ("推論 max"), or null when the runner sent no level
    /// (absent field = no badge).</summary>
    public static string BadgeText(string level, string source, Dictionary<string, object> lastSwitch, bool ja)
    {
        if (string.IsNullOrEmpty(level)) return null;
        return (ja ? "推論 " : "effort ") + level;
    }

    /// <summary>The pill's tooltip: source, and the last switch labelled record-only (live
    /// switching changes nothing yet, so it must not read as something that happened).</summary>
    public static string BadgeTip(string level, string source, Dictionary<string, object> lastSwitch, bool ja)
    {
        if (string.IsNullOrEmpty(level)) return null;
        string tip = "source: " + (string.IsNullOrEmpty(source) ? "?" : source);
        if (lastSwitch != null && lastSwitch.Count > 0)
        {
            string reason = Str(lastSwitch, "reason");
            tip += "; last: " + Str(lastSwitch, "from") + "->" + Str(lastSwitch, "to")
                 + (reason.Length > 0 ? " (" + reason + ")" : "")
                 + (ja ? " (記録のみ)" : " (shadow)");
        }
        return tip;
    }
}

// The fan-out switch's words and parsing, WPF-free for the same reason as EffortPolicyView (and
// kept in this file so the shipped build list does not change). The Python side decides
// (relay/task_router.py:_wants_fanout, relay/fleet_runner.py `--fanout/--no-fanout`) and writes
// what the coordinator was STARTED with into status.json "fanout_run"; this only words it.
public static class FanoutView
{
    public const string Key = "fanout";

    //: Absent settings key means ON. Must equal tools/settings_keys.py default("fanout")
    //: (tests/test_fanout_default_on_agrees.py compares them).
    public const bool DefaultOn = true;

    //: The persisted tokens, in combo order.
    public static readonly string[] Modes = { "on", "off" };

    /// <summary>The value of one fanout= line exactly as Python's settings_fanout reads it:
    /// on|1|true|yes -> true, anything else -> false. Null when the line is not a fanout= line.</summary>
    public static bool? ParseSetting(string line)
    {
        if (line == null) return null;
        string ln = line.TrimStart('﻿').Trim();
        if (!ln.StartsWith(Key + "=", StringComparison.Ordinal)) return null;
        string v = ln.Substring(Key.Length + 1).Trim().ToLowerInvariant();
        return v == "on" || v == "1" || v == "true" || v == "yes";
    }

    public static string Token(bool on) { return on ? "on" : "off"; }

    public static string Label(bool ja) { return ja ? "分割実行" : "Fan-out"; }

    public static string ModeLabel(string mode, bool ja)
    {
        if (mode == "on") return ja ? "オン(長い依頼を分けて並列実行)" : "On (split long goals, run in parallel)";
        if (mode == "off") return ja ? "オフ(1会話で実行)" : "Off (one conversation per goal)";
        return mode ?? "";
    }

    public static string Help(bool ja)
    {
        return ja ? "オン=長い依頼を独立したサブタスクに分けて並列実行し、結果を統合します。各依頼は個別に判定され、収まる依頼は分割されません。"
                  : "On = long goals are split into independent sub-tasks, run in parallel and merged. Each goal is judged separately; a goal that fits is not split.";
    }

    public static string TakeEffectTip(bool ja)
    {
        return ja ? "次に起動するコーディネータから反映されます(稼働中の実行は変わりません)。"
                  : "Applies from the next coordinator start; a run already going keeps what it started with.";
    }

    /// <summary>What the coordinator was really started with. Null when it reported nothing
    /// usable (old runner / no run yet): nothing is shown rather than a guess.</summary>
    public static string Describe(bool hasReport, bool enabled, string source, bool ja)
    {
        if (!hasReport) return null;
        string on = enabled ? (ja ? "オン" : "on") : (ja ? "オフ" : "off");
        string how = source == "flag" ? (enabled ? "--fanout" : "--no-fanout")
                                      : (ja ? "既定" : "default");
        return (ja ? "稼働中: " : "In effect: ") + on + " (" + how + ")";
    }

    /// <summary>The note shown when the running coordinator's value differs from the selected
    /// one (the change lands at the next start); null when they agree or nothing was reported.</summary>
    public static string PendingText(bool hasReport, bool enabled, bool selected, bool ja)
    {
        if (!hasReport || enabled == selected) return null;
        return ja ? "選択は次回起動から反映" : "selection applies from next start";
    }
}

// The per-tree fan-out budget's words, bounds and parsing, WPF-free (same file as FanoutView so
// the shipped build list does not change). The Python side decides (relay/fanout_budget.py reads
// the four keys at every split and relay/fleet_runner.py exports the limits in force as
// status.json "fanout_budget"); this only parses setting lines, clamps what the operator types
// and words the report. Index order is the order of Keys everywhere.
public static class FanoutBudgetView
{
    //: settings.txt key names; must equal tools/settings_keys.py (tests/test_fanout_budget.py).
    public static readonly string[] Keys =
        { "fanout_max_total", "fanout_max_active", "fanout_max_turns", "fanout_max_wall_min" };

    //: Absent-key defaults; must equal the registry defaults.
    public static readonly int[] Defaults = { 24, 3, 400, 120 };

    //: Bounds; must equal relay/fanout_budget.py BOUNDS.
    public static readonly int[] Lows = { 2, 1, 1, 1 };
    public static readonly int[] Highs = { 1000, 100, 1000000, 10080 };

    //: Keys of the limits object the runner exports, in the same order.
    public static readonly string[] ReportKeys = { "total", "active", "turns", "wall_min" };

    public static int Clamp(int idx, int v)
    {
        return Math.Max(Lows[idx], Math.Min(Highs[idx], v));
    }

    /// <summary>The clamped value of one settings.txt line for key `idx`, or null when the line
    /// is not that key (`fanout_max_total` does not match `fanout_max_totals`) or the value is
    /// not a whole number (the caller KEEPS its current value, as Python falls back to default).</summary>
    public static int? ParseLine(int idx, string line)
    {
        if (line == null) return null;
        string ln = line.TrimStart('﻿').Trim();
        string key = Keys[idx];
        if (!ln.StartsWith(key + "=", StringComparison.Ordinal)) return null;
        int v;
        if (!int.TryParse(ln.Substring(key.Length + 1).Trim(), System.Globalization.NumberStyles.Integer,
                          System.Globalization.CultureInfo.InvariantCulture, out v)) return null;
        return Clamp(idx, v);
    }

    /// <summary>What the operator typed, clamped; false when it is not a whole number.</summary>
    public static bool TryParseInput(int idx, string text, out int value)
    {
        value = 0;
        int v;
        if (text == null || !int.TryParse(text.Trim(), System.Globalization.NumberStyles.Integer,
                                          System.Globalization.CultureInfo.InvariantCulture, out v)) return false;
        value = Clamp(idx, v);
        return true;
    }

    public static string GroupLabel(bool ja) { return ja ? "分割の上限" : "Fan-out budget"; }

    public static string ShortLabel(int idx, bool ja)
    {
        switch (idx)
        {
            case 0: return ja ? "総数" : "total";
            case 1: return ja ? "同時" : "active";
            case 2: return ja ? "ターン" : "turns";
            case 3: return ja ? "分" : "min";
        }
        return "";
    }

    public static string Help(int idx, bool ja)
    {
        switch (idx)
        {
            case 0: return ja ? "1つの分割ツリーが持てるワーカー総数(統合用に1つ確保)"
                              : "Most workers one fan-out tree may hold (one slot is kept for the merge)";
            case 1: return ja ? "ツリーがさらに分割を求めるとき、同時に動いていてよいワーカー数"
                              : "Most of a tree's workers that may be running when it asks to split more";
            case 2: return ja ? "ツリー全体で使えるターン数の上限" : "Most conversation turns one tree may use";
            case 3: return ja ? "ツリーの最初の分割からの経過分数の上限" : "Most minutes since a tree's first split";
        }
        return "";
    }

    //: Wording of WHEN a change lands, matching tools/settings_keys.py (each_gate).
    public static string TakeEffectTip(bool ja)
    {
        return ja ? "次の分割判断から有効。再起動不要。分割済みのツリーは変わりません。"
                  : "Applies to the next split decision, no restart. Trees already split keep going.";
    }

    /// <summary>The limits the coordinator reports it applies (status.json "fanout_budget"),
    /// as total/active/turns/wall_min; null when it reported nothing usable (old runner).</summary>
    public static string Describe(int[] limits, bool ja)
    {
        if (limits == null || limits.Length != 4) return null;
        return (ja ? "稼働中: 総数 " : "In effect: total ") + limits[0]
             + (ja ? " / 同時 " : " / active ") + limits[1]
             + (ja ? " / ターン " : " / turns ") + limits[2]
             + (ja ? " / " : " / ") + limits[3] + (ja ? "分" : " min");
    }

    /// <summary>The note shown when what the runner applies differs from the boxes (the boxes
    /// persist at once; the runner reads them at its next split); null when they agree.</summary>
    public static string PendingText(int[] limits, int[] selected, bool ja)
    {
        if (limits == null || selected == null || limits.Length != 4 || selected.Length != 4) return null;
        for (int i = 0; i < 4; i++)
            if (limits[i] != selected[i])
                return ja ? "選択は次の分割から反映" : "selection applies from the next split";
        return null;
    }
}

// The split-depth selector's words and parsing, WPF-free (same file as FanoutView). The Python
// side decides: relay/fanout.py reads `fanout_max_depth` at every split and caps it while the
// merge of nested splits is not enabled; relay/fleet_runner.py exports both the configured and
// the effective depth as status.json "fanout_depth". The screen words the REPORT, so it never
// claims a depth the coordinator is not applying.
public static class FanoutDepthView
{
    //: must equal tools/settings_keys.py and relay/fanout.py DEPTH_SETTING_KEY / BOUNDS.
    public const string Key = "fanout_max_depth";
    public const int Default = 1;
    public const int Low = 1;
    public const int High = 3;

    public static readonly string[] Modes = { "1", "2", "3" };

    public static int Clamp(int v) { return Math.Max(Low, Math.Min(High, v)); }

    /// <summary>The clamped value of one fanout_max_depth= line; null when the line is not that
    /// key or the value is not a whole number (the caller keeps its current value).</summary>
    public static int? ParseLine(string line)
    {
        if (line == null) return null;
        string ln = line.TrimStart('﻿').Trim();
        if (!ln.StartsWith(Key + "=", StringComparison.Ordinal)) return null;
        int v;
        if (!int.TryParse(ln.Substring(Key.Length + 1).Trim(), System.Globalization.NumberStyles.Integer,
                          System.Globalization.CultureInfo.InvariantCulture, out v)) return null;
        return Clamp(v);
    }

    public static string Label(bool ja) { return ja ? "分割の深さ" : "Split depth"; }

    public static string ModeLabel(string mode, bool ja)
    {
        if (mode == "1") return ja ? "1(最上位のみ)" : "1 (top level only)";
        if (mode == "2") return ja ? "2(子も分割可)" : "2 (children may split)";
        if (mode == "3") return ja ? "3(孫も分割可)" : "3 (grandchildren may split)";
        return mode ?? "";
    }

    public static string Help(bool ja)
    {
        return ja ? "分割がさらに分割できる段数の上限。1=最上位の依頼だけが分割されます。統合の準備が整うまで、実際に使われる深さは下の「稼働中」表示のとおりです。"
                  : "How many levels deep a split may nest. 1 = only the top-level goal splits. Until nested merging is enabled, the depth actually used is the one shown as in effect.";
    }

    public static string TakeEffectTip(bool ja)
    {
        return ja ? "次の分割判断から有効。再起動不要。分割済みのツリーは変わりません。"
                  : "Applies to the next split decision, no restart. Trees already split keep going.";
    }

    /// <summary>What the coordinator reports (configured / effective); null when it reported
    /// nothing usable. Says plainly when the effective depth is below the configured one.</summary>
    public static string Describe(int configured, int effective, string reason, bool ja)
    {
        string s = (ja ? "稼働中: 深さ " : "In effect: depth ") + effective;
        if (effective < configured)
        {
            s += ja ? " (設定 " + configured + " はまだ有効ではありません" : " (setting " + configured + " is not active";
            if (!string.IsNullOrEmpty(reason))
                s += ja ? ": 入れ子統合の設定がオフ" : ": " + reason;
            s += ")";
        }
        return s;
    }

    /// <summary>The note shown when the configured depth the runner reports differs from the
    /// selection (the runner reads the key at its next split); null when they agree.</summary>
    public static string PendingText(int configured, int selected, bool ja)
    {
        if (configured == selected) return null;
        return ja ? "選択は次の分割から反映" : "selection applies from the next split";
    }
}

// The hierarchical-merge switch's words and parsing, WPF-free (same file as FanoutDepthView).
// The Python side decides: relay/fanout.py reads `fanout_hierarchical_merge` at every split and,
// only while it is on, lets `fanout_max_depth` above 1 take effect; relay/fleet_runner.py exports
// the state in force as status.json "fanout_depth".hierarchical_merge. Default off.
public static class HierarchicalMergeView
{
    //: must equal tools/settings_keys.py and relay/fanout.py HIERARCHICAL_SETTING_KEY / _DEFAULT
    //: (tests/test_hierarchical_merge_setting.py compares them).
    public const string Key = "fanout_hierarchical_merge";
    public const string Default = "off";

    public static readonly string[] Modes = { "off", "on" };

    public static bool IsMode(string v) { return v == "off" || v == "on"; }

    /// <summary>The mode named by one fanout_hierarchical_merge= line; null when the line is not
    /// that key or the value is not off|on (the caller keeps its value, off). A byte order mark
    /// is tolerated and the value is case-insensitive, as in Python.</summary>
    public static string ParseLine(string line)
    {
        if (line == null) return null;
        string ln = line.TrimStart('﻿').Trim();
        if (!ln.StartsWith(Key + "=", StringComparison.Ordinal)) return null;
        string v = ln.Substring(Key.Length + 1).Trim().ToLowerInvariant();
        return IsMode(v) ? v : null;
    }

    public static string Label(bool ja) { return ja ? "入れ子の統合" : "Hierarchical merge"; }

    public static string ModeLabel(string mode, bool ja)
    {
        if (mode == "off") return ja ? "オフ" : "Off";
        if (mode == "on") return ja ? "オン" : "On";
        return mode ?? "";
    }

    public static string Help(bool ja)
    {
        return ja ? "オン=分割の深さ2以上が有効になります。まず小さな依頼で確認してから使ってください。"
                  : "On lets fan-out depth above 1 take effect; verify with small goals first.";
    }

    public static string TakeEffectTip(bool ja)
    {
        return ja ? "次の分割判断から有効。再起動不要。分割済みのツリーは変わりません。"
                  : "Applies to the next split decision, no restart. Trees already split keep going.";
    }

    /// <summary>The state the coordinator reports; null when it reported nothing usable.</summary>
    public static string Describe(string reported, bool ja)
    {
        if (!IsMode(reported)) return null;
        return (ja ? "稼働中: " : "In effect: ") + ModeLabel(reported, ja);
    }

    /// <summary>The note shown when the state the runner reports differs from the selection (the
    /// runner re-reads the key at its next split); null when they agree.</summary>
    public static string PendingText(string reported, string selected, bool ja)
    {
        if (!IsMode(reported) || reported == selected) return null;
        return ja ? "選択は次の分割から反映" : "selection applies from the next split";
    }
}

// The sibling write-scope selector's words and parsing, WPF-free (same file as FanoutView). The
// Python side decides: relay/write_scope.py reads `fanout_write_scope` at every sweep and, in
// shadow, records overlapping writes among siblings of one fan-out campaign; relay/fleet_runner.py
// exports {mode, overlaps_seen} as status.json "fanout_write_scope". SHADOW ONLY: there is no
// enforcing value, so nothing is ever blocked and no prompt or output changes.
public static class WriteScopeView
{
    //: must equal tools/settings_keys.py and relay/write_scope.py KEY / MODES.
    public const string Key = "fanout_write_scope";
    public const string Default = "off";

    public static readonly string[] Modes = { "off", "shadow" };

    public static bool IsMode(string v) { return v == "off" || v == "shadow"; }

    /// <summary>The mode named by one fanout_write_scope= line; null when the line is not that
    /// key or the value is not off|shadow (the caller keeps its value, off). A byte order mark
    /// is tolerated and the value is case-insensitive, as in Python.</summary>
    public static string ParseLine(string line)
    {
        if (line == null) return null;
        string ln = line.TrimStart('\uFEFF').Trim();
        if (!ln.StartsWith(Key + "=", StringComparison.Ordinal)) return null;
        string v = ln.Substring(Key.Length + 1).Trim().ToLowerInvariant();
        return IsMode(v) ? v : null;
    }

    public static string Label(bool ja) { return ja ? "兄弟の書込み範囲" : "Sibling write scope"; }

    public static string ModeLabel(string mode, bool ja)
    {
        if (mode == "off") return ja ? "オフ" : "Off";
        if (mode == "shadow") return ja ? "シャドウ(記録のみ)" : "Shadow (record only)";
        return mode ?? "";
    }

    public static string Help(bool ja)
    {
        return ja ? "記録のみ: 何もブロックしません。同じ分割の兄弟が同じファイルへ書いたとき、または他の兄弟の担当に書いたときに記録します。"
                  : "Record only: nothing is blocked. Records when two siblings of one split write the same file, or one writes into a path another sibling's step names.";
    }

    public static string TakeEffectTip(bool ja)
    {
        return ja ? "次の巡回から有効。再起動不要。"
                  : "Applies from the next sweep, no restart.";
    }

    /// <summary>What the coordinator reports (mode / overlaps seen); null when the mode is not
    /// usable (old runner): the cockpit then shows nothing rather than guessing.</summary>
    public static string Describe(string mode, int overlapsSeen, bool ja)
    {
        if (!IsMode(mode)) return null;
        string head = (ja ? "稼働中: " : "In effect: ") + (mode == "shadow" ? (ja ? "シャドウ(記録のみ)" : "shadow (record only)") : (ja ? "オフ" : "off"));
        if (mode == "shadow")
            head += ja ? " / 検出 " + overlapsSeen + " 件" : " / " + overlapsSeen + " overlap(s) seen";
        return head;
    }

    /// <summary>The note shown when the mode the runner reports differs from the selection (the
    /// runner re-reads the key at its next sweep); null when they agree.</summary>
    public static string PendingText(string reported, string selected, bool ja)
    {
        if (!IsMode(reported) || reported == selected) return null;
        return ja ? "選択は次の巡回から反映" : "selection applies from the next sweep";
    }
}

// The split-group line on a fan-out parent card, WPF-free (same file as FanoutView). Reads one
// entry of status.json "groups" (relay/family_view.py) and, for a root group, the usage of its
// tree (status.json "tree_budget") and the depth report ("fanout_depth"). Every nesting field is
// optional: a group without them is worded exactly as a flat group always was. OWNER RULES: the
// goal text is never shown (only counts and labels); one short line; no popup.
public static class GroupTreeView
{
    static string Str(Dictionary<string, object> d, string k)
    {
        object v;
        return (d != null && d.TryGetValue(k, out v) && v != null) ? v.ToString() : "";
    }

    static Dictionary<string, object> Obj(Dictionary<string, object> d, string k)
    {
        object v;
        return (d != null && d.TryGetValue(k, out v)) ? v as Dictionary<string, object> : null;
    }

    static double Num(Dictionary<string, object> d, string k, double dflt)
    {
        object v;
        if (d == null || !d.TryGetValue(k, out v) || v == null) return dflt;
        try { return Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture); }
        catch (Exception) { return dflt; }
    }

    static int Int(Dictionary<string, object> d, string k) { return (int)Num(d, k, 0); }

    static string Count(Dictionary<string, object> ch, string key, string label)
    {
        int n = Int(ch, key);
        return n > 0 ? label + " " + n : "";
    }

    /// <summary>Nesting depth of the group, 0 for a root or a flat group (field absent).</summary>
    public static int Depth(Dictionary<string, object> g)
    {
        return Math.Max(0, Math.Min(6, Int(g, "depth")));
    }

    /// <summary>Left indent in pixels for a nested group's line.</summary>
    public static int IndentPx(Dictionary<string, object> g) { return Depth(g) * 14; }

    public static string MergeWords(Dictionary<string, object> g, bool ja)
    {
        string st = Str(g, "merge_state");
        if (st == "waiting_on_subgroups") return ja ? "下位グループの統合待ち" : "waiting on sub-groups";
        if (st == "unknown") return ja ? "不明" : "unknown";
        return Str(g, "merge_label");
    }

    /// <summary>One line: split group, children counts, merge state, then (only when the group
    /// carries them) sub-group count and a missing-parent note. A flat group's text is the same
    /// as before nesting existed.</summary>
    public static string Line(Dictionary<string, object> g, bool ja)
    {
        if (g == null) return "";
        var ch = Obj(g, "children") ?? new Dictionary<string, object>();
        var parts = new List<string>();
        string[] counts = {
            Count(ch, "queued", ja ? "待機" : "queued"),
            Count(ch, "running", ja ? "実行中" : "running"),
            Count(ch, "done", ja ? "完了" : "done"),
            Count(ch, "failed", ja ? "失敗" : "failed"),
            Count(ch, "interrupted", ja ? "中断" : "interrupted") };
        foreach (string p in counts) if (p.Length > 0) parts.Add(p);
        string head = (ja ? "分割グループ 子" : "Split group, ") + Int(g, "children_total")
                      + (ja ? "件" : " parts");
        string body = parts.Count > 0 ? ": " + string.Join(" · ", parts.ToArray()) : "";
        string ml = MergeWords(g, ja);
        string s = head + body + (ml.Length > 0 ? (ja ? " / 統合: " : " / merge: ") + ml : "");
        object ids;
        int subs = 0;
        if (g.TryGetValue("child_group_ids", out ids) && ids is object[]) subs = ((object[])ids).Length;
        subs += Int(g, "child_group_ids_truncated");
        int desc = Int(g, "descendant_count");
        if (subs > 0 || desc > 0)
        {
            if (subs < 1) subs = desc;
            s += ja ? " / 下位グループ " + subs + "件" + (desc > subs ? " (全部で " + desc + "件)" : "")
                    : " / sub-groups: " + subs + (desc > subs ? " (" + desc + " in all)" : "");
        }
        object orph;
        if (g.TryGetValue("orphan", out orph) && orph is bool && (bool)orph)
            s += ja ? " / 親グループが見つかりません" : " / parent group not found";
        return s;
    }

    /// <summary>Usage of the root group's tree: used/limit for total, active, turns, minutes.
    /// Only for a root group (depth 0) whose root has an entry in tree_budget; null otherwise.
    /// level: 0 normal, 1 at 80% or more of any limit, 2 at 100% or more.</summary>
    public static string BudgetText(Dictionary<string, object> statusRoot, Dictionary<string, object> g,
                                    bool ja, out int level)
    {
        level = 0;
        if (g == null || Depth(g) != 0) return null;
        var tb = Obj(statusRoot, "tree_budget");
        var e = tb != null ? Obj(tb, Str(g, "root_id").Length > 0 ? Str(g, "root_id") : Str(g, "campaign_id")) : null;
        if (e == null) return null;
        var lim = Obj(e, "limits") ?? Obj(statusRoot, "fanout_budget");
        var parts = new List<string>();
        for (int i = 0; i < FanoutBudgetView.ReportKeys.Length; i++)
        {
            string k = FanoutBudgetView.ReportKeys[i];
            if (e == null || !e.ContainsKey(k) || e[k] == null) continue;
            double used = Num(e, k, 0);
            double max = Num(lim, k, 0);
            string s = FanoutBudgetView.ShortLabel(i, ja) + " " + (long)used;
            if (max > 0)
            {
                s += "/" + (long)max;
                if (used * 100 >= max * 100) level = 2;
                else if (used * 100 >= max * 80 && level < 1) level = 1;
            }
            parts.Add(s);
        }
        if (parts.Count == 0) return null;
        return (ja ? "使用量 " : "usage ") + string.Join(" · ", parts.ToArray());
    }

    /// <summary>The note when the coordinator applies a split depth below the configured one.</summary>
    public static string DepthNote(Dictionary<string, object> statusRoot, bool ja)
    {
        var fd = Obj(statusRoot, "fanout_depth");
        if (fd == null || !fd.ContainsKey("configured") || !fd.ContainsKey("effective")) return null;
        int conf = Int(fd, "configured"), eff = Int(fd, "effective");
        if (eff >= conf) return null;
        return ja ? "深さは " + eff + " までに制限中: 階層統合は未有効"
                  : "depth capped at " + eff + ": hierarchical merge not enabled";
    }

    /// <summary>The second, collapsible line under a root group: usage + depth note. null when
    /// neither is reported (a flat status.json shows nothing extra).</summary>
    public static string ExtraText(Dictionary<string, object> statusRoot, Dictionary<string, object> g,
                                   bool ja, out int level)
    {
        string b = BudgetText(statusRoot, g, ja, out level);
        string n = (g != null && Depth(g) == 0) ? DepthNote(statusRoot, ja) : null;
        if (b == null) return n;
        return n == null ? b : b + " / " + n;
    }
}

// The auto-resume switch's words and parsing, WPF-free (same file as WriteScopeView). The Python
// side decides: relay/fleet_resume.py reads `fleet_auto_resume` and the supervisor consults the
// loop guard each cycle; relay/fleet_runner.py exports {setting, last_decision, pending_snapshots}
// as status.json "auto_resume" (relay/fleet_resume.py also patches an idle status.json, so the
// screen can say what the gate decided although no coordinator is alive). Default on.
public static class AutoResumeView
{
    //: must equal tools/settings_keys.py and relay/fleet_resume.py AUTO_RESUME_SETTING_KEY / _DEFAULT.
    public const string Key = "fleet_auto_resume";
    public const string Default = "on";

    public static readonly string[] Modes = { "off", "on" };

    public static bool IsMode(string v) { return v == "off" || v == "on"; }

    /// <summary>The mode named by one fleet_auto_resume= line; null when the line is not that key
    /// or the value is not off|on (the caller keeps its value, on). A byte order mark is tolerated
    /// and the value is case-insensitive, as in Python.</summary>
    public static string ParseLine(string line)
    {
        if (line == null) return null;
        string ln = line.TrimStart('﻿').Trim();
        if (!ln.StartsWith(Key + "=", StringComparison.Ordinal)) return null;
        string v = ln.Substring(Key.Length + 1).Trim().ToLowerInvariant();
        return IsMode(v) ? v : null;
    }

    public static string Label(bool ja) { return ja ? "中断した実行を自動で再開" : "Auto-resume interrupted runs"; }

    public static string ModeLabel(string mode, bool ja)
    {
        if (mode == "off") return ja ? "オフ" : "Off";
        if (mode == "on") return ja ? "オン" : "On";
        return mode ?? "";
    }

    public static string Help(bool ja)
    {
        return ja ? "オン=コーディネーターが落ちて中断した実行を、人に聞かずに再開します。再開は最大3回、間隔は5分×2^回数で、停止指示のあと・実行中・空き容量が下限未満のときは再開しません。待機中の依頼は中断した実行の再開を先に待ちます。"
                  : "On resumes a run interrupted by a coordinator crash without asking. At most 3 automatic resumes, 5 min x 2^n apart; never after a stop, while a coordinator is running, or under the disk floor. Queued goals wait for the interrupted run to be resumed first.";
    }

    public static string TakeEffectTip(bool ja)
    {
        return ja ? "次の監視サイクル(約15秒)から有効。再起動不要。"
                  : "Applies from the next supervisor cycle (about 15 s), no restart.";
    }

    /// <summary>The gate's last decision in words; null when there is none.</summary>
    public static string DecisionText(string decision, string reason, bool ja)
    {
        if (string.IsNullOrEmpty(decision)) return null;
        if (decision == "resumed") return ja ? "再開しました" : "resumed";
        string why = ReasonText(reason, ja);
        if (decision == "waiting") return (ja ? "待機中" : "waiting") + (why != null ? " (" + why + ")" : "");
        return (ja ? "再開しません" : "not resumed") + (why != null ? " (" + why + ")" : "");
    }

    static string ReasonText(string reason, bool ja)
    {
        switch (reason)
        {
            case "backoff": return ja ? "次の試行まで間隔を空けています" : "waiting out the retry interval";
            case "below_floor": return ja ? "空き容量が下限未満" : "free space is under the floor";
            case "max_resumes": return ja ? "自動再開の上限3回に達しました" : "reached the 3-resume limit";
            case "stop_requested": return ja ? "停止が指示されていました" : "a stop was requested";
            case "same_crash_no_more_space": return ja ? "同じ原因で空きが増えていません" : "same crash and no more free space";
            case "disk_full_no_more_space": return ja ? "ディスク満杯で空きが増えていません" : "disk was full and has not freed up";
            case "coordinator_live": return ja ? "別のコーディネーターが稼働中" : "a coordinator is already running";
            default: return string.IsNullOrEmpty(reason) ? null : reason;
        }
    }

    /// <summary>What the runner/supervisor report: the setting in force, whether an interrupted run
    /// is waiting, and what the gate last decided; null when the setting is not usable (old runner),
    /// so the cockpit shows nothing rather than guessing.</summary>
    public static string Describe(string reported, string decision, string reason, int pending, bool ja)
    {
        if (!IsMode(reported)) return null;
        string head = (ja ? "稼働中: " : "In effect: ") + ModeLabel(reported, ja);
        head += pending > 0
            ? (ja ? " / 再開待ちの中断 " + pending + " 件" : " / " + pending + " interrupted run(s) waiting")
            : (ja ? " / 再開待ちなし" : " / none waiting");
        string d = DecisionText(decision, reason, ja);
        if (d != null) head += (ja ? " / 直近の判断: " : " / last decision: ") + d;
        return head;
    }

    /// <summary>The note shown when the setting the supervisor reports differs from the selection
    /// (it re-reads the key each cycle); null when they agree.</summary>
    public static string PendingText(string reported, string selected, bool ja)
    {
        if (!IsMode(reported) || reported == selected) return null;
        return ja ? "選択は次の監視サイクルから反映" : "selection applies from the next cycle";
    }
}

// The tool-call check's interval, WPF-free (same file as AutoResumeView). The Python side decides:
// bridge/copilot_bridge.py reads `tool_probe_idle_min` every few minutes and sends a probe message
// only when no real tool call has proved the path for that long; it reports its decisions in
// .fleet/tool_probe_state.json (and the bridge's /status "probe"). Default 30 minutes, 0 = never.
public static class ToolProbeView
{
    //: must equal tools/settings_keys.py and tools/tool_probe.py IDLE_MIN_KEY / IDLE_MIN_DEFAULT.
    public const string Key = "tool_probe_idle_min";
    public const string Default = "30";

    public static readonly string[] Choices = { "0", "15", "30", "60" };

    public static bool IsChoice(string v)
    {
        for (int i = 0; i < Choices.Length; i++) if (Choices[i] == v) return true;
        return false;
    }

    /// <summary>The choice named by one tool_probe_idle_min= line; null when the line is not that key
    /// or the value is not one the control offers (the caller keeps its value; Python still honours
    /// a hand-edited 5..1440, and the in-effect line then shows what is really running).</summary>
    public static string ParseLine(string line)
    {
        if (line == null) return null;
        string ln = line.TrimStart('﻿').Trim();
        if (!ln.StartsWith(Key + "=", StringComparison.Ordinal)) return null;
        string v = ln.Substring(Key.Length + 1).Trim();
        return IsChoice(v) ? v : null;
    }

    /// <summary>How long a green tool-call check stays valid, in minutes: the interval plus ten
    /// minutes of slack for the probe's own round trip (30-180 s), never less than the 20 minutes
    /// the check always allowed. An unknown interval (old bridge) keeps the 20.</summary>
    public static double StaleAfterMin(double intervalMin)
    {
        if (intervalMin <= 0) return 20.0;
        return Math.Max(20.0, intervalMin + 10.0);
    }

    public static string Label(bool ja) { return ja ? "ツール呼び出し確認の間隔(待機時)" : "Tool-call check interval (idle)"; }

    public static string ChoiceLabel(string v, bool ja)
    {
        if (v == "0") return ja ? "実行しない" : "Never";
        return ja ? v + "分" : v + " min";
    }

    public static string Help(bool ja)
    {
        return ja ? "ツール呼び出しが実際に成功してから、この時間が経っても何の証拠も無いときだけ、確認用メッセージをCopilotへ送ります。実際のツール呼び出しが成功していれば送りません。失敗した後は間隔を倍々(最大2時間)に延ばします。「実行しない」では確認メッセージを送らず、画面は「未確認」になります(緑にはなりません)。"
                  : "A check message is sent to Copilot only when no real tool call has succeeded for this long. A real successful call counts as proof and no message is sent. After a failed check the wait doubles (up to 2 hours). 'Never' sends no check message and the screen shows 'not checked' (never green).";
    }

    public static string TakeEffectTip(bool ja)
    {
        return ja ? "約5分以内に有効。再起動不要。" : "Applies within about 5 minutes, no restart.";
    }

    static string AgoText(double nowUnix, double ts, bool ja)
    {
        if (ts <= 0) return null;
        int m = (int)Math.Max(0, Math.Round((nowUnix - ts) / 60.0));
        return ja ? m + "分前" : m + " min ago";
    }

    static string ReasonText(string reason, double nowUnix, double evidenceTs, bool ja)
    {
        switch (reason)
        {
            case "fleet_evidence":
                string ago = AgoText(nowUnix, evidenceTs, ja);
                return ja ? "実際のツール呼び出しを確認済みのため送信せず" + (ago != null ? " (" + ago + ")" : "")
                          : "not sent: a real tool call was seen" + (ago != null ? " (" + ago + ")" : "");
            case "user_turn": return ja ? "送信せず: 利用者の発言の直後" : "not sent: right after a user turn";
            case "page_busy": return ja ? "送信せず: ページ使用中" : "not sent: page busy";
            case "disabled_setting": return ja ? "設定で停止中" : "switched off by the setting";
            case "disabled_env": return ja ? "環境変数 MCP_TOOL_PROBE_SEC で停止中" : "switched off by MCP_TOOL_PROBE_SEC";
            default: return string.IsNullOrEmpty(reason) ? null : reason;
        }
    }

    /// <summary>What the bridge reports ("probe" in its /status, mirrored in the state file). Null
    /// when the report is unusable (old bridge), so the screen shows nothing instead of guessing.</summary>
    public static string Describe(bool known, bool enabled, double intervalMin, string source,
                                  double lastSent, string skipReason, double skipTs, int skipped,
                                  double evidenceTs, int backoffFailures, double nowUnix, bool ja)
    {
        if (!known) return null;
        string head = ja ? "稼働中: " : "In effect: ";
        if (!enabled)
        {
            head += ja ? "確認メッセージを送りません(画面は「未確認」)" : "no check message is sent (the screen shows 'not checked')";
            if (source == "env") head += ja ? " / 環境変数が設定より優先" : " / the environment variable overrides the setting";
            return head;
        }
        int im = (int)Math.Round(intervalMin);
        head += ja ? im + "分ごと(実際のツール呼び出しが無いときだけ)" : "every " + im + " min (only when no real tool call has been seen)";
        if (source == "env") head += ja ? " / 環境変数が設定より優先" : " / the environment variable overrides the setting";
        string sent = AgoText(nowUnix, lastSent, ja);
        head += ja ? " / 直近の送信: " + (sent ?? "なし") : " / last sent: " + (sent ?? "none");
        if (skipped > 0) head += ja ? " / 送らずに済んだ回数 " + skipped : " / skipped " + skipped + "x";
        string why = ReasonText(skipReason, nowUnix, evidenceTs, ja);
        if (why != null && skipTs > 0) head += ja ? " / 直近: " + why : " / last: " + why;
        if (backoffFailures > 0) head += ja ? " / 失敗後の待機を延長中(連続" + backoffFailures + "回)" : " / backing off after " + backoffFailures + " failure(s)";
        return head;
    }

    /// <summary>The note shown when the interval the bridge reports differs from the selection.</summary>
    public static string PendingText(bool known, bool enabled, double intervalMin, string source, string selected, bool ja)
    {
        if (!known || source == "env") return null;
        double sel;
        if (!double.TryParse(selected, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out sel)) return null;
        double reported = enabled ? intervalMin : 0.0;
        if (Math.Abs(reported - sel) < 0.01) return null;
        return ja ? "選択は約5分以内に反映" : "selection applies within about 5 minutes";
    }
}

// The merge-conversation selector's words and parsing, WPF-free. KEPT AT THE END OF THE FILE on
// purpose: shape tests slice this file from one view class to the end of the next, and a view
// dropped between two of them has already broken one. The Python side decides:
// relay/conversation_saving.py reads `merge_conversation` at every split and every merge and, only
// for `parent`, continues the splitting worker's conversation in the merge instead of opening a
// fresh aggregator conversation; relay/fleet_runner.py exports the state in force as status.json
// "conversation_saving" {merge_conversation, aggregators_saved, unsent_created}. Default fresh.
public static class MergeConversationView
{
    //: must equal tools/settings_keys.py and relay/conversation_saving.py MERGE_SETTING_KEY / _DEFAULT
    //: (tests/test_conversation_saving.py compares them).
    public const string Key = "merge_conversation";
    public const string Default = "fresh";

    public static readonly string[] Modes = { "fresh", "parent" };

    public static bool IsMode(string v) { return v == "fresh" || v == "parent"; }

    /// <summary>The mode named by one merge_conversation= line; null when the line is not that key
    /// or the value is not fresh|parent (the caller keeps its value, fresh). A byte order mark is
    /// tolerated and the value is case-insensitive, as in Python.</summary>
    public static string ParseLine(string line)
    {
        if (line == null) return null;
        string ln = line.TrimStart('﻿').Trim();
        if (!ln.StartsWith(Key + "=", StringComparison.Ordinal)) return null;
        string v = ln.Substring(Key.Length + 1).Trim().ToLowerInvariant();
        return IsMode(v) ? v : null;
    }

    public static string Label(bool ja) { return ja ? "統合を実行する会話" : "Merge conversation"; }

    public static string ModeLabel(string mode, bool ja)
    {
        if (mode == "fresh") return ja ? "新しい会話" : "Fresh";
        if (mode == "parent") return ja ? "分割した会話" : "Parent";
        return mode ?? "";
    }

    public static string Help(bool ja)
    {
        return ja ? "新しい会話=統合のたびに新しい会話を開きます(従来どおり)。分割した会話=分割を担当した会話の続きで統合し、会話を1つ節約します。統合は別の依頼として扱われ、確認や順序は変わりません。まず小さな依頼で確認してください。"
                  : "Fresh opens a new conversation for every merge (as before). Parent runs the merge in the splitting worker's own conversation and saves one conversation per family; the merge is still its own job with the same checks and order. Verify with small goals first.";
    }

    public static string TakeEffectTip(bool ja)
    {
        return ja ? "次の分割・統合の判断から有効。再起動不要。"
                  : "Applies to the next split or merge decision, no restart.";
    }

    /// <summary>The state the coordinator reports plus the savings so far; null when it reported
    /// nothing usable.</summary>
    public static string Describe(string reported, int saved, int unsent, bool ja)
    {
        if (!IsMode(reported)) return null;
        string head = (ja ? "稼働中: " : "In effect: ") + ModeLabel(reported, ja);
        head += ja ? " / 節約した統合会話 " + saved + " 件 / 未送信で終わった会話 " + unsent + " 件"
                   : " / merge conversations saved " + saved + " / opened but never sent " + unsent;
        return head;
    }

    /// <summary>The note shown when the state the runner reports differs from the selection (the
    /// runner re-reads the key at its next split or merge); null when they agree.</summary>
    public static string PendingText(string reported, string selected, bool ja)
    {
        if (!IsMode(reported) || reported == selected) return null;
        return ja ? "選択は次の分割・統合から反映" : "selection applies from the next split or merge";
    }
}
