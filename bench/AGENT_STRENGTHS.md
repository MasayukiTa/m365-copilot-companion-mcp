# Drawing out the agent's strengths — where this system can be genuinely great

Date: 2026-06-21

Not "catch up to Claude Code" (covered in M365_HARDENING_AND_UX.md) — this is the other axis:
**amplify what is structurally ours.** Each bet below exploits an asset a single-session, per-token-
billed CLI structurally lacks.

## The four structural assets

1. **Cheap massive parallelism** — the fleet drives an M365 *seat*, not a per-token API. Running 20
   workers costs ~the same as 1. For Claude Code, N attempts = N× the API bill. So parallelism that
   is uneconomic there is ~free here. **This is the biggest, most defensible edge.**
2. **A self-improvement controller** — measures its own pass@1, A/B-tests changes with a frozen
   statistical judge, keeps only real gains. Neither Claude Code nor Codex ships this.
3. **An enterprise substrate** — Graph/SharePoint/Teams context + governance, available to no generic
   CLI.
4. **A self-measurement harness** — decoupled solve/grade, burned hygiene, sentinels. The agent can
   know its own competence.

## Bet #1 (headline): turn cheap parallelism into best-of-N + a verification swarm

The model is stochastic; one shot leaves quality on the table. Because N is ~free here:

- **Best-of-N solve**: fan out N diverse attempts at the SAME task (different approaches / effort /
  scaffold genomes), verify each with the existing red→green self-test + diff-gate, and **select the
  winner**. This raises per-task success above single-shot — and the self-improvement controller can
  *prove the lift* (best-of-N vs N=1 on a fresh slice, McNemar-gated) and the dashboard can show it.
- **Verification swarm**: after a candidate is DONE, spawn K independent adversarial verifiers (the
  refuter pattern already exists) — keep the patch only if a majority fail to refute. Reliability up,
  false-DONE down.

Why it's defensible: Claude Code *can* do subagents, but paying N× tokens for best-of-N on every task
is not economic; here it is the natural mode. **The whole point of the fleet stops being "do many
different tasks" and becomes "do one task many ways and keep the best."**

Honest caveats: (a) best-of-N is only as good as the SELECTOR — a weak verifier picks the wrong
winner; our edge depends on the self-test/grade being strong (and that is itself A/B-improvable). (b)
It costs wall-clock and Edge/RAM — so it is **gated by the hardening** (can't run 20 if Edge wedges).
Hardening and this bet are coupled: hardening *unlocks* the strength.

## Bet #2: a self-knowing, calibrated agent

The measurement harness lets the agent state *measured* competence, not vibes: "I resolve
django-forms-class tasks at ~85% (fresh Verified); this task looks like that class, so high
confidence" — or "this looks like the wrong-layer cluster I miss 40% of the time; I'll fan out
best-of-N and verify harder." Calibrated, evidence-backed confidence is a trust feature Claude Code
does not expose, and it lets the agent **spend parallelism where it's weak** (route hard classes to
best-of-N, easy ones to single-shot) — efficiency from self-knowledge.

## Bet #3: scaffold/prompt evolution as a service (the controller, generalized)

The controller A/B-tests scaffold cards today. Generalize the genome to include prompts, tool
choices, routing, turn budgets — anything in the harness. Then the rigorous-A/B machinery becomes a
product: "propose any change to how the agent works → get a frozen, significance-gated yes/no." Most
teams change agent prompts on vibes; this system changes them on evidence. Per-repo specialization
follows: learn (domain-general-within-this-repo) tweaks that help on *your* codebase — an agent that
measurably gets better at YOUR code over weeks (with per-repo burned hygiene so it never fools
itself).

## Bet #4: enterprise-grounded coding + governance

Auto-pull the company's coding standards / architecture docs / the relevant ticket from
Graph/SharePoint into the task context, so output matches house style because it *read the house
docs*. Pair with governance: every run audited to M365, risky steps approved via Teams. "Compliant,
context-aware autonomous coding" is a market a local CLI structurally cannot serve.

## The synthesis (the identity to aim for)

These compound: **cheap parallelism × self-improvement × self-measurement = an agent that does each
task many ways and keeps the best, knows how good it is, gets measurably better at your codebase over
time, and can prove every gain — at seat cost, not per-token.** That is not a Claude Code clone; it
is a different animal that a per-token single-session CLI cannot easily become.

## First experiment (cheapest proof, do after the running measurement frees the fleet)

Prototype **best-of-N=4 vs N=1** on a small fresh Verified slice, selecting by the existing self-test
+ diff-gate, and have the self-improvement controller measure the lift (paired, McNemar-gated) and
render it on the dashboard. If best-of-N shows a real, gated lift, that single result both validates
the headline strength and gives the dashboard its first "win". The genome/propose machinery already
exists to make N-diversity (different scaffold genomes) the source of the N attempts.

Build order: this is fleet-heavy → after the measurement. Until then it is design + a measurement-safe
"selector" module (score N captured diffs, pick the best) that can be unit-tested now.
