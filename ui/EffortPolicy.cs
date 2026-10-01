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
