# -*- coding: utf-8 -*-
"""Why one waiting worker is not moving: queue rank, workers ahead, or a resource wait.

WHY THIS EXISTS (owner requirement, 2026-10-27 16:16). When many tasks are queued the owner
asks "why isn't MY task progressing" and the honest answer is mechanical, not a mood: the
goal is Nth in the admission queue, M goals sit ahead of it, and/or admission is held because
a resource (the concurrency cap, the cloud send rate, or the disk floor) is at its bound. The
admission loop in relay/relay_fleet.py (the `while pending:` sweep around line 11096) decides
all of this already; this module reads the SAME facts, deterministically, and renders ONE line.

THIS MODULE IS PURE AND READ-ONLY. It takes a small immutable snapshot of what the sweep sees
-- the goal's position in the `pending` admission queue, how many goals are ahead of it, the
live concurrency cap and active count, and three resource booleans -- and returns one typed
Stall plus a one-line Japanese headline. It never touches a worker, the fleet, or disk. That
is what makes it unit-testable without a running fleet (see ui/test_a_stalled_card_says_why.py).

WHAT COUNTS AS "STALLED". Only a goal that is WAITING TO START (not yet admitted: status
pending/ready and turn 0, still in the `pending` queue) can be explained by a queue rank. A
goal that has already been admitted and is taking turns is not queue-blocked -- its own turn
reason (relay/stuck_cause.py) answers for it -- so `stall_reason` returns a `running` Stall
with no queue headline for those, leaving the turn-level diagnosis to its owner.

KINDS of the resource bound that holds admission, mirroring the sweep's own gates:
  * "capacity"  -- socket_capacity.admits_another_goal is False: the concurrency cap is full
                   (ram_safeguard_cap at line 11074). The goal waits for a running goal to end.
  * "rate"      -- the cloud send rate is at the ceiling (rate_headroom_ok is False): tokens
                   are the bound, the goal is READY and waiting for a send token.
  * "disk"      -- the disk floor would be breached by admitting another build (disk_admission_ok
                   is False, line 11156): admission is deferred until C: frees up.
  * "token"     -- no socket token / route shut (line 11100): the goal holds in the queue.
  * ""          -- no resource bound; the goal is simply behind others in the queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

#: The resource bounds that can hold admission, in the order the sweep tests them. A goal at
#: the HEAD of the queue is held by at most one of these; a goal further back is held by its
#: rank first (someone ahead must clear), and the resource only matters once it reaches the head.
RESOURCE_KINDS = ("token", "capacity", "disk", "rate", "")

#: Human head for each resource wait, Japanese. Only used when the goal is at (or effectively
#: at) the head and that resource is the thing in its way.
_RESOURCE_HEAD = {
    "capacity": "同時実行の空き待ち",
    "rate": "送信レート上限待ち",
    "disk": "ディスク空き待ち",
    "token": "ソケットトークン待ち",
}


@dataclass(frozen=True)
class Stall:
    """One worker's stall, computed from the admission snapshot. Immutable."""
    stalled: bool                   # True only when waiting-to-start behind a rank or a resource
    rank: int = 0                   # 1-based position in the admission queue (0 = not queued)
    ahead: int = 0                  # goals ahead of this one in the queue
    resource: str = ""              # one of RESOURCE_KINDS; the bound holding admission, or ""
    active: int = 0                 # goals currently admitted/running
    cap: int = 0                    # live concurrency cap (lane ceiling after the RAM safeguard)
    at_head: bool = False           # True when nothing is ahead of it in the queue
    detail: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["headline"] = self.headline()
        return d

    def headline(self) -> str:
        """The one line shown on the card. Concrete: rank, how many ahead, the resource wait."""
        if not self.stalled:
            return ""
        parts = []
        if self.rank > 0:
            parts.append("待ち行列 %d 番目" % self.rank)
        if self.ahead > 0:
            parts.append("前に %d 件" % self.ahead)
        res = _RESOURCE_HEAD.get(self.resource, "")
        if res:
            if self.resource == "capacity" and (self.cap or self.active):
                res = "%s(実行 %d/%d)" % (res, self.active, self.cap)
            parts.append(res)
        if not parts:
            # Queued, at the head, no resource bound found: it is simply about to go.
            return "まもなく開始(待ち要因なし)"
        return "・".join(parts)


def resource_wait(*, at_head, token_ok=True, admits_another=True,
                  disk_ok=True, rate_ok=True) -> str:
    """Which single resource bound, if any, is holding admission RIGHT NOW.

    Mirrors the order of the gates in the `while pending:` sweep: token/route first (the goal
    cannot even be considered without a socket), then the concurrency cap, then the disk floor,
    then the send rate. Returns one of RESOURCE_KINDS. A goal that is not yet at the head of the
    queue is held by its rank, not by a resource, so this returns "" for it -- the resource is
    only its blocker once it reaches the head.
    """
    if not at_head:
        return ""
    if not token_ok:
        return "token"
    if not admits_another:
        return "capacity"
    if not disk_ok:
        return "disk"
    if not rate_ok:
        return "rate"
    return ""


def stall_reason(*, status, turn, ahead, queue_len,
                 active=0, cap=0,
                 token_ok=True, admits_another=True, disk_ok=True, rate_ok=True) -> Stall:
    """Compute why a waiting-to-start worker is not progressing. Pure.

    Inputs are the facts the admission sweep already has:
      * status, turn -- a goal with turn > 0, or in a terminal/awaiting state, has been admitted;
        it is not queue-blocked and gets a non-stalled `running` Stall (its turn reason answers).
      * ahead      -- goals before it in the `pending` admission queue (0 => at the head).
      * queue_len  -- total goals in the queue, for context (not required for the headline).
      * active, cap -- live concurrency numbers, shown with a capacity wait.
      * token_ok/admits_another/disk_ok/rate_ok -- the four admission gates, in sweep order.

    A goal only counts as STALLED when it is waiting to start (not admitted) AND there is a real
    reason: either something ahead of it, or a resource bound at the head. A lone goal at the
    head with every gate open is `stalled=False` (nothing is in its way; it is about to send).
    """
    s = (status or "").strip().lower()
    admitted = (int(turn or 0) > 0) or s in (
        "verifying", "researching", "refuting", "awaiting_gate",
        "done", "stuck", "maxturns", "error")
    if admitted:
        return Stall(stalled=False, rank=0, ahead=0, resource="",
                     active=int(active or 0), cap=int(cap or 0), at_head=False,
                     detail="admitted; turn-level reason applies")

    ahead_n = max(0, int(ahead or 0))
    at_head = ahead_n == 0
    rank = ahead_n + 1
    resource = resource_wait(at_head=at_head, token_ok=token_ok,
                             admits_another=admits_another, disk_ok=disk_ok, rate_ok=rate_ok)
    # Stalled if something is ahead, or a resource bound holds it at the head.
    stalled = (ahead_n > 0) or bool(resource)
    return Stall(stalled=stalled, rank=rank, ahead=ahead_n, resource=resource,
                 active=int(active or 0), cap=int(cap or 0), at_head=at_head,
                 detail="")
