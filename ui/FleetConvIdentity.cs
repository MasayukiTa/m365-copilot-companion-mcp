// FleetConvIdentity.cs -- the identity-merge DECISION for a fleet conversation's Goal /
// Transcript / Source / Name fields, extracted out of ui/CopilotChat.cs (commit 3c93b59) so it
// can be run by a test instead of only read as text.
//
// WHY THIS IS ITS OWN FILE. Before 3c93b59, OpenFromFleet() and SyncRegistry() (both in
// ui/CopilotChat.cs, a WPF class that cannot be constructed without a dispatcher, a bridge and
// a desktop) resolved a fleet conversation's identity fields and then either copied them onto
// the Conversation object or silently didn't. The only thing ever checking that logic was
// ui/test_a_fleet_conversation_opened_from_the_cockpit_keeps_its_identity.py, a source-text
// test -- it can see the string "c.Goal = bestGoal" exists somewhere in the method, but it
// cannot see whether the guard around it is correct, or run the never-overwrite rule against
// real values. Pulling the decision out into a WPF-free static class lets a test compile just
// THIS file (see ui/harness/FleetConvIdentityHarness.cs) and execute it.
//
// KEPT FREE OF THE Conversation TYPE ON PURPOSE. Conversation lives in ui/ChatSend.cs, which is
// compiled into CopilotChat.exe only (ui/rebuild_ui.ps1's CopilotChat Build line) -- FleetCockpit
// has no such type. This file's own Build-line entry covers BOTH binaries (a WPF-free decision
// belongs in both, not just the one that happens to use it today), so it operates on plain
// strings and lets each caller apply the result to whatever field it owns, instead of taking a
// Conversation parameter that would only resolve in one of the two builds.
//
// TWO MERGE RULES, taken from the two call sites verbatim:
//
//   MergeForward     -- OpenFromFleet: the freshly-resolved value (a live status.json worker
//                        dict read, or a transcript re-read) wins whenever it is non-empty,
//                        because OpenFromFleet's own reads are always at least as current as
//                        whatever the row already carried from an earlier poll. Falls back to
//                        the existing value only when the fresh read came up empty (a transient
//                        miss -- no live worker, no meta line, e.g. a slow disk read -- must not
//                        blank out an identity the row already had).
//
//   MergeBackfillOnly -- SyncRegistry: the registry poll runs continuously while the window
//                        stays open, so a row it already added must be able to pick up a value
//                        that only became available later -- but must NEVER overwrite a value
//                        the row already has, full stop. Also used by OpenFromFleet itself for
//                        Source ("claim fleet only for an unclaimed row") and Name (worker name
//                        is a fallback identifier, never a correction).
//
// Legacy csc (Framework64 v4.0.30319, C# 5): NO string interpolation, NO null-conditional, NO
// expression-bodied members, NO nameof. No WPF types in this file -- that is what lets a test
// compile it standalone.
using System;

static class FleetConvIdentity
{
    /// The fresh value wins when present; otherwise keep whatever the row already had. Used by
    /// OpenFromFleet for Transcript and Goal: its own reads (the live worker dict / a transcript
    /// re-read) are always at least as current as an earlier poll, so they are allowed to move
    /// the row forward, but an empty read must never blank out a value the row already carried.
    public static string MergeForward(string existing, string freshValue)
    {
        if (!string.IsNullOrEmpty(freshValue)) return freshValue;
        return existing ?? "";
    }

    /// Fill in a value only when the row does not already have one; never replace a non-empty
    /// value. Used by SyncRegistry for its Goal/Transcript backfill (the ONLY feed that updates
    /// a fleet conversation already in the sidebar while the window stays open -- an in-flight
    /// worker's registry read must not stomp a correct value with a stale or empty one), and by
    /// OpenFromFleet for Source (claim "fleet" only for an unclaimed row -- a row already
    /// carrying a real, different source, e.g. a plain Copilot-side orphan sharing this exact
    /// ConvUrl, must not be silently reclassified) and Name (a worker name is a fallback
    /// identifier, filled in once and never overwritten).
    public static string MergeBackfillOnly(string existing, string freshValue)
    {
        if (string.IsNullOrEmpty(existing) && !string.IsNullOrEmpty(freshValue)) return freshValue;
        return existing ?? "";
    }

    /// OpenFromFleet's goal resolution: the live status.json worker dict's own "goal" field
    /// wins when non-empty; otherwise the transcript's first-line meta goal (already looked up
    /// by the caller via TranscriptMetaGoal -- this method takes the result, not the path, so it
    /// stays free of file I/O). Equivalent to MergeForward(transcriptMetaGoal, liveGoal), named
    /// separately because "which source wins" reads more plainly than a forward-merge here.
    public static string ResolveGoal(string liveGoal, string transcriptMetaGoal)
    {
        if (!string.IsNullOrEmpty(liveGoal)) return liveGoal;
        return transcriptMetaGoal ?? "";
    }
}
