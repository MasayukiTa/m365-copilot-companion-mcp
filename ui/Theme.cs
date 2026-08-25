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
// the text it describes, or in a small chip.
//
// This paragraph once ended with a carve-out: the cockpit's run rows could keep their rail,
// "a gutter marking rows in a dense list rather than a decoration on a block". That sentence
// was mine, not the operator's, and it had exactly the shape of the stale recommendation it
// was written to replace -- normative text in the single source of truth that the next reader
// takes for settled policy. The rail is gone and so is the width token; there is no exception.
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
    // LIGHT DARKENED 71717A -> 5F5F66. On the app background the old value scored 4.47 and on a
    // selected row 4.39 -- under the 4.5 that normal-size text needs, at 26 call sites.
    public static string Muted(bool d)         { return d ? "#A1A1AA" : "#5F5F66"; }  // secondary text
    // LIGHT DARKENED A1A1AA -> 6B6B73. The old value scored 2.33-2.56 -- not merely under the
    // 4.5 for normal text but under the 3.0 floor for large text, on every surface, at 47 call
    // sites. This is the timestamp and elapsed-time row: the text the operator reads to find out
    // whether the thing in front of them is still alive.
    public static string Faint(bool d)         { return d ? "#71717A" : "#6B6B73"; }  // meta text
    // LIGHT DARKENED D9480F -> C4400D. White on the old fill scored 4.30, so the label on the
    // one button that matters most was under the line; as text on the background it scored 3.98.
    // Both clear 4.5 now (5.14 and 4.66) and the hue is unchanged to the eye.
    public static string Accent(bool d)        { return d ? "#F97316" : "#C4400D"; }  // primary action ONLY
    public static string AccentSoft(bool d)    { return d ? "#3A2416" : "#FFF1E8"; }  // primary hover / subtle badge
    // WHITE, IN BOTH THEMES -- the operator looked at near-black on orange and judged it harder to
    // read, and a contrast ratio is a floor for legibility, not a ranking of it. The number still
    // has to be met, so the FILL moved instead: see AccentFill / SuccessFill below.
    public static string AccentFg(bool d)      { return "#FFFFFF"; }                  // text on a saturated fill
    // LIGHT DARKENED 16A34A -> 15803D. On a selected row the old green scored 2.99 -- the "done"
    // chip, the most-read state in the list, below even the large-text floor.
    public static string Success(bool d)       { return d ? "#22C55E" : "#15803D"; }  // done chip
    public static string Warning(bool d)       { return d ? "#F59E0B" : "#B45309"; }  // needs-attention
    public static string Danger(bool d)        { return d ? "#EF4444" : "#B91C1C"; }  // error
    public static string Info(bool d)          { return d ? "#60A5FA" : "#2563EB"; }  // running / reviewing

    // ── fills that carry white text ──────────────────────────────────────────────
    // A FILL IS NOT A MARK, AND THE DARK THEME IS WHERE THAT STOPS BEING PEDANTRY. Accent and
    // Success above are drawn ON the dark ground, so they are bright: 6.74 and 8.29 there, which
    // is what a status mark needs. Fill a button with those same brights and put a white label on
    // it and you get 2.80 and 2.28 -- the two worst pairs in this palette, on the largest controls
    // on the screen. Both roles were sharing one number, and only one of them could win.
    //
    // Light needs no split: #C4400D and #15803D already carry white (5.14, 5.02) and already read
    // as text on a pale ground (4.66, 4.83). Only the dark theme's brights had to darken, and only
    // where they are a fill -- the tab underline, the click flash and the composer's focus ring
    // keep the bright Accent, because nothing sits on top of them.
    public static string AccentFill(bool d)    { return d ? "#C2410C" : "#C4400D"; }  // white label: 5.18 / 5.14
    public static string SuccessFill(bool d)   { return d ? "#15803D" : "#15803D"; }  // white label: 5.02
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
    // THE VALUES THAT ARE ACTUALLY DRAWN. 62 of the 74 corner radii in this UI were literals --
    // 4, 8, 10, 6, and then 12, 9, 7, 5, 14, 1 -- while these tokens sat here being read as a
    // system by anyone who opened this file. A design system nothing obeys is decoration, and it
    // is the same failure as a comment nobody follows: it describes an intention, not the product.
    // RadChip and RadBubble were added because those sizes were in real use and had no name; the
    // strays snapped to the nearest token (9->8, 7->6, 14->12), a change of at most two pixels.
    // Radii that are geometry rather than style -- half of a dot's width, half of an underline's
    // height -- stay computed at their call site and are deliberately not tokens.
    // ── spacing ───────────────────────────────────────────────────────────────────
    // A SCALE, MEASURED INTO EXISTENCE RATHER THAN ASSUMED. Before this there were 2,266 spacing
    // numbers across 28 distinct values, with essentially every integer from 1 to 16 in use --
    // the same shape as the radii, thirty times bigger. Structural space now steps in fours.
    //
    // Below the scale sits a documented exception: 1, 2, 3, 5, 6 and 7 remain in wide use and are
    // NOT errors. That range is where you stop laying out and start adjusting for the eye -- the
    // pixel that centres an icon against a cap height, the two that keep a dense row off its
    // divider -- and rounding those to four would damage the rows rather than tidy them.
    //
    // ui/test_spacing_scale.py holds the set of values actually in use and fails when a NEW one
    // appears. It can shrink, never grow: the problem was not any single number, it was that
    // nothing stopped the next one.
    public const double Sp1 = 4;
    public const double Sp2 = 8;
    public const double Sp3 = 12;
    public const double Sp4 = 16;
    public const double Sp5 = 24;
    public const double Sp6 = 32;

    public const double RadChip     = 4;  // chips, pills, tags
    public const double RadSmall    = 6;  // small controls
    public const double RadCard     = 8;  // cards
    public const double RadComposer = 10; // composer
    public const double RadPopover  = 10; // modal / popover
    public const double RadBubble   = 12; // chat bubbles, large panels

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
