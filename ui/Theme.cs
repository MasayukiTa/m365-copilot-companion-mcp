// Theme.cs -- single source of truth for the companion UI design tokens.
//
// Both executables compile this file in (FleetCockpit.exe and CopilotChat.exe), so the
// palette, status model and metrics live in exactly one place. Before this, the warm-gray
// "calm Codex" palette and the slate the sibling app palette were duplicated inline across
// FleetCockpit.cs / CopilotChat.cs / SelfImproveDashboard.cs and drifted apart.
//
// Design target (fleet_ui_redesign_spec.md): a quiet code-work surface -- light/dark warm
// neutrals, accent reserved for the single primary action, status shown as a small chip
// (never a full-card fill).
//
// NO COLOURED LEFT RAILS ON CONTENT BLOCKS. The line above used to recommend "a thin left
// rail + small chip", and that recommendation was read and followed when the self-improvement
// records were rebuilt; the operator's response was that they have said repeatedly they
// dislike the look -- a coloured bar down the left edge of a block reads as a sticky note.
// Corrected here rather than only at the call site, because a stale recommendation in the
// single source of truth is followed again by whoever reads it next. Put the status colour on
// the text it describes, or in a small chip. RailW survives for the cockpit's run rows, where
// it is a gutter marking rows in a dense list rather than a decoration on a block.
//
// IMPORTANT: this is compiled by legacy csc (Framework64 v4.0.30319, C# 5). NO expression-
// bodied members, NO string interpolation, NO null-conditional. Classic method bodies only.
using System.Collections.Generic;
using System.Windows.Media;

static class Theme
{
    // ── primitives ────────────────────────────────────────────────────────────────
    public static Color Col(string hex) { return (Color)ColorConverter.ConvertFromString(hex); }
    public static SolidColorBrush Br(string hex) { return new SolidColorBrush(Col(hex)); }

    // ── color tokens (hex by mode) ─────────────────────────────────────────────────
    // d == true => dark mode. Values mirror the spec Design Tokens tables verbatim.
    public static string Bg(bool d)            { return d ? "#111111" : "#F7F6F2"; }  // app background
    public static string Surface(bool d)       { return d ? "#181818" : "#FFFFFF"; }  // main surface, cards
    public static string SurfaceSubtle(bool d) { return d ? "#202020" : "#F4F4F2"; }  // composer, selected row
    // Selected/active card fill: a quiet step OFF the surrounding surface (no colored border/rail).
    // Light: one step darker than SurfaceSubtle/PanelAlt (#F4F4F2) toward Border (#D8D6CF) in the same
    // warm-neutral family -> #E7E5DE (clearly-but-quietly darker, still lighter than the 1px border so
    // an inner card never out-values its own edge). Dark: one step LIGHTER than the surface (#202020),
    // below BorderStrong (#3A3A3A) so the selected fill stays under the hover/active border value.
    public static string Selected(bool d)      { return d ? "#2C2C2C" : "#E7E5DE"; }  // active/selected row card
    public static string Border(bool d)        { return d ? "#2E2E2E" : "#D8D6CF"; }  // 1px borders
    public static string BorderStrong(bool d)  { return d ? "#3A3A3A" : "#D4D4D0"; }  // active / hover border
    public static string Text(bool d)          { return d ? "#F4F4F5" : "#18181B"; }  // body
    public static string Muted(bool d)         { return d ? "#A1A1AA" : "#71717A"; }  // secondary text
    public static string Faint(bool d)         { return d ? "#71717A" : "#A1A1AA"; }  // meta text
    public static string Accent(bool d)        { return d ? "#F97316" : "#D9480F"; }  // primary action ONLY
    public static string AccentSoft(bool d)    { return d ? "#3A2416" : "#FFF1E8"; }  // primary hover / subtle badge
    public static string AccentFg(bool d)      { return "#FFFFFF"; }                  // text on accent fill
    public static string Success(bool d)       { return d ? "#22C55E" : "#16A34A"; }  // done chip / left rail
    public static string Warning(bool d)       { return d ? "#F59E0B" : "#B45309"; }  // needs-attention
    public static string Danger(bool d)        { return d ? "#EF4444" : "#B91C1C"; }  // error
    public static string Info(bool d)          { return d ? "#60A5FA" : "#2563EB"; }  // running / reviewing
    public static string Secondary(bool d)    { return d ? "#A1A1AA" : "#3F3F46"; }  // secondary text (Ledger: Graphite)

    // Translucent hover/press overlays (white on dark, black on light).
    public static string Hover(bool d) { return d ? "#22FFFFFF" : "#14000000"; }
    public static string Press(bool d) { return d ? "#38FFFFFF" : "#26000000"; }

    // ── typography ────────────────────────────────────────────────────────────────
    public const string UiFont   = "Segoe UI Variable, Segoe UI";
    public const string CodeFont = "Cascadia Mono, Consolas";
    public const double FsTitle   = 16; // app title (semibold)
    public const double FsSection = 13; // section title (semibold)
    public const double FsBody    = 13; // body
    public const double FsMeta    = 12; // meta
    public const double FsChip    = 12; // chip (semibold)
    public const double FsLog     = 12; // monospace log

    // ── spacing / radius ──────────────────────────────────────────────────────────
    public const double PadApp        = 16;
    public const double HeaderH       = 48;
    public const double FleetHeaderH  = 52;
    public const double CtrlH         = 30;
    public const double BtnH          = 32;
    public const double ChipH         = 24;
    public const double ComposerMinH  = 56;
    public const double CardGap       = 8;
    public const double SectionGap    = 16;
    public const double RadSmall    = 6;  // small controls
    public const double RadCard     = 8;  // cards
    public const double RadComposer = 10; // composer
    public const double RadPopover  = 10; // modal / popover
    public const double RailW       = 3;  // card left status rail width

    // ── status model ──────────────────────────────────────────────────────────────
    // Canonical status keys (see DeriveStatus in the cockpit) -> visual treatment.
    // Rail kinds: "neutral" | "info" | "success" | "warning" | "danger".
    // No status fills the whole card; the rail color + chip carry the meaning.
    static readonly Dictionary<string, string> _rail = new Dictionary<string, string>
    {
        { "pending",     "neutral" },
        { "ready",       "info"    },
        { "waiting",     "info"    },
        { "researching", "info"    },
        { "refuting",    "info"    },
        { "verifying",   "info"    },
        { "awaiting",    "warning" },
        { "done",        "success" },
        { "stuck",       "warning" },
        { "maxturns",    "warning" },
        { "error",       "danger"  },
        { "cancelled",   "neutral" },
        { "freed",       "neutral" },
    };

    public static string StatusRail(string canonical)
    {
        string v;
        if (canonical != null && _rail.TryGetValue(canonical, out v)) return v;
        return "neutral";
    }

    // Resolve a rail/chip kind to its accent color for the given mode.
    public static string RailColor(string railKind, bool dark)
    {
        if (railKind == "info") return Info(dark);
        if (railKind == "success") return Success(dark);
        if (railKind == "warning") return Warning(dark);
        if (railKind == "danger") return Danger(dark);
        return Muted(dark); // neutral
    }

    public static string StatusColor(string canonical, bool dark)
    {
        return RailColor(StatusRail(canonical), dark);
    }

    // Display label. lang: 0 = Japanese, 1 = English. Called during render (not cached),
    // so a language toggle that re-renders picks up the new label automatically.
    public static string StatusLabel(string canonical, int lang)
    {
        bool jp = lang == 0;
        switch (canonical)
        {
            case "pending":     return jp ? "待機"           : "Queued";
            case "ready":       return jp ? "開始中"         : "Starting";
            case "waiting":     return jp ? "実行中"         : "Running";
            case "researching": return jp ? "調査中"         : "Researching";
            case "refuting":    return jp ? "レビュー中"     : "Reviewing";
            case "verifying":   return jp ? "検証中"         : "Verifying";
            case "awaiting":    return jp ? "承認待ち"       : "Needs input";
            case "done":        return jp ? "完了"           : "Done";
            case "stuck":       return jp ? "要対応"         : "Needs attention";
            case "maxturns":    return jp ? "要対応"         : "Needs attention";
            case "error":       return jp ? "停止(エラー)"   : "Stopped (error)";
            case "cancelled":   return jp ? "停止"           : "Stopped";
            case "freed":       return jp ? "解放済"         : "Released";
            default:            return canonical == null ? "" : canonical;
        }
    }

    // Color interpolation helper (shared by callers that still want a soft tint somewhere).
    public static Color Mix(Color a, Color b, double t)
    {
        return Color.FromRgb((byte)(a.R * t + b.R * (1 - t)),
                             (byte)(a.G * t + b.G * (1 - t)),
                             (byte)(a.B * t + b.B * (1 - t)));
    }
}
