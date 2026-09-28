# -*- coding: utf-8 -*-
"""Every settings key, and WHEN a change to it actually takes effect.

WHY THIS EXISTS. The settings panel presents a dozen controls as if they were one kind of
thing. They are not. Moving the disk floor changes a fleet that is already running, within a
second. Moving the retry cap does nothing at all to that fleet -- it is read once, before the
sweep loop starts, and the run keeps the number it was born with. Both controls sit in the
same column, look identical, and give no hint which one they are. An operator who changes a
knob and watches for the run to respond is therefore right half the time, and when they are
wrong the only evidence is that nothing happens, which is indistinguishable from a bug.

That ambiguity is not a documentation gap. It is what let the 2026-09-16 defect live for a
month: the panel said 1 GB, the fleet reserved 4 GB, and "settings sometimes need a restart"
was available as an explanation for why the two disagreed. When every key states its own
timing, "I changed it and nothing happened" is either expected or a failing test.

WHAT A DECLARATION MEANS.

  live          adopted into a fleet that is ALREADY RUNNING, within about a second. The
                evidence must be an actual re-read inside a loop or a watcher -- a value read
                once and passed around is not live, however often the thing holding it runs.
  each_gate     re-read at every admission or approval decision. Work already in flight keeps
                going; the next decision uses the new value. Distinguished from live because
                the two make different promises and only one of them changes running work.
  sweep_start   read once when a coordinator starts. The next run gets it.
  bridge_start  read once when the bridge process starts, so it must be restarted. Named for
                the boundary rather than for who can cross it: starting another job is easy,
                restarting the bridge is a different act.
  ui_only       never read outside the GUI process. It persists a window's own state.

THE DEFAULTS ARE PART OF THE DECLARATION, because a default is a fact with as many owners as
there are readers, and those owners drift. `ram_floor_mb` had THREE: the panel showed 2048,
the coordinator fell back to an unrelated flag's default of 1400, and the three admission
gates shared 512. Nothing was wrong with any single number; they had simply never been
required to agree. tools/test_a_setting_declares_when_it_takes_effect.py requires it.

DELIBERATELY NOT A MECHANISM. This module decides nothing at runtime -- it declares, and the
test compares the declaration with the implementation. Making it authoritative (having the
follower build its watch list from here, say) would make the two agree by construction, which
is exactly the property that stops a disagreement from being detectable.
"""
from __future__ import annotations

from collections import OrderedDict

#: THE BOUNDARY A CHANGE CROSSES, not a rough class of it. The first version of this table
#: used "live" for both the follower and the per-decision reads, which are different promises:
#: one changes work already in flight, the other only the next decision. gpt-6-astra named
#: that, and "per_job"/"at_launch", which said who could reach the setting rather than where
#: it is read.
LIVE = "live"                  # adopted into a fleet that is already running (~1 s)
EACH_GATE = "each_gate"        # re-read at every admission or approval decision
SWEEP_START = "sweep_start"    # read once when a coordinator starts; the next run gets it
BRIDGE_START = "bridge_start"  # read once when the bridge starts; it must be restarted
UI_ONLY = "ui_only"            # never read outside the GUI

EFFECTS = (LIVE, EACH_GATE, SWEEP_START, BRIDGE_START, UI_ONLY)

#: Sentinel for "absent means undecided", which is not the same as "absent means this value".
#: `fanout` is the only key with it: unset does not mean off, it means the length heuristic in
#: relay/task_router.py decides, and writing a boolean here would state something false.
UNDECIDED = object()


class Key(object):
    """One settings key: what changes it, when the change lands, and what absent means."""

    __slots__ = ("name", "effect", "default", "reader", "note")

    def __init__(self, name, effect, default, reader, note):
        assert effect in EFFECTS, (name, effect)
        self.name = name
        self.effect = effect
        self.default = default
        self.reader = reader          # where the deciding read happens, file:function
        self.note = note              # what an operator needs to know, in one sentence

    def __repr__(self):
        return "Key(%r, %s)" % (self.name, self.effect)


def _k(name, effect, default, reader, note):
    return (name, Key(name, effect, default, reader, note))


KEYS = OrderedDict([

    # ---------------------------------------------------------------- live: the follower
    # relay/settings_follow.py re-reads the file every sweep (~1 s) and adopts a value whose
    # FILE contents changed. Following changes rather than values is what lets a live
    # override -- 強制開始 dropping the disk floor to 0 -- survive until the operator next
    # touches that knob.
    _k("disk_floor_gb", LIVE, 6.0,
       "relay/fleet_runner.py:settings_disk_floor + Follower.watch",
       "A running fleet adopts a new floor within a second."),

    _k("ram_floor_mb", LIVE, 512.0,
       "relay/fleet_runner.py:settings_ram_floor + Follower.watch",
       "A running fleet adopts a new floor within a second."),

    _k("maxtabs", LIVE, 3,
       "relay/fleet_runner.py:settings_maxtabs + Follower.watch",
       "Live, but it means different things: with autoscale ON this moves the CEILING, "
       "with autoscale off it is the fixed cap."),

    # ---------------------------------------------------------------- each_gate
    _k("rate_ceiling_rpm", EACH_GATE, 100.0,
       "relay/relay_fleet.py:rate_ceiling (every admission check)",
       "Read fresh on every admission decision."),

    _k("job_approval_mode", EACH_GATE, "auto",
       "tools/approval_policy.py:current_approval_mode (every gate check)",
       "Read fresh at every approval gate, so the panel governs jobs already queued."),

    # ---------------------------------------------------------------- sweep_start
    _k("autoscale", SWEEP_START, 0,
       "relay/fleet_runner.py:settings_autoscale (once, before the sweep)",
       "Takes effect on the NEXT run. Toggling it mid-run does nothing, although it sits "
       "beside knobs that are live."),

    _k("autoscale_max", SWEEP_START, 100,
       "relay/fleet_runner.py:settings_autoscale (once, before the sweep)",
       "Takes effect on the NEXT run -- while maxtabs, next to it, moves the same ceiling "
       "live when autoscale is on."),

    _k("autoscale_per_tab_mb", SWEEP_START, 700.0,
       "relay/fleet_runner.py:settings_per_tab (once, before the sweep)",
       "Written by bench/ram_calib.py's calibration, not by any panel control."),

    _k("effort", SWEEP_START, "auto",
       "relay/fleet_runner.py:settings_effort (once, in main)",
       "Takes effect on the NEXT run."),

    _k("autoretry", SWEEP_START, 1,
       "relay/fleet_runner.py:settings_autoretry (once, before the sweep)",
       "Takes effect on the NEXT run."),

    _k("autoretry_max", SWEEP_START, 2,
       "relay/fleet_runner.py:settings_autoretry (once, before the sweep)",
       "Takes effect on the NEXT run. Clamped to 0..3 here and to 1..3 in the panel: "
       "0 means off and only the file can say it, because the panel expresses off "
       "with the autoretry toggle instead."),

    _k("fanout", SWEEP_START, UNDECIDED,
       "relay/task_router.py:_wants_fanout (once per autostart goal)",
       "Governs goals that arrive through autostart only; a run launched from the cockpit "
       "carries the checkbox as a flag instead. Unset means UNDECIDED -- a length heuristic "
       "decides, it does not mean off."),

    _k("fleet_log_days", SWEEP_START, 14.0,
       "relay/fleet_retention.py:apply (once, at coordinator start)",
       "Applied when the next coordinator starts."),

    _k("fleet_store_days", SWEEP_START, 30.0,
       "relay/fleet_retention.py:apply (once, at coordinator start)",
       "Applied when the next coordinator starts."),

    # THESE TWO HAD NO WRITER until 2026-09-17. fleet_retention asked for both on every run
    # and no control set either, so the only way to choose was to edit settings.txt by hand --
    # the one thing an operator is told not to do. A setting reachable only by breaking the
    # rule about settings is a setting nobody has.
    _k("fleet_scratch_days", SWEEP_START, 14.0,
       "relay/fleet_retention.py:apply (once, at coordinator start)",
       "Applied when the next coordinator starts."),

    _k("fleet_compress_hours", SWEEP_START, 6.0,
       "relay/fleet_retention.py:apply (once, at coordinator start)",
       "Applied when the next coordinator starts. Floored at 1 hour in the panel: compressing "
       "a run's files the moment it ends fights whatever is still reading them."),

    # ---------------------------------------------------------------- bridge_start
    # The bridge applies retention once, at startup, deliberately: a timer that deletes
    # conversations while the operator is reading them is worse than a stale setting.
    _k("session_retention_days", BRIDGE_START, 90.0,
       "bridge/session_store.py:read_retention via apply_retention",
       "Applied once when the bridge starts; changing it needs the bridge restarted. "
       "Owner decision 2026-09-24: unset now means 90 days, not forever. An explicit 0 is "
       "still the operator choosing to keep everything -- only absent gets the default."),

    _k("session_max_mb", BRIDGE_START, None,
       "bridge/session_store.py:read_retention via apply_retention",
       "Applied once when the bridge starts. None/0 means no cap."),

    # ---------------------------------------------------------------- ui_only
    _k("approval", UI_ONLY, "run", "ui/FleetCockpit.cs",
       "Restores the plan/auto/run selector. The choice reaches a run as a command-line "
       "flag; no Python reads this key."),
    _k("runtime", UI_ONLY, "fleet", "ui/FleetCockpit.cs",
       "Chooses the next cockpit launch path: fleet keeps the conversation-driven runner; "
       "durable starts the SQLite-backed LOCAL_LOOP runtime. Existing work is unaffected."),
    _k("dark", UI_ONLY, 1, "ui/FleetCockpit.cs, ui/CopilotChat.cs", "Theme."),
    _k("lang", UI_ONLY, 0, "ui/FleetCockpit.cs, ui/CopilotChat.cs", "Interface language."),
    _k("ui_scale", UI_ONLY, "auto", "ui/FleetCockpit.cs, ui/CopilotChat.cs", "Zoom."),
    _k("ui_scale_target", UI_ONLY, "auto", "ui/FleetCockpit.cs, ui/CopilotChat.cs", "Zoom."),
    _k("sidebar_collapsed", UI_ONLY, 0, "ui/CopilotChat.cs", "Sidebar state."),
    _k("deletemode", UI_ONLY, 1, "ui/CopilotChat.cs", "Delete-button behaviour."),
    _k("last_open_conv", UI_ONLY, "", "ui/CopilotChat.cs", "Which conversation to reopen."),
    _k("autoarchive", UI_ONLY, 0, "ui/FleetCockpit.cs", "Archive a run when it finishes."),
])


def effect(name):
    """When a change to `name` takes effect, or None when the key is not declared."""
    k = KEYS.get(name)
    return k.effect if k else None


def default(name):
    """What an absent `name` means. Raises for an undeclared key rather than guessing."""
    return KEYS[name].default


def names_with_effect(which):
    return [k.name for k in KEYS.values() if k.effect == which]


def describe(name):
    """One line for an operator: the timing and what it implies."""
    k = KEYS.get(name)
    if k is None:
        return "%s: not declared" % name
    return "%s [%s] %s" % (k.name, k.effect, k.note)
