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
