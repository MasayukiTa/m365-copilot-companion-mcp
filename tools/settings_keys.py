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

import math
from collections import OrderedDict

# ---- HISTORICAL RATE-BASED ESTIMATE (the operational default is measured below) -----------
# How many goals may run at once is bounded by the cloud rate, not by RAM or tabs: a goal sends
# about one request per turn, so by Little's law the goals the rate can feed at once are
#     concurrent goals ~= rate (requests/min) x seconds per turn / 60.
# Past that point extra goals only queue READY behind the send token bucket. The ceiling is
# that figure with headroom, so it is high enough never to be the binding limit and low enough
# to mean something. Replaces the 2026-10-14 emergency literal of 1000.
CLOUD_RPM = 100.0            # the published per-environment limit (relay/quota_meter.LIMIT_RPM)
TYPICAL_TURN_S = 60.0        # measured: median 37 s (other runs) to 57 s (the 189-goal run)
LANE_HEADROOM = 2.0          # goals beyond what the rate can feed, so turn-time swings never starve it


def derive_lane_ceiling(rpm=CLOUD_RPM, turn_s=TYPICAL_TURN_S, headroom=LANE_HEADROOM) -> int:
    """Lane ceiling from the cloud rate: ceil(rpm * turn_s / 60 * headroom). 200 by default."""
    return int(math.ceil(float(rpm) * float(turn_s) / 60.0 * float(headroom)))


# 2026-10-27: 120-200 concurrent yielded 5 DONE and 178 STUCK; reserve 10 priority slots.
LANE_CEILING_DEFAULT = min(50, derive_lane_ceiling())

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

    _k("maxtabs", LIVE, LANE_CEILING_DEFAULT,
       "relay/fleet_runner.py:settings_maxtabs + Follower.watch",
       "Live, but it means different things: with autoscale ON this moves the CEILING, "
       "with autoscale off it is the fixed cap."),

    _k("priority_extra_slots", EACH_GATE, 10,
       "relay/relay_fleet.py:priority_extra_slots (each admission sweep)",
       "Extra priority slots above the normal cap. On 2026-10-27, 120-200 concurrent "
       "yielded 5 DONE and 178 STUCK; the defaults are 50 plus 10 priority slots."),

    # ---------------------------------------------------------------- each_gate
    _k("rate_ceiling_rpm", EACH_GATE, 100.0,
       "relay/relay_fleet.py:rate_ceiling (every admission check)",
       "Read fresh on every admission decision."),

    _k("socket_goal_mb", EACH_GATE, 25.0,
       "relay/socket_capacity.py:goal_cost_mb (every admission sweep)",
       "Price in MB of one held socket conversation, used for the RAM safeguard on admitting "
       "new goals: cap = active + (free RAM - ram_floor_mb) / this. Measured cost is a few MB "
       "(1.9 MB per conversation), so 25 is conservative. Running goals are never stopped."),

    _k("job_approval_mode", EACH_GATE, "auto",
       "tools/approval_policy.py:current_approval_mode (every gate check)",
       "Read fresh at every approval gate, so the panel governs jobs already queued."),

    # fleet_submit's word-overlap duplicate guard (tools/fleet_intake.py). OFF by default by
    # owner order 2026-10-26: refusing a goal at the door on word overlap turned away DIFFERENT
    # work (two short goals sharing a verb, two coding goals sharing one appended block of common
    # rules), so near-duplicates are now NORMALISED IN THE BACKGROUND -- a possible_duplicate_of
    # tag on the job and one line in .fleet/dup_notes.jsonl -- instead of being refused. This
    # switch only restores the OLD refusal for an operator who wants it.
    _k("fleet_submit_dup_guard", EACH_GATE, "off",
       "tools/fleet_intake.py:_dup_guard_on (every fleet_submit call)",
       "off|on. off (default, owner order 2026-10-26): a goal that resembles one already queued or "
       "running is ACCEPTED and recorded (possible_duplicate_of on the job, one line in "
       ".fleet/dup_notes.jsonl), never refused. on: restores the old behaviour -- such a goal is "
       "refused at the door using PR #293's improved similarity (the appended common-rules tail is "
       "ignored, short goals weigh their own identifiers). Read fresh on every submission, so a "
       "change takes effect on the next fleet_submit with no restart. Absent, unreadable or "
       "anything but on reads as off."),

    # ---------------------------------------------------------------- sweep_start
    _k("autoscale", SWEEP_START, 0,
       "relay/fleet_runner.py:settings_autoscale (once, before the sweep)",
       "Takes effect on the NEXT run. Toggling it mid-run does nothing, although it sits "
       "beside knobs that are live."),

    _k("autoscale_max", SWEEP_START, LANE_CEILING_DEFAULT,
       "relay/fleet_runner.py:settings_autoscale (once, before the sweep)",
       "Takes effect on the NEXT run -- while maxtabs, next to it, moves the same ceiling "
       "live when autoscale is on."),

    _k("autoscale_per_tab_mb", SWEEP_START, 700.0,
       "relay/fleet_runner.py:settings_per_tab (once, before the sweep)",
       "Written by bench/ram_calib.py's calibration, not by any panel control."),

    _k("effort", SWEEP_START, "auto",
       "relay/fleet_runner.py:settings_effort (once, in main)",
       "Takes effect on the NEXT run."),

    _k("effort_policy", EACH_GATE, "off",
       "relay/effort_policy.py:mode_info (every mode() call, mtime-cached)",
       "off|shadow|on. Re-read at each policy decision (next worker, fan-out or turn "
       "evaluation) with no restart; decisions already made stand. An MCP_EFFORT_POLICY "
       "environment variable beats this setting and is reported as a conflict."),

    _k("tool_transport", EACH_GATE, "connector-less",
       "relay/tool_bridge.py:transport (every driver_for, mtime-cached)",
       "connector|connector-less. connector: the agent holds the tools itself through a custom "
       "connector (the existing path, needs a Dev Tunnel). connector-less: the agent has no "
       "connector and no tunnel is needed; the model asks for a call in its reply and this "
       "process runs it against the local server (docs/connector_less_route.md). Read when a "
       "conversation is created, so a change reaches the NEXT conversation with no restart and "
       "conversations already running keep the transport they started with. With no usable "
       "line the value is connector-less, except on a machine whose .env already has "
       "MCP_TUNNEL_URL or MCP_TUNNEL_NAME (set up for the connector route before the default "
       "changed): that one stays on connector. An unknown value reads as if the line were "
       "absent. No environment variable overrides it."),

    # Work IQ consent cards on the connector-less route (relay/consent_approve.py). ON by default
    # because the owner's standing requirement is that nobody clicks these cards by hand.
    _k("consent_auto_approve", EACH_GATE, "on",
       "relay/consent_approve.py:mode (every connector-less turn that ends without text, "
       "mtime-cached)",
       "on|off. on: when a connector-less turn ends on a tool-approval card, a helper process "
       "opens that conversation in a light page of the product's own Edge (no Copilot turn is "
       "sent) and presses the card ONLY when its tool name is one of the 7 Work IQ tools "
       "(Calendar, Copilot, Mail, OneDrive, SharePoint, Teams, User) and the page is the "
       "configured agent's conversation; any other card is left alone and recorded in "
       ".fleet/consent_approvals.jsonl. Read when a turn ends on a card, so a change reaches the "
       "next card with no restart. The connector route never reads it. Anything but off reads "
       "as on."),

    # Resident tools for a person's own chat (relay/resident_guard.py). Nothing acts on it yet: the
    # first slice adds only the refusal rules and an observation probe, and status.json reports it.
    _k("resident_tools", EACH_GATE, "off",
       "relay/resident_guard.py:mode (reported in status.json every snapshot, mtime-cached; "
       "no component acts on it yet)",
       "off|on. Reserved for the resident component that would run tool blocks found in the "
       "configured user's own conversations with the configured agent (Copilot app, Teams, "
       "mobile) on this PC. In this version it changes nothing: the value is only reported in "
       "status.json. Read at each status snapshot, so a change shows with no restart. Anything "
       "but on reads as off."),

    # Continue turn after an unknown delivery (relay/continue_recovery.py). OFF by default.
    _k("socket_continue_recovery", EACH_GATE, "on",
       "relay/continue_recovery.py:mode (every send on the socket route and every retry "
       "decision, mtime-cached)",
       "on|off. on (default): for connector-less (bridged) repository work, a socket goal whose "
       "turn was cut after unknown delivery opens a continue turn in the SAME conversation "
       "instead of ending STUCK -- it first checks that the conversation still holds its "
       "history, re-quotes the results of calls this process already ran, and never runs the "
       "same call twice. off: such a goal ends STUCK, exactly as before. Read at each send and "
       "retry decision, so a change reaches the next turn with no restart; only conversations "
       "created while it is on carry the refs the check needs. A present line reads as on only when it "
       "says on; anything else on a present line is off, while an absent line is on (the default)."),

    # Sibling write scope (relay/write_scope.py). SHADOW ONLY: it records overlapping writes
    # among siblings of one campaign and changes nothing. There is deliberately no `on` value;
    # enforcing a write scope would go through the folder policy (frozen) and needs a separate
    # approval before such a value may exist.
    _k("fanout_write_scope", EACH_GATE, "off",
       "relay/write_scope.py:mode (every coordinator sweep, mtime-cached)",
       "off|shadow. Re-read at every sweep with no restart. shadow records a scope_overlap "
       "row when two siblings write one path or one writes a path another's step names; "
       "nothing is blocked and no prompt or output changes."),

    # Checkout-root placement guard (tools/workarea_guard.py). Refuses (or in the default
    # warn mode, only records) a write that would create a NEW untracked top-level name
    # directly under the production checkout. Ships as warn so the refusal ledger
    # (.fleet/workarea_refusals.jsonl) measures the real traffic before the switch is flipped
    # to enforce -- the same measure-before-you-close posture as the command judge.
    _k("workarea_guard", EACH_GATE, "warn",
       "tools/workarea_guard.py:mode (every write through file_ops._validate_path, reads settings.txt mtime-cached)",
       "off|warn|enforce. Re-read at every write with no restart. warn records each violation "
       "to .fleet/workarea_refusals.jsonl and lets the write through; enforce refuses it with a "
       "message naming the allowed places; off disables the guard. Reads and writes outside the "
       "checkout are never affected. MCP_WORKAREA_GUARD overrides this and is reported as a "
       "conflict. Flip to enforce on the strength of the warn-mode ledger, not before."),

    # Extra top-level names the guard treats as allowed, beyond git-tracked entries and the
    # product's own runtime dirs. One comma- or path-separator-separated list. Read with the
    # guard's config on the same mtime cache.
    _k("workarea_allow", EACH_GATE, "",
       "tools/workarea_guard.py:_allow_extra (every write, reads settings.txt mtime-cached)",
       "Extra top-level names allowed at the checkout root, on top of the git-tracked entries "
       "and the product-managed dirs the guard already permits. Empty by default: the tracked "
       "list plus the product dirs are the whole allow-set. Changing it reaches the next write "
       "with no restart."),

    # Load-aware routing (relay/load_routing.py). SHADOW ONLY: at each admission it records
    # which queued goal a router using the remaining admission margins and the role profile
    # would have admitted, beside the one that was. There is deliberately no value that routes.
    _k("fanout_load_routing", EACH_GATE, "off",
       "relay/load_routing.py:mode (every admission, mtime-cached)",
       "off|shadow. Re-read at every admission with no restart. shadow appends one row per "
       "admission to .fleet/load_routing_shadow.jsonl (the goal admitted, the goal the router "
       "would have admitted, the margins); nothing is reordered and no prompt or output "
       "changes."),

    # Read-only tool result cache (relay/result_cache.py). SHADOW ONLY: at each read-only tool
    # call it records whether a cache would have had the same answer to serve and how long that
    # would have saved, beside the real call. There is deliberately no value that serves.
    _k("result_cache", EACH_GATE, "off",
       "relay/result_cache.py:mode (every tool call, mtime-cached)",
       "off|shadow. Re-read at every tool call with no restart. shadow appends one row per "
       "read-only call to .fleet/result_cache_shadow.jsonl (the tool, an argument hash, whether "
       "a cache would have hit and matched, the time saved); nothing is served from a cache and "
       "no prompt or output changes."),

    # Dynamic team formation (relay/team_formation.py). SHADOW ONLY: at each split it records the
    # team (roles and member counts) a team-forming rule would have proposed beside the static
    # one that was built. There is deliberately no value that forms a team.
    _k("team_mode", EACH_GATE, "off",
       "relay/team_formation.py:mode (every split decision, mtime-cached)",
       "off|shadow. Re-read at every split with no restart. shadow appends one row per split "
       "to .fleet/team_formation_shadow.jsonl (roles and counts, no goal text); the children, "
       "prompts, queue and merge are unchanged."),

    # Relay findings between fan-out stages (relay/relay_findings.py). SHADOW ONLY: when a
    # single-stage child finishes it records the findings a SECOND stage could have been handed
    # (discovered paths, confirmed facts, unresolved gaps), beside nothing being changed. There
    # is deliberately no value that builds a second stage; `on` is reserved and behaves as off.
    _k("relay_findings", EACH_GATE, "off",
       "relay/relay_findings.py:mode (every child settle, mtime-cached)",
       "off|shadow|on. Re-read at every child settle with no restart. shadow appends one row per "
       "finished child to .fleet/relay_findings_shadow.jsonl (the findings carried forward and "
       "the hand-off text a second stage would have gotten -- the child's own lines, never the "
       "goal text); nothing is queued and no prompt or output changes. on is reserved (no "
       "second-stage builder yet) and behaves as off."),

    # Campaign board (relay/agent_board.py). The coordinator reads it once when it starts.
    # p1 (workers write) is declared and NOT implemented: it behaves as p0.
    _k("agent_board_mode", SWEEP_START, "off",
       "relay/agent_board.py:begin_run (once, at coordinator start)",
       "off|p0|p1. Takes effect on the NEXT run. p0 appends one pointer row per finished "
       "child to .fleet/board.jsonl and adds two pointer lines to the merge prompt; off "
       "changes no prompt or output. p1 is not implemented and behaves as p0."),

    # Role and protocol version record (relay/org/versions.py). Read once when a run starts.
    _k("org_version_record", SWEEP_START, "off",
       "relay/org/versions.py:record_run (once, at run start)",
       "off|on. Takes effect on the NEXT run. on appends two small lines per run to "
       "org_versions.jsonl beside the transcripts: a content hash of each role and protocol "
       "definition, and the run id. Never goal text. off writes nothing. No prompt or "
       "admission changes either way."),

    # Genotype id on that record (relay/org/versions.py). Read once when a run starts, beside
    # org_version_record. Independent switch: it only labels the lines that record already writes.
    _k("org_genotype_record", SWEEP_START, "off",
       "relay/org/versions.py:record_run (once, at run start)",
       "off|on. Takes effect on the NEXT run, and only when org_version_record is on (it adds a "
       "field to the lines that record writes). on tags each defs and run line in "
       "org_versions.jsonl with genotype_id: one 16-hex name for the pair of role and protocol "
       "hashes that line already carries, so runs of one version share a label. An unreadable "
       "version gets an empty id, never a real one. No new line, no goal text, off adds nothing. "
       "No prompt or admission changes either way."),

    # Shadow evaluation of those proposals (relay/org/shadow_eval.py). Read once when a run starts.
    _k("org_shadow_eval", SWEEP_START, "off",
       "relay/org/shadow_eval.py:record_run (once, at run start)",
       "off|shadow. Takes effect on the NEXT run. shadow appends one small line per run and "
       "per proposal still on file to .fleet/org_shadow_eval.jsonl: the proposal id and hashes "
       "only, never goal text or the proposed text. It counts the runs a proposal would have "
       "touched; it cannot show a proposal is better. off writes nothing. `on` is not "
       "implemented. No prompt or admission changes either way."),

    # Record-only A/B arm assignment (relay/org/ab.py). Read fresh on each run-start call, so a
    # change is seen by the next run with no restart.
    _k("org_ab", EACH_GATE, "off",
       "relay/org/ab.py:record_run (each run start, re-reads the file)",
       "off|shadow. Takes effect on the NEXT run. shadow appends one small line per run to "
       ".fleet/org_ab_assign.jsonl: the run id and which arm (baseline|proposed) the run would "
       "land on if an A/B were running, chosen deterministically from a hash of the run id. "
       "Never goal text or any prompt text. It only records the arm; it swaps no prompt and "
       "changes no admission. off writes nothing. No prompt or admission changes either way."),

    # Change proposals for role and protocol definitions (relay/org/proposals.py). Read on each
    # call of the propose_org_change tool, so a change applies to the next proposal.
    _k("org_proposals", EACH_GATE, "off",
       "relay/org/proposals.py:submit (each propose_org_change call)",
       "off|on. Applies to the NEXT propose_org_change call. on lets that tool file a "
       "proposal: one line in .fleet/org_proposals.jsonl and a decision card; off makes the tool "
       "refuse and writes nothing. A proposal applies nothing and is never read by a prompt, "
       "role or admission. No prompt or admission changes either way."),

    # Per-tree fan-out budget (relay/fanout_budget.py). Read at each split decision; a tree
    # already running keeps going, and the next split uses the new values.
    _k("fanout_max_total", EACH_GATE, 24,
       "relay/fanout_budget.py:limits_from_settings (every split decision)",
       "Most workers one fan-out tree may hold, children and merge together (one slot is kept "
       "for the merge). Re-read at each split; a split that would exceed it is trimmed or run "
       "in one conversation. Default 24 changes nothing: one split makes at most 12 children."),

    _k("fanout_max_active", EACH_GATE, 3,
       "relay/fanout_budget.py:limits_from_settings (every split decision)",
       "Most workers of one tree that may be running when it asks for more. Absent means the "
       "operator's maxtabs (default 3, the fleet's own concurrency cap), so it never lowers "
       "what the fleet already runs at."),

    _k("fanout_max_turns", EACH_GATE, 400,
       "relay/fanout_budget.py:limits_from_settings (every split decision)",
       "Total conversation turns one tree may have used before it may not split again."),

    _k("fanout_max_wall_min", EACH_GATE, 120,
       "relay/fanout_budget.py:limits_from_settings (every split decision)",
       "Minutes since a tree's root split before it may not split again."),

    _k("fanout_max_depth", EACH_GATE, 2,
       "relay/fanout.py:configured_max_depth (every split decision)",
       "How many levels deep a fan-out tree may split (1..3). 1 means only the top-level goal "
       "splits. The value reported as in effect (status.json fanout_depth) can be lower than "
       "the setting while the merge of nested splits is not enabled."),

    _k("fanout_hierarchical_merge", EACH_GATE, "on",
       "relay/fanout.py:hierarchical_merge_setting (every split decision)",
       "off|on. on lets fanout_max_depth above 1 take effect; off caps the depth at 1 whatever "
       "fanout_max_depth says. On by default since nested merging was verified on small goals "
       "(43 split trees, no duplicates, no false completions). Re-read at each split with no restart; "
       "trees already split keep going."),

    _k("merge_conversation", EACH_GATE, "fresh",
       "relay/conversation_saving.py:merge_conversation_setting (every split and every merge)",
       "fresh|parent. parent runs a fan-out family's merge in the PARENT worker's existing "
       "conversation (a socket parent that has spoken) instead of opening a fresh aggregator "
       "conversation; the merge stays its own goal with the same checks, slot and resume key. "
       "A family without a recorded parent conversation merges in a fresh one. Default fresh "
       "until a merge has been measured to answer as well in the parent's conversation. "
       "Re-read at each split and each merge with no restart."),

    # Recovery. Read by the supervisor on every cycle (about every 15 s) through
    # relay/fleet_resume.py:auto_resume_enabled; "each_gate" is the nearest declared boundary:
    # a decision per cycle, work already in flight untouched.
    _k("fleet_auto_resume", EACH_GATE, "on",
       "relay/fleet_resume.py:auto_resume_setting (every supervisor cycle)",
       "off|on. on lets the supervisor resume an interrupted fleet run by itself, behind the loop "
       "guard (at most 3 automatic resumes per run, 5 min x 2^n backoff, never after a stop, never "
       "while a coordinator is live, never under the disk floor). Queued goals wait for the "
       "interrupted run's resume instead of starting a fresh coordinator. Re-read each supervisor "
       "cycle with no restart. An MCP_FLEET_AUTORESUME environment variable, when set, beats it. "
       "Default on, matching the startup resume that was always on."),

    # Read by the supervisor only once its own code has been found stale (a changed
    # scripts/supervisor.ps1), never on an ordinary cycle.
    _k("supervisor_self_restart", EACH_GATE, "on",
       "relay/code_staleness.py:self_restart_setting (supervisor cycle, only once its code is stale)",
       "off|on. A supervisor is a PowerShell script loaded once, so a merged change to "
       "scripts/supervisor.ps1 does nothing until it restarts. on lets the supervisor replace "
       "itself, but only when it is safe: no coordinator running, no interrupted run pending or "
       "being resumed, no review or local-loop run, the bridge idle, the new script parses, and "
       "not within 10 minutes of its last self-restart. off only reports that a restart is "
       "needed. Re-read each cycle with no restart. Default on: the failure it removes (a "
       "stale supervisor running a dry-run resume) already happened, and the gates above are "
       "the same idle rule the server's stale-code cycle uses."),

    # Whether to host a Dev Tunnel for an EXTERNAL client even on the connector-less route
    # (relay/code_staleness.py). ON by default. The agent's tool_transport says how the AGENT
    # reaches its tools; it does NOT say whether an outside client (a separate Claude session,
    # say) needs the local server published through a tunnel. When those two were conflated the
    # tunnel went unhosted after a reboot on a connector-less PC and the outside client could not
    # connect. The supervisor reads this each cycle: on means Test-TunnelRouteWanted returns true
    # whatever the route is; off restores the old behaviour (connector-less hosts no tunnel).
    _k("host_tunnel_for_external_clients", EACH_GATE, "on",
       "relay/code_staleness.py:host_tunnel_for_external_clients_setting (every supervisor cycle)",
       "off|on. on: the supervisor hosts and watches a Dev Tunnel even when tool_transport is "
       "connector-less, because an external client (not a fleet agent) reaches the local server "
       "through that tunnel. off: the old behaviour, a connector-less route hosts no tunnel. "
       "Re-read each supervisor cycle with no restart. A tunnel already running is left alone "
       "either way. Default on: the reboot that left the tunnel unhosted stranded an outside "
       "client that depended on it."),

    # The bridge's tool-call probe (bridge/copilot_bridge.py). It decides when to send, so the
    # timer re-reads this every few minutes; "each_gate" is the nearest declared boundary: a
    # decision per tick, a probe already in flight untouched.
    _k("tool_probe_idle_min", EACH_GATE, 30,
       "bridge/copilot_bridge.py:_probe_interval_s (every probe tick, about every 5 min)",
       "0|15|30|60 (minutes; 0 = never probe). How long the tool path may go without any proof "
       "before the bridge sends a probe message -- a real tool call that succeeded within the "
       "interval counts as proof and no message is sent. Re-read about every 5 minutes with no "
       "restart. A MCP_TOOL_PROBE_SEC environment variable, when set, beats it. Default 30 "
       "(the probe used to run every 10 minutes whatever else was happening)."),

    _k("autoretry", SWEEP_START, 1,
       "relay/fleet_runner.py:settings_autoretry (once, before the sweep)",
       "Takes effect on the NEXT run."),

    _k("autoretry_max", SWEEP_START, 2,
       "relay/fleet_runner.py:settings_autoretry (once, before the sweep)",
       "Takes effect on the NEXT run. Clamped to 0..3 here and to 1..3 in the panel: "
       "0 means off and only the file can say it, because the panel expresses off "
       "with the autoretry toggle instead."),

    _k("fanout", SWEEP_START, True,
       "relay/task_router.py:_wants_fanout (once per autostart goal)",
       "Governs every coordinator start (autostart and the cockpit's own launches). Unset "
       "means ON: only an explicit fanout=off turns it off. Each goal is still judged "
       "separately, so a goal that fits costs nothing."),

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
    _k("sidebar_section_cap", UI_ONLY, 8, "ui/FleetCockpit.cs (writes), ui/CopilotChat.cs (reads)",
       "How many conversations each sidebar section shows before '+N more' (the Fleet runs "
       "section especially). Chosen in the cockpit settings popup; the chat window re-reads it "
       "when settings.txt changes. 0 shows every conversation."),
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
